"""
cogs/stats.py – Auction statistics and leaderboards.

Field mapping (DB short name → meaning):
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  iv   = total_iv_percent    hp/atk/def/spa/spd/spe = individual IVs
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id           (stored directly as int — no more avatar URL parsing)

Commands:
  j!stats @user          — full stats for a user
  j!lb sellers           — top sellers by money earned
  j!lb bidders           — top bidders by money spent
  j!lb listed            — most auctions listed (as seller)
  j!lb won               — most auctions won (as bidder)
  j!lb pokemon           — most auctioned Pokémon (normal/shiny/gmax/overall)
  j!lb expensive         — biggest single sales ever
  j!market               — server-wide market insights
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

import config
from config import REPLY
from utils import format_winning_bid, shiny_prefix

log = logging.getLogger(__name__)

_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

LB_SIZE = 10


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val/1_000:.1f}k"
    return f"{int(val):,}"


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"`#{i+1}`"


def _fmt_user(user_id, name: str | None = None) -> str:
    """
    Format a user as a Discord mention if we have their ID,
    otherwise fall back to showing just their name.
    """
    if user_id is not None:
        try:
            mention = f"<@{int(user_id)}>"
            return f"{mention} (`{name}`)" if name else mention
        except (TypeError, ValueError):
            pass
    # No id — old record, just show the stored name
    return f"`{name}`" if name else "`Unknown`"


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _get_date_range_footer() -> str:
    """Return 'Data from X to Y' based on ts range in the collection."""
    try:
        oldest = _col.find_one(
            {"ts": {"$exists": True, "$ne": None}},
            {"ts": 1},
            sort=[("ts", 1)],
        )
        newest = _col.find_one(
            {"ts": {"$exists": True, "$ne": None}},
            {"ts": 1},
            sort=[("ts", -1)],
        )
        if oldest and newest:
            fmt   = "%b %d, %Y"
            start = datetime.fromtimestamp(oldest["ts"], tz=timezone.utc).strftime(fmt)
            end   = datetime.fromtimestamp(newest["ts"], tz=timezone.utc).strftime(fmt)
            return f"📅 Data from **{start}** to **{end}**"
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# USER RANK LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

def _get_user_rank(lb_type: str, uid: int) -> dict | None:
    """
    Find the calling user's rank and stats for a given ranked leaderboard.
    Returns dict with rank, total, count, total_entries — or None if not ranked.
    Works by running the full aggregation (no $limit) and scanning for the user.
    """
    try:
        if lb_type in ("sellers", "listed"):
            sort_field = "total" if lb_type == "sellers" else "count"
            pipe = [
                {"$match": {"$or": [
                    {"sid": {"$exists": True, "$ne": None}},
                    {"sn":  {"$exists": True, "$ne": None}},
                ]}},
                {"$group": {
                    "_id": {
                        "$cond": {
                            "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                            "then": {"type": "id",   "val": "$sid"},
                            "else": {"type": "name", "val": "$sn"},
                        }
                    },
                    "sid":   {"$last": "$sid"},
                    "total": {"$sum": "$bid"},
                    "count": {"$sum": 1},
                }},
                {"$sort": {sort_field: -1}},
            ]
            rows = list(_col.aggregate(pipe))
            for i, r in enumerate(rows):
                if r.get("sid") == uid:
                    return {"rank": i + 1, "total": r["total"], "count": r["count"], "total_entries": len(rows)}

        elif lb_type in ("bidders", "won"):
            sort_field = "total" if lb_type == "bidders" else "count"
            pipe = [
                {"$match": {"bdr": {"$exists": True}}},
                {"$group": {
                    "_id":   "$bdr",
                    "total": {"$sum": "$bid"},
                    "count": {"$sum": 1},
                }},
                {"$sort": {sort_field: -1}},
            ]
            rows = list(_col.aggregate(pipe))
            for i, r in enumerate(rows):
                if r["_id"] == uid:
                    return {"rank": i + 1, "total": r["total"], "count": r["count"], "total_entries": len(rows)}

    except Exception:
        log.exception("Error in _get_user_rank")
    return None


def _rank_footer(lb_type: str, uid: int) -> str | None:
    """
    Build the 'You are at #X' line for a ranked leaderboard.
    Returns None for leaderboard types that don't rank users (pokemon, expensive).
    """
    if lb_type not in ("sellers", "listed", "bidders", "won"):
        return None

    data = _get_user_rank(lb_type, uid)
    if not data:
        return None

    rank          = data["rank"]
    total_entries = data["total_entries"]
    medal         = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"**#{rank:,}**"

    if lb_type == "sellers":
        stat_s = f"`{_fmt(data['total'])}` earned across `{data['count']:,}` sales"
    elif lb_type == "listed":
        stat_s = f"`{data['count']:,}` auctions listed"
    elif lb_type == "bidders":
        stat_s = f"`{_fmt(data['total'])}` spent across `{data['count']:,}` wins"
    else:  # won
        stat_s = f"`{data['count']:,}` auctions won"

    return f"📍 You are {medal} out of `{total_entries:,}` — {stat_s}"


# ─────────────────────────────────────────────────────────────────────────────
# SELLER MATCH HELPERS
#
# seller_id (sid) is now stored directly as an int in the DB.
# We match on the int value, but also accept the seller's name as a fallback
# for records scraped before the schema change (where sid may be None).
# ─────────────────────────────────────────────────────────────────────────────

def _seller_match_by_id(uid: int) -> dict:
    """Match auctions where sid == uid (exact int match)."""
    return {"sid": uid}


def _seller_match_any(uid: int) -> dict:
    """
    Match auctions for a seller using their ID.
    sid is stored as an int. A simple $eq covers it.
    """
    return {"sid": uid}


# ─────────────────────────────────────────────────────────────────────────────
# SELLER LEADERBOARD AGGREGATION
#
# sid is now a real indexed int field — we can aggregate directly in MongoDB.
# ─────────────────────────────────────────────────────────────────────────────

def _seller_leaderboard(mode: str) -> list[dict]:
    """
    Return top sellers aggregated by sid when available, falling back to sn
    for old records where sid was not yet stored.
    mode: "total" → sort by money earned, "count" → sort by auctions listed.
    Returns list of dicts: {id, name, total, count}
    """
    sort_field = "total" if mode == "total" else "count"

    pipe = [
        # Include any record that has at least a name or an id
        {"$match": {"$or": [
            {"sid": {"$exists": True, "$ne": None}},
            {"sn":  {"$exists": True, "$ne": None}},
        ]}},
        # Group key: prefer sid (int) if present, otherwise fall back to sn string
        {"$group": {
            "_id": {
                "$cond": {
                    "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                    "then": {"type": "id",   "val": "$sid"},
                    "else": {"type": "name", "val": "$sn"},
                }
            },
            "name":  {"$last": "$sn"},
            "sid":   {"$last": "$sid"},
            "total": {"$sum": "$bid"},
            "count": {"$sum": 1},
        }},
        {"$sort": {sort_field: -1}},
        {"$limit": LB_SIZE},
    ]

    rows = list(_col.aggregate(pipe))
    return [
        {
            "id":    r.get("sid"),           # None for name-only (old) records
            "name":  r.get("name") or "Unknown",
            "total": r["total"],
            "count": r["count"],
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# USER STATS
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_stats_view(user: discord.User | discord.Member) -> discord.ui.LayoutView:
    uid = user.id

    # ── As bidder (bdr = bidder_id, stored as int) ────────────────────────────
    bidder_match = {"bdr": uid}

    won_res = list(_col.aggregate([
        {"$match": bidder_match},
        {"$group": {
            "_id":         None,
            "total_spent": {"$sum": "$bid"},
            "count":       {"$sum": 1},
            "avg_spent":   {"$avg": "$bid"},
        }},
    ]))
    won = won_res[0] if won_res else {}

    fav_buys = list(_col.aggregate([
        {"$match": bidder_match},
        {"$group": {"_id": "$pn", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 3},
    ]))

    priciest_buy = _col.find_one(bidder_match, sort=[("bid", -1)])

    # ── As seller (sid = seller_id, stored as int) ────────────────────────────
    seller_match = _seller_match_any(uid)

    sold_res = list(_col.aggregate([
        {"$match": seller_match},
        {"$group": {
            "_id":          None,
            "total_earned": {"$sum": "$bid"},
            "count":        {"$sum": 1},
            "avg_earned":   {"$avg": "$bid"},
        }},
    ]))
    sold = sold_res[0] if sold_res else {}

    best_sales = list(
        _col.find(seller_match).sort("bid", -1).limit(3)
    )

    fav_sells = list(_col.aggregate([
        {"$match": seller_match},
        {"$group": {"_id": "$pn", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 3},
    ]))

    won_count  = won.get("count", 0)
    sold_count = sold.get("count", 0)

    if won_count == 0 and sold_count == 0:
        return _error_view(f"❌ No auction activity found for {user.display_name}.")

    # ── Bidder block ───────────────────────────────────────────────────────────
    if won_count > 0:
        fav_buy_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in fav_buys) or "—"
        pb_name   = priciest_buy.get("pn", "?") if priciest_buy else "?"
        pb_price  = _fmt(priciest_buy.get("bid", 0)) if priciest_buy else "?"
        pb_shiny  = shiny_prefix(priciest_buy) if priciest_buy else ""
        bidder_text = (
            f"**💸 As Bidder**\n"
            f"{REPLY} **Auctions Won:** `{won_count:,}`\n"
            f"{REPLY} **Total Spent:** `{_fmt(won.get('total_spent', 0))}`\n"
            f"{REPLY} **Avg per Win:** `{_fmt(won.get('avg_spent', 0))}`\n"
            f"{REPLY} **Most Expensive Buy:** {pb_shiny}`{pb_name}` — `{pb_price}`\n"
            f"{REPLY} **Favourite Buys:** {fav_buy_s}"
        )
    else:
        bidder_text = f"**💸 As Bidder**\n{REPLY} _No auction wins recorded._"

    # ── Seller block ───────────────────────────────────────────────────────────
    if sold_count > 0:
        fav_sell_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in fav_sells) or "—"
        best_sale_lines = "\n".join(
            f"{REPLY} {shiny_prefix(s)}`{s.get('pn','?')}` — `{_fmt(s.get('bid',0))}`"
            for s in best_sales
        )
        seller_text = (
            f"**🏷️ As Seller**\n"
            f"{REPLY} **Auctions Listed:** `{sold_count:,}`\n"
            f"{REPLY} **Total Earned:** `{_fmt(sold.get('total_earned', 0))}`\n"
            f"{REPLY} **Avg Sale Price:** `{_fmt(sold.get('avg_earned', 0))}`\n"
            f"{REPLY} **Favourite Sells:** {fav_sell_s}\n"
            f"{REPLY} **Best Sales:**\n{best_sale_lines}"
        )
    else:
        seller_text = f"**🏷️ As Seller**\n{REPLY} _No auctions listed._"

    # ── Net balance ────────────────────────────────────────────────────────────
    net     = sold.get("total_earned", 0) - won.get("total_spent", 0)
    net_s   = f"+{_fmt(net)}" if net >= 0 else f"-{_fmt(abs(net))}"
    net_col = "🟢" if net >= 0 else "🔴"
    net_text = f"**⚖️ Net Balance:** {net_col} `{net_s}` _(earned − spent)_"

    date_footer = _get_date_range_footer()

    comps = [
        discord.ui.TextDisplay(content=f"## 📊 Auction Stats — {user.display_name}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=bidder_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=seller_text),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=net_text),
    ]
    if date_footer:
        comps += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=f"-# {date_footer}"),
        ]

    class StatsView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=180)

    return StatsView()


# ─────────────────────────────────────────────────────────────────────────────
# LEADERBOARD BODY
# ─────────────────────────────────────────────────────────────────────────────

_LB_TITLES = {
    "sellers":         "🏆 Top Sellers — Money Earned",
    "bidders":         "💸 Top Bidders — Money Spent",
    "listed":          "📋 Most Auctions Listed",
    "won":             "🎯 Most Auctions Won",
    "pokemon_overall": "📦 Most Auctioned — Overall",
    "pokemon_normal":  "🔵 Most Auctioned — Normal",
    "pokemon_shiny":   "✨ Most Auctioned — Shiny",
    "pokemon_gmax":    "⚡ Most Auctioned — Gigantamax",
    "expensive":       "💰 Biggest Sales Ever",
}


def _build_lb_body(lb_type: str, variant: str = "overall", caller_id: int | None = None) -> str:
    rows:  list = []
    lines: list = []

    if lb_type == "sellers":
        # Aggregated directly in MongoDB using sid (int) — no more Python-side URL parsing
        rows = _seller_leaderboard("total")
        lines = [
            f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{_fmt(r['total'])}` from `{r['count']:,}` sales"
            for i, r in enumerate(rows)
        ]

    elif lb_type == "listed":
        rows = _seller_leaderboard("count")
        lines = [
            f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{r['count']:,}` auctions"
            for i, r in enumerate(rows)
        ]

    elif lb_type == "bidders":
        # bdr = bidder_id (int)
        pipe = [
            {"$match": {"bdr": {"$exists": True}}},
            {"$group": {
                "_id":   "$bdr",
                "total": {"$sum": "$bid"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"total": -1}},
            {"$limit": LB_SIZE},
        ]
        rows = list(_col.aggregate(pipe))
        lines = [
            f"{_medal(i)} <@{r['_id']}> — `{_fmt(r['total'])}` across `{r['count']:,}` wins"
            for i, r in enumerate(rows)
        ]

    elif lb_type == "won":
        pipe = [
            {"$match": {"bdr": {"$exists": True}}},
            {"$group": {
                "_id":   "$bdr",
                "count": {"$sum": 1},
            }},
            {"$sort": {"count": -1}},
            {"$limit": LB_SIZE},
        ]
        rows = list(_col.aggregate(pipe))
        lines = [
            f"{_medal(i)} <@{r['_id']}> — `{r['count']:,}` wins"
            for i, r in enumerate(rows)
        ]

    elif lb_type == "pokemon":
        # sh = shiny, gx = gmax, pn = pokemon_name
        match_filter: dict = {}
        if variant == "shiny":
            match_filter = {"sh": True, "gx": {"$ne": True}}
        elif variant == "gmax":
            match_filter = {"gx": True}
        elif variant == "normal":
            match_filter = {"sh": {"$ne": True}, "gx": {"$ne": True}}

        pipe = [
            {"$match": match_filter},
            {"$group": {
                "_id":   "$pn",
                "count": {"$sum": 1},
                "avg":   {"$avg": "$bid"},
            }},
            {"$sort": {"count": -1}},
            {"$limit": LB_SIZE},
        ]
        rows = list(_col.aggregate(pipe))
        lines = [
            f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions  •  avg `{_fmt(r['avg'])}`"
            for i, r in enumerate(rows)
        ]

    elif lb_type == "expensive":
        # bid = winning_bid, pn = pokemon_name, sn = seller_name, sid = seller_id
        # bdr = bidder_id, aid = auction_id, sh = shiny, gx = gmax
        rows = list(
            _col.find(
                {},
                {"aid": 1, "pn": 1, "bid": 1, "sn": 1, "sid": 1, "bdr": 1, "sh": 1, "gx": 1}
            ).sort("bid", -1).limit(LB_SIZE)
        )
        for i, r in enumerate(rows):
            prefix    = shiny_prefix(r)
            seller_s  = _fmt_user(r["sid"], r.get("sn")) if r.get("sid") else f"`{r.get('sn', '?')}`"
            bidder_s  = f"<@{r['bdr']}>" if r.get("bdr") else "Unknown"
            lines.append(
                f"{_medal(i)} {prefix}**{r.get('pn','?')}** — `{_fmt(r.get('bid', 0))}`\n"
                f"　Sold by {seller_s} → {bidder_s}  •  `#{r.get('aid','?')}`"
            )
    else:
        return "❌ Unknown leaderboard type."

    if not rows:
        return "❌ No data found for this leaderboard."

    body = "\n".join(lines)

    # Append the caller's personal rank if this leaderboard supports it
    if caller_id is not None:
        footer = _rank_footer(lb_type, caller_id)
        if footer:
            body += f"\n\n{footer}"

    return body


# ─────────────────────────────────────────────────────────────────────────────
# LEADERBOARD VIEW  (with dropdown)
# ─────────────────────────────────────────────────────────────────────────────

def _build_lb_selector_view(current_type: str = "pokemon", current_variant: str = "shiny", caller_id: int | None = None) -> discord.ui.LayoutView:
    title_key   = f"{current_type}_{current_variant}" if current_type == "pokemon" else current_type
    title       = _LB_TITLES.get(title_key, "🏆 Leaderboard")
    body        = _build_lb_body(current_type, current_variant, caller_id=caller_id)
    date_footer = _get_date_range_footer()

    class TypeSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Top Sellers",        value="sellers",         emoji="🏆", description="Most money earned"),
                discord.SelectOption(label="Top Bidders",        value="bidders",         emoji="💸", description="Most money spent"),
                discord.SelectOption(label="Most Listed",        value="listed",          emoji="📋", description="Most auctions listed as seller"),
                discord.SelectOption(label="Most Won",           value="won",             emoji="🎯", description="Most auctions won as bidder"),
                discord.SelectOption(label="Pokémon — Overall",  value="pokemon_overall", emoji="📦"),
                discord.SelectOption(label="Pokémon — Normal",   value="pokemon_normal",  emoji="🔵"),
                discord.SelectOption(label="Pokémon — Shiny",    value="pokemon_shiny",   emoji="✨"),
                discord.SelectOption(label="Pokémon — Gmax",     value="pokemon_gmax",    emoji="⚡"),
                discord.SelectOption(label="Biggest Sales Ever", value="expensive",       emoji="💰", description="Top single sales"),
            ]
            for o in options:
                if o.value == title_key:
                    o.default = True
            super().__init__(placeholder="Switch leaderboard…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                val = self.values[0]
                caller_id = interaction.user.id
                if val.startswith("pokemon_"):
                    lb_t, lb_v = "pokemon", val.split("_", 1)[1]
                else:
                    lb_t, lb_v = val, "overall"
                new_view = _build_lb_selector_view(lb_t, lb_v, caller_id=caller_id)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in leaderboard select callback")
                await interaction.edit_original_response(
                    view=_error_view("❌ Something went wrong loading that leaderboard.")
                )

    comps = [
        discord.ui.TextDisplay(content=f"## {title}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=body),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
    ]
    if date_footer:
        comps += [
            discord.ui.TextDisplay(content=f"-# {date_footer}"),
            discord.ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small),
        ]
    comps.append(discord.ui.ActionRow(TypeSelect()))

    class LbSelectorView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return LbSelectorView()


# ─────────────────────────────────────────────────────────────────────────────
# MARKET INSIGHTS
# ─────────────────────────────────────────────────────────────────────────────

def _build_market_view() -> discord.ui.LayoutView:
    total_auctions = _col.count_documents({})
    vol_res        = list(_col.aggregate([{"$group": {"_id": None, "total": {"$sum": "$bid"}}}]))
    total_volume   = vol_res[0]["total"] if vol_res else 0

    # pn = pokemon_name, bid = winning_bid
    avg_price_rows = list(_col.aggregate([
        {"$group": {
            "_id":   "$pn",
            "avg":   {"$avg": "$bid"},
            "count": {"$sum": 1},
        }},
        {"$match": {"count": {"$gte": 5}}},
        {"$sort": {"avg": -1}},
        {"$limit": 5},
    ]))

    most_traded = list(_col.aggregate([
        {"$group": {"_id": "$pn", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 5},
    ]))

    rarest = list(_col.aggregate([
        {"$group": {"_id": "$pn", "count": {"$sum": 1}}},
        {"$sort": {"count": 1}},
        {"$limit": 5},
    ]))

    # ts = unix_timestamp
    active_month_res = list(_col.aggregate([
        {"$match": {"ts": {"$exists": True}}},
        {"$addFields": {
            "month": {"$dateToString": {
                "format": "%Y-%m",
                "date":   {"$toDate": {"$multiply": ["$ts", 1000]}}
            }}
        }},
        {"$group": {"_id": "$month", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1},
    ]))
    active_month       = active_month_res[0]["_id"]   if active_month_res else "?"
    active_month_count = active_month_res[0]["count"] if active_month_res else 0

    # sh = shiny, gx = gmax
    shiny_count  = _col.count_documents({"sh": True})
    gmax_count   = _col.count_documents({"gx": True})
    normal_count = total_auctions - shiny_count - gmax_count
    date_footer  = _get_date_range_footer()

    avg_lines = "\n".join(
        f"{REPLY} **{r['_id']}** — avg `{_fmt(r['avg'])}` over `{r['count']:,}` sales"
        for r in avg_price_rows
    ) or "_No data_"

    traded_lines = "\n".join(
        f"{REPLY} **{r['_id']}** — `{r['count']:,}` auctions"
        for r in most_traded
    ) or "_No data_"

    rarest_lines = "\n".join(
        f"{REPLY} **{r['_id']}** — `{r['count']:,}` auction(s)"
        for r in rarest
    ) or "_No data_"

    comps = [
        discord.ui.TextDisplay(content="## 🌐 Market Insights"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=(
            f"**📈 Market Overview**\n"
            f"{REPLY} **Total Auctions:** `{total_auctions:,}`\n"
            f"{REPLY} **Total Volume:** `{_fmt(total_volume)}`\n"
            f"{REPLY} **Normal:** `{normal_count:,}`  •  "
            f"**Shiny:** `{shiny_count:,}`  •  "
            f"**Gmax:** `{gmax_count:,}`\n"
            f"{REPLY} **Busiest Month:** `{active_month}` — `{active_month_count:,}` auctions"
        )),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=f"**💎 Most Expensive on Average** _(min 5 sales)_\n{avg_lines}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=f"**🔥 Most Traded Pokémon**\n{traded_lines}"),
        discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
        discord.ui.TextDisplay(content=f"**🌙 Rarest Pokémon** _(fewest sales)_\n{rarest_lines}"),
    ]
    if date_footer:
        comps += [
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=f"-# {date_footer}"),
        ]

    class MarketView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=180)

    return MarketView()


# ─────────────────────────────────────────────────────────────────────────────
# HELP VIEW
# ─────────────────────────────────────────────────────────────────────────────

def _build_help_view() -> discord.ui.LayoutView:
    text = (
        f"**Commands:**\n"
        f"{REPLY} `j!stats [@user]` — auction stats for yourself or another user\n"
        f"{REPLY} `j!lb sellers` — top sellers by money earned\n"
        f"{REPLY} `j!lb bidders` — top bidders by money spent\n"
        f"{REPLY} `j!lb listed` — most auctions listed\n"
        f"{REPLY} `j!lb won` — most auctions won\n"
        f"{REPLY} `j!lb pokemon [normal|shiny|gmax|overall]` — most auctioned Pokémon\n"
        f"{REPLY} `j!lb expensive` — biggest single sales ever\n"
        f"{REPLY} `j!market` — server-wide market insights\n\n"
        f"**Examples:**\n"
        f"{REPLY} `j!stats @user`\n"
        f"{REPLY} `j!lb pokemon shiny`\n"
        f"{REPLY} `j!lb sellers`\n"
        f"{REPLY} `j!market`"
    )

    class HelpView(discord.ui.LayoutView):
        container = discord.ui.Container(
            discord.ui.TextDisplay(content="## 📊 Stats & Leaderboards — Help"),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
        def __init__(self):
            super().__init__(timeout=180)

    return HelpView()


# ─────────────────────────────────────────────────────────────────────────────
# COG
# ─────────────────────────────────────────────────────────────────────────────

class Stats(commands.Cog):
    """Auction statistics and leaderboards"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="auction_stats", aliases=["stats"])
    @app_commands.describe(user="User to look up (leave empty for yourself)")
    async def stats_cmd(self, ctx: commands.Context, user: discord.Member | discord.User | None = None):
        """Show auction stats for a user"""
        target = user or ctx.author
        async with ctx.typing():
            view = _build_user_stats_view(target)
        await ctx.send(view=view, reference=ctx.message, mention_author=False)

    @commands.hybrid_command(name="lb", aliases=["leaderboard"])
    @app_commands.describe(
        lb_type="Type: sellers | bidders | listed | won | pokemon | expensive",
        variant="For pokemon: normal | shiny | gmax | overall",
    )
    async def lb_cmd(
        self,
        ctx: commands.Context,
        lb_type: str = "pokemon",
        variant: str = "shiny",
    ):
        """Show a leaderboard"""
        lb_type = lb_type.lower().strip()
        variant = variant.lower().strip()

        if lb_type not in {"sellers", "bidders", "listed", "won", "pokemon", "expensive"}:
            await ctx.send(view=_build_help_view(), reference=ctx.message, mention_author=False)
            return

        if variant not in {"normal", "shiny", "gmax", "overall"}:
            variant = "overall"

        async with ctx.typing():
            view = _build_lb_selector_view(lb_type, variant, caller_id=ctx.author.id)
        await ctx.send(view=view, reference=ctx.message, mention_author=False)

    @commands.hybrid_command(name="auction_insights", aliases=["ai"])
    async def market_cmd(self, ctx: commands.Context):
        """Server-wide market insights"""
        async with ctx.typing():
            view = _build_market_view()
        await ctx.send(view=view, reference=ctx.message, mention_author=False)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
