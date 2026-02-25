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
  sid  = seller_id           (stored directly as int)

Commands:
  j!stats [@user]        — full stats for a user (tabbed: overview / buying / selling)
  j!lb [type] [variant]  — leaderboards with dropdown switcher
  j!market               — server-wide market insights (tabbed dropdown)
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
from utils import shiny_prefix

log = logging.getLogger(__name__)

_mongo = MongoClient(config.MONGO_URI)
_db    = _mongo[config.MONGO_DB_NAME]
_col   = _db[config.MONGO_COLLECTION]

LB_SIZE = 10
SAFE_MENTIONS = discord.AllowedMentions.none()


# ═════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def _fmt(val: float) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return f"{int(val):,}"


def _medal(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"`#{i + 1}`"


def _fmt_user(user_id, name: str | None = None) -> str:
    if user_id is not None:
        try:
            mention = f"<@{int(user_id)}>"
            return f"{mention} (`{name}`)" if name else mention
        except (TypeError, ValueError):
            pass
    return f"`{name}`" if name else "`Unknown`"


def _sep(visible: bool = True) -> discord.ui.Separator:
    return discord.ui.Separator(visible=visible, spacing=discord.SeparatorSpacing.small)


def _get_date_range_footer() -> str:
    try:
        oldest = _col.find_one({"ts": {"$exists": True, "$ne": None}}, {"ts": 1}, sort=[("ts", 1)])
        newest = _col.find_one({"ts": {"$exists": True, "$ne": None}}, {"ts": 1}, sort=[("ts", -1)])
        if oldest and newest:
            fmt   = "%b %d, %Y"
            start = datetime.fromtimestamp(oldest["ts"], tz=timezone.utc).strftime(fmt)
            end   = datetime.fromtimestamp(newest["ts"], tz=timezone.utc).strftime(fmt)
            return f"📅 Data from **{start}** to **{end}**"
    except Exception:
        pass
    return ""


def _error_view(text: str) -> discord.ui.LayoutView:
    class EV(discord.ui.LayoutView):
        c = discord.ui.Container(
            discord.ui.TextDisplay(content=text),
            accent_colour=config.EMBED_COLOR,
        )
    return EV()


def _interleave_seps(sections: list[str], final_sep: bool = True) -> list:
    """Turn a list of text sections into alternating TextDisplay + Separator components.
    If final_sep is True, a separator is placed after the last section too."""
    comps = []
    for i, section in enumerate(sections):
        comps.append(discord.ui.TextDisplay(content=section))
        if final_sep or i < len(sections) - 1:
            comps.append(_sep())
    return comps


# ═════════════════════════════════════════════════════════════════════════════
# SELLER LEADERBOARD AGGREGATION
# ═════════════════════════════════════════════════════════════════════════════

def _seller_leaderboard(mode: str) -> list[dict]:
    """Aggregate top sellers by sid (int), falling back to sn for old records."""
    sort_field = "total" if mode == "total" else "count"
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
            "name":  {"$last": "$sn"},
            "sid":   {"$last": "$sid"},
            "total": {"$sum": "$bid"},
            "count": {"$sum": 1},
        }},
        {"$sort": {sort_field: -1}},
        {"$limit": LB_SIZE},
    ]
    rows = list(_col.aggregate(pipe))
    return [{"id": r.get("sid"), "name": r.get("name") or "Unknown", "total": r["total"], "count": r["count"]} for r in rows]


def _seller_match(uid: int) -> dict:
    return {"sid": uid}


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD RANK LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

def _get_user_rank(lb_type: str, uid: int) -> dict | None:
    try:
        if lb_type in ("sellers", "listed"):
            sort_field = "total" if lb_type == "sellers" else "count"
            pipe = [
                {"$match": {"$or": [{"sid": {"$exists": True, "$ne": None}}, {"sn": {"$exists": True, "$ne": None}}]}},
                {"$group": {
                    "_id": {"$cond": {
                        "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                        "then": {"type": "id",   "val": "$sid"},
                        "else": {"type": "name", "val": "$sn"},
                    }},
                    "sid": {"$last": "$sid"}, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
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
                {"$group": {"_id": "$bdr", "total": {"$sum": "$bid"}, "count": {"$sum": 1}}},
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
    if lb_type not in ("sellers", "listed", "bidders", "won"):
        return None
    data = _get_user_rank(lb_type, uid)
    if not data:
        return None
    rank  = data["rank"]
    total = data["total_entries"]
    medal = ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"**#{rank:,}**"
    if lb_type == "sellers":
        stat_s = f"`{_fmt(data['total'])}` earned across `{data['count']:,}` sales"
    elif lb_type == "listed":
        stat_s = f"`{data['count']:,}` auctions listed"
    elif lb_type == "bidders":
        stat_s = f"`{_fmt(data['total'])}` spent across `{data['count']:,}` wins"
    else:
        stat_s = f"`{data['count']:,}` auctions won"
    return f"📍 You are {medal} out of `{total:,}` — {stat_s}"


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — DATA FETCHING
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_user_data(uid: int) -> dict:
    bm  = {"bdr": uid}
    sm  = _seller_match(uid)
    agg = lambda pipe: list(_col.aggregate(pipe))  # noqa: E731

    # ── Bidder ────────────────────────────────────────────────────────────────
    won_res = agg([{"$match": bm}, {"$group": {
        "_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
        "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"},
    }}])
    won = won_res[0] if won_res else {}

    fav_buys      = agg([{"$match": bm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    priciest_buy  = _col.find_one(bm, sort=[("bid", -1)])
    shiny_bought  = agg([{"$match": {**bm, "sh": True, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}])
    gmax_bought   = agg([{"$match": {**bm, "gx": True}},                      {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}])
    natures_b     = agg([{"$match": {**bm, "nat": {"$ne": None}}},             {"$group": {"_id": "$nat", "count": {"$sum": 1}}},                         {"$sort": {"count": -1}}, {"$limit": 3}])
    iv_b_res      = agg([{"$match": {**bm, "iv":  {"$ne": None}}},             {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}])
    iv_bought     = iv_b_res[0] if iv_b_res else {}
    monthly_spent = agg([
        {"$match": {**bm, "ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "spent": {"$sum": "$bid"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": 4},
    ])

    # ── Seller ────────────────────────────────────────────────────────────────
    sold_res = agg([{"$match": sm}, {"$group": {
        "_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
        "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"},
    }}])
    sold = sold_res[0] if sold_res else {}

    fav_sells      = agg([{"$match": sm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    best_sales     = list(_col.find(sm).sort("bid", -1).limit(5))
    shiny_sold     = agg([{"$match": {**sm, "sh": True, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}])
    gmax_sold      = agg([{"$match": {**sm, "gx": True}},                      {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}])
    natures_s      = agg([{"$match": {**sm, "nat": {"$ne": None}}},             {"$group": {"_id": "$nat", "count": {"$sum": 1}}},                         {"$sort": {"count": -1}}, {"$limit": 3}])
    iv_s_res       = agg([{"$match": {**sm, "iv":  {"$ne": None}}},             {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}])
    iv_sold        = iv_s_res[0] if iv_s_res else {}
    monthly_earned = agg([
        {"$match": {**sm, "ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "earned": {"$sum": "$bid"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": 4},
    ])

    return {
        "won": won, "sold": sold,
        "fav_buys": fav_buys, "priciest_buy": priciest_buy,
        "shiny_bought": shiny_bought, "gmax_bought": gmax_bought,
        "natures_bought": natures_b, "iv_bought": iv_bought,
        "monthly_spent": monthly_spent,
        "fav_sells": fav_sells, "best_sales": best_sales,
        "shiny_sold": shiny_sold, "gmax_sold": gmax_sold,
        "natures_sold": natures_s, "iv_sold": iv_sold,
        "monthly_earned": monthly_earned,
    }


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — PAGE RENDERERS  (return list[str] — one string per section)
# ═════════════════════════════════════════════════════════════════════════════

def _page_user_overview(data: dict, user: discord.User | discord.Member) -> list[str]:
    won  = data["won"]
    sold = data["sold"]
    wc   = won.get("count", 0)
    sc   = sold.get("count", 0)

    net     = sold.get("total", 0) - won.get("total", 0)
    net_s   = f"+{_fmt(net)}" if net >= 0 else f"-{_fmt(abs(net))}"
    net_ico = "🟢" if net >= 0 else "🔴"

    pb   = data["priciest_buy"]
    pb_s = f"{shiny_prefix(pb)}`{pb.get('pn','?')}` — `{_fmt(pb.get('bid',0))}`" if pb else "—"
    bs   = data["best_sales"][0] if data["best_sales"] else None
    bs_s = f"{shiny_prefix(bs)}`{bs.get('pn','?')}` — `{_fmt(bs.get('bid',0))}`" if bs else "—"

    # Section 1 — bidder summary
    if wc:
        bidder_block = "\n".join([
            "**💸 As Bidder**",
            f"{REPLY} Auctions Won: `{wc:,}`  •  Total Spent: `{_fmt(won.get('total', 0))}`",
            f"{REPLY} Avg per Win: `{_fmt(won.get('avg', 0))}`  •  Highest: `{_fmt(won.get('max', 0))}`",
            f"{REPLY} Priciest Buy: {pb_s}",
        ])
    else:
        bidder_block = f"**💸 As Bidder**\n{REPLY} _No wins recorded._"

    # Section 2 — seller summary
    if sc:
        seller_block = "\n".join([
            "**🏷️ As Seller**",
            f"{REPLY} Auctions Listed: `{sc:,}`  •  Total Earned: `{_fmt(sold.get('total', 0))}`",
            f"{REPLY} Avg Sale: `{_fmt(sold.get('avg', 0))}`  •  Highest: `{_fmt(sold.get('max', 0))}`",
            f"{REPLY} Best Sale: {bs_s}",
        ])
    else:
        seller_block = f"**🏷️ As Seller**\n{REPLY} _No auctions listed._"

    # Section 3 — net balance
    net_block = f"**⚖️ Net Balance:** {net_ico} `{net_s}` _(earned − spent)_"

    return [bidder_block, seller_block, net_block]


def _page_user_buying(data: dict) -> list[str]:
    if not data["won"].get("count"):
        return ["**💸 Buying Details**\n\n_No auction wins recorded._"]

    fav_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["fav_buys"]) or "—"

    shiny_s = "\n".join(
        f"{REPLY} ✨**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["shiny_bought"]
    ) or f"{REPLY} —"

    gmax_s = "\n".join(
        f"{REPLY} ⚡**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["gmax_bought"]
    ) or f"{REPLY} —"

    nat_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["natures_bought"]) or "—"

    iv   = data["iv_bought"]
    iv_s = f"`{iv.get('avg', 0):.1f}%` avg  •  `{iv.get('max', 0):.2f}%` best" if iv else "—"

    monthly = "\n".join(
        f"{REPLY} `{r['_id']}` — `{_fmt(r['spent'])}` across `{r['count']:,}` wins"
        for r in data["monthly_spent"]
    ) or f"{REPLY} —"

    # Each block becomes its own section with a separator after it
    return [
        f"**Favourite Pokémon Bought**\n{REPLY} {fav_s}",
        f"**Top Shinies Bought**\n{shiny_s}",
        f"**Top Gmaxes Bought**\n{gmax_s}",
        f"**Preferred Natures**\n{REPLY} {nat_s}",
        f"**IV Quality Bought**\n{REPLY} {iv_s}",
        f"**Monthly Spending**\n{monthly}",
    ]


def _page_user_selling(data: dict) -> list[str]:
    if not data["sold"].get("count"):
        return ["**🏷️ Selling Details**\n\n_No auctions listed._"]

    fav_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["fav_sells"]) or "—"

    best_s = "\n".join(
        f"{REPLY} {shiny_prefix(s)}`{s.get('pn', '?')}` — `{_fmt(s.get('bid', 0))}`"
        for s in data["best_sales"]
    ) or f"{REPLY} —"

    shiny_s = "\n".join(
        f"{REPLY} ✨**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["shiny_sold"]
    ) or f"{REPLY} —"

    gmax_s = "\n".join(
        f"{REPLY} ⚡**{x['_id']}** — ×{x['count']} auctions  •  avg `{_fmt(x['avg'])}`"
        for x in data["gmax_sold"]
    ) or f"{REPLY} —"

    nat_s = ", ".join(f"**{x['_id']}** ×{x['count']}" for x in data["natures_sold"]) or "—"

    iv   = data["iv_sold"]
    iv_s = f"`{iv.get('avg', 0):.1f}%` avg  •  `{iv.get('max', 0):.2f}%` best" if iv else "—"

    monthly = "\n".join(
        f"{REPLY} `{r['_id']}` — `{_fmt(r['earned'])}` across `{r['count']:,}` sales"
        for r in data["monthly_earned"]
    ) or f"{REPLY} —"

    # Each block becomes its own section with a separator after it
    return [
        f"**Favourite Pokémon Sold**\n{REPLY} {fav_s}",
        f"**Best Sales**\n{best_s}",
        f"**Top Shinies Sold**\n{shiny_s}",
        f"**Top Gmaxes Sold**\n{gmax_s}",
        f"**Top Natures Sold**\n{REPLY} {nat_s}",
        f"**IV Quality Sold**\n{REPLY} {iv_s}",
        f"**Monthly Earnings**\n{monthly}",
    ]


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_user_stats_view(
    user: discord.User | discord.Member,
    page: str = "overview",
    data: dict | None = None,
) -> discord.ui.LayoutView:

    if data is None:
        data = _fetch_user_data(user.id)

    if not data["won"].get("count") and not data["sold"].get("count"):
        return _error_view(f"❌ No auction activity found for **{user.display_name}**.")

    page_labels = {
        "overview": "👤 Overview",
        "buying":   "💸 Buying Details",
        "selling":  "🏷️ Selling Details",
    }
    page_sections = {
        "overview": _page_user_overview(data, user),
        "buying":   _page_user_buying(data),
        "selling":  _page_user_selling(data),
    }

    label    = page_labels.get(page, "👤 Overview")
    sections = page_sections.get(page, page_sections["overview"])
    date_footer = _get_date_range_footer()

    class PageSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Overview",        value="overview", emoji="👤", description="Summary of all activity"),
                discord.SelectOption(label="Buying Details",  value="buying",   emoji="💸", description="Detailed buying stats"),
                discord.SelectOption(label="Selling Details", value="selling",  emoji="🏷️", description="Detailed selling stats"),
            ]
            for o in options:
                if o.value == page:
                    o.default = True
            super().__init__(placeholder="Switch section…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = _build_user_stats_view(user, self.values[0], data)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in user stats select callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps: list = [
        discord.ui.TextDisplay(content=f"## 📊 {user.display_name} — {label}"),
        _sep(),
    ]
    # Every section gets its own TextDisplay + separator after it
    comps += _interleave_seps(sections, final_sep=True)

    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]
    comps.append(discord.ui.ActionRow(PageSelect()))

    class StatsView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return StatsView()


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — DATA FETCHING
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_market_data() -> dict:
    agg = lambda pipe: list(_col.aggregate(pipe))  # noqa: E731

    total         = _col.count_documents({})
    shiny_count   = _col.count_documents({"sh": True, "gx": {"$ne": True}})
    gmax_count    = _col.count_documents({"gx": True})
    normal_count  = total - shiny_count - gmax_count
    perfect_count = _col.count_documents({"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31})
    zero_count    = _col.count_documents({"hp": 0,  "atk": 0,  "def": 0,  "spa": 0,  "spd": 0,  "spe": 0})

    vol_res   = agg([{"$group": {"_id": None, "total": {"$sum": "$bid"}, "avg": {"$avg": "$bid"}}}])
    total_vol = vol_res[0]["total"] if vol_res else 0
    avg_price = vol_res[0]["avg"]   if vol_res else 0

    iv_res   = agg([{"$match": {"iv": {"$ne": None}}}, {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}])
    iv_stats = iv_res[0] if iv_res else {}

    busiest_res = agg([
        {"$match": {"ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": 1},
    ])
    busiest = busiest_res[0] if busiest_res else {}

    monthly_trend = agg([
        {"$match": {"ts": {"$exists": True}}},
        {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
        {"$group": {"_id": "$month", "count": {"$sum": 1}, "volume": {"$sum": "$bid"}}},
        {"$sort": {"_id": -1}}, {"$limit": 6},
    ])

    top_natures = agg([{"$match": {"nat": {"$ne": None}}}, {"$group": {"_id": "$nat", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}])

    most_overall = agg([{"$group": {"_id": "$pn", "count": {"$sum": 1}}},                                       {"$sort": {"count": -1}}, {"$limit": 5}])
    most_normal  = agg([{"$match": {"sh": {"$ne": True}, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}},                         {"$sort": {"count": -1}}, {"$limit": 5}])
    most_shiny   = agg([{"$match": {"sh": True,          "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 5}])
    most_gmax    = agg([{"$match": {"gx": True}},                               {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 5}])

    avg_normal = agg([{"$match": {"sh": {"$ne": True}, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 5}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])
    avg_shiny  = agg([{"$match": {"sh": True,          "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 3}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])
    avg_gmax   = agg([{"$match": {"gx": True}},                               {"$group": {"_id": "$pn", "avg": {"$avg": "$bid"}, "count": {"$sum": 1}}}, {"$match": {"count": {"$gte": 3}}}, {"$sort": {"avg": -1}}, {"$limit": 5}])

    big_overall = list(_col.find({},                                 {"pn": 1, "bid": 1, "sh": 1, "gx": 1}).sort("bid", -1).limit(5))
    big_shiny   = list(_col.find({"sh": True, "gx": {"$ne": True}}, {"pn": 1, "bid": 1, "sh": 1}).sort("bid", -1).limit(5))
    big_gmax    = list(_col.find({"gx": True},                       {"pn": 1, "bid": 1, "gx": 1}).sort("bid", -1).limit(5))
    rarest      = agg([{"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": 1}}, {"$limit": 5}])

    return {
        "total": total, "shiny_count": shiny_count, "gmax_count": gmax_count,
        "normal_count": normal_count, "perfect_count": perfect_count, "zero_count": zero_count,
        "total_vol": total_vol, "avg_price": avg_price, "iv_stats": iv_stats,
        "busiest": busiest, "monthly_trend": monthly_trend, "top_natures": top_natures,
        "most_overall": most_overall, "most_normal": most_normal,
        "most_shiny": most_shiny, "most_gmax": most_gmax,
        "avg_normal": avg_normal, "avg_shiny": avg_shiny, "avg_gmax": avg_gmax,
        "big_overall": big_overall, "big_shiny": big_shiny, "big_gmax": big_gmax,
        "rarest": rarest,
    }


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — FORMATTING HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _trade_lines(rows: list, show_avg: bool = False) -> str:
    lines = []
    for i, r in enumerate(rows):
        line = f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions"
        if show_avg and r.get("avg"):
            line += f"  •  avg `{_fmt(r['avg'])}`"
        lines.append(line)
    return "\n".join(lines) or "_No data_"


def _avg_lines(rows: list) -> str:
    return "\n".join(
        f"{_medal(i)} **{r['_id']}** — avg `{_fmt(r['avg'])}` over `{r['count']:,}` sales"
        for i, r in enumerate(rows)
    ) or "_No data_"


def _big_lines(rows: list) -> str:
    return "\n".join(
        f"{_medal(i)} {shiny_prefix(r)}**{r.get('pn', '?')}** — `{_fmt(r.get('bid', 0))}`"
        for i, r in enumerate(rows)
    ) or "_No data_"


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — PAGE RENDERERS  (return list[str] — one string per section)
# ═════════════════════════════════════════════════════════════════════════════

def _page_market_overview(d: dict) -> list[str]:
    iv    = d["iv_stats"]
    busy  = d["busiest"]

    overview_lines = [
        "**📈 Overview**",
        f"{REPLY} Total Auctions: `{d['total']:,}`  •  Total Volume: `{_fmt(d['total_vol'])}`",
        f"{REPLY} Avg Sale Price: `{_fmt(d['avg_price'])}`",
        f"{REPLY} Normal: `{d['normal_count']:,}`  •  Shiny: `{d['shiny_count']:,}`  •  Gmax: `{d['gmax_count']:,}`",
        f"{REPLY} Busiest Month: `{busy.get('_id', '?')}` — `{busy.get('count', 0):,}` auctions",
        f"{REPLY} Perfect IVs (6×31): `{d['perfect_count']:,}`  •  Zero IVs (6×0): `{d['zero_count']:,}`",
    ]
    if iv:
        overview_lines.append(f"{REPLY} Avg IV %: `{iv.get('avg', 0):.1f}%`  •  Highest Ever: `{iv.get('max', 0):.2f}%`")

    trend_lines = ["**📅 Monthly Trend** _(most recent first)_"]
    trend_lines += [
        f"{REPLY} `{r['_id']}` — `{r['count']:,}` auctions  •  vol `{_fmt(r['volume'])}`"
        for r in d["monthly_trend"]
    ] or [f"{REPLY} —"]

    nats = "  ".join(f"**{r['_id']}** `{r['count']:,}`" for r in d["top_natures"]) or "—"

    return [
        "\n".join(overview_lines),
        "\n".join(trend_lines),
        f"**🌿 Most Common Natures**\n{REPLY} {nats}",
    ]


def _page_market_traded(d: dict) -> list[str]:
    return [
        f"**🔥 Most Traded — Overall**\n\n{_trade_lines(d['most_overall'])}",
        f"**🔵 Most Traded — Normal**\n\n{_trade_lines(d['most_normal'])}",
        f"**✨ Most Traded — Shiny**\n\n{_trade_lines(d['most_shiny'], show_avg=True)}",
        f"**⚡ Most Traded — Gigantamax**\n\n{_trade_lines(d['most_gmax'], show_avg=True)}",
    ]


def _page_market_prices(d: dict) -> list[str]:
    # Every subsection is its own entry → gets its own separator in the view
    return [
        f"**💎 Priciest on Average — Normal** _(min 5 sales)_\n\n{_avg_lines(d['avg_normal'])}",
        f"**💎 Priciest on Average — Shiny** _(min 3 sales)_\n\n{_avg_lines(d['avg_shiny'])}",
        f"**💎 Priciest on Average — Gmax** _(min 3 sales)_\n\n{_avg_lines(d['avg_gmax'])}",
        # Each "Biggest Single Sales" block is its own section too
        f"**💰 Biggest Single Sales — Overall**\n\n{_big_lines(d['big_overall'])}",
        f"**💰 Biggest Single Sales — Shiny**\n\n{_big_lines(d['big_shiny'])}",
        f"**💰 Biggest Single Sales — Gmax**\n\n{_big_lines(d['big_gmax'])}",
    ]


def _page_market_rarity(d: dict) -> list[str]:
    return [f"**🌙 Rarest Pokémon** _(fewest total sales)_\n\n{_trade_lines(d['rarest'])}"]


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INSIGHTS — VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_market_view(page: str = "overview", data: dict | None = None) -> discord.ui.LayoutView:
    if data is None:
        data = _fetch_market_data()

    pages = {
        "overview": ("📈 Overview & Trends",    _page_market_overview(data)),
        "traded":   ("🔥 Most Traded Pokémon",  _page_market_traded(data)),
        "prices":   ("💎 Prices & Big Sales",   _page_market_prices(data)),
        "rarity":   ("🌙 Rarity",               _page_market_rarity(data)),
    }
    title, sections = pages.get(page, pages["overview"])
    date_footer = _get_date_range_footer()

    class PageSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Overview & Trends",   value="overview", emoji="📈", description="Volume, breakdown, monthly trend"),
                discord.SelectOption(label="Most Traded Pokémon", value="traded",   emoji="🔥", description="Top traded overall/normal/shiny/gmax"),
                discord.SelectOption(label="Prices & Big Sales",  value="prices",   emoji="💎", description="Avg prices and record sales"),
                discord.SelectOption(label="Rarity",              value="rarity",   emoji="🌙", description="Rarest Pokémon by sale count"),
            ]
            for o in options:
                if o.value == page:
                    o.default = True
            super().__init__(placeholder="Switch section…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                new_view = _build_market_view(self.values[0], data)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in market select callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps: list = [
        discord.ui.TextDisplay(content=f"## 🌐 Auction Insights — {title}"),
        _sep(),
    ]
    comps += _interleave_seps(sections, final_sep=True)

    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]
    comps.append(discord.ui.ActionRow(PageSelect()))

    class MarketView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return MarketView()


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD BODY
# ═════════════════════════════════════════════════════════════════════════════

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
        rows  = _seller_leaderboard("total")
        lines = [f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{_fmt(r['total'])}` from `{r['count']:,}` sales" for i, r in enumerate(rows)]

    elif lb_type == "listed":
        rows  = _seller_leaderboard("count")
        lines = [f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{r['count']:,}` auctions" for i, r in enumerate(rows)]

    elif lb_type == "bidders":
        rows = list(_col.aggregate([
            {"$match": {"bdr": {"$exists": True}}},
            {"$group": {"_id": "$bdr", "total": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"total": -1}}, {"$limit": LB_SIZE},
        ]))
        lines = [f"{_medal(i)} <@{r['_id']}> — `{_fmt(r['total'])}` across `{r['count']:,}` wins" for i, r in enumerate(rows)]

    elif lb_type == "won":
        rows = list(_col.aggregate([
            {"$match": {"bdr": {"$exists": True}}},
            {"$group": {"_id": "$bdr", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}, {"$limit": LB_SIZE},
        ]))
        lines = [f"{_medal(i)} <@{r['_id']}> — `{r['count']:,}` wins" for i, r in enumerate(rows)]

    elif lb_type == "pokemon":
        match_filter: dict = {}
        if variant == "shiny":
            match_filter = {"sh": True, "gx": {"$ne": True}}
        elif variant == "gmax":
            match_filter = {"gx": True}
        elif variant == "normal":
            match_filter = {"sh": {"$ne": True}, "gx": {"$ne": True}}
        rows = list(_col.aggregate([
            {"$match": match_filter},
            {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}},
            {"$sort": {"count": -1}}, {"$limit": LB_SIZE},
        ]))
        lines = [f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions  •  avg `{_fmt(r['avg'])}`" for i, r in enumerate(rows)]

    elif lb_type == "expensive":
        rows = list(_col.find({}, {"aid": 1, "pn": 1, "bid": 1, "sn": 1, "sid": 1, "bdr": 1, "sh": 1, "gx": 1}).sort("bid", -1).limit(LB_SIZE))
        for i, r in enumerate(rows):
            seller_s = _fmt_user(r["sid"], r.get("sn")) if r.get("sid") else f"`{r.get('sn', '?')}`"
            bidder_s = f"<@{r['bdr']}>" if r.get("bdr") else "Unknown"
            lines.append(
                f"{_medal(i)} {shiny_prefix(r)}**{r.get('pn', '?')}** — `{_fmt(r.get('bid', 0))}`\n"
                f"　Sold by {seller_s} → {bidder_s}  •  `#{r.get('aid', '?')}`"
            )
    else:
        return "❌ Unknown leaderboard type."

    if not rows:
        return "❌ No data found for this leaderboard."

    body = "\n".join(lines)
    if caller_id is not None:
        footer = _rank_footer(lb_type, caller_id)
        if footer:
            body += f"\n\n{footer}"
    return body


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_lb_view(
    current_type:    str = "pokemon",
    current_variant: str = "shiny",
    caller_id:       int | None = None,
) -> discord.ui.LayoutView:

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
                cid = interaction.user.id
                lb_t, lb_v = ("pokemon", val.split("_", 1)[1]) if val.startswith("pokemon_") else (val, "overall")
                new_view = _build_lb_view(lb_t, lb_v, caller_id=cid)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in leaderboard select callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps = [
        discord.ui.TextDisplay(content=f"## {title}"),
        _sep(),
        discord.ui.TextDisplay(content=body),
        _sep(),
    ]
    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]
    comps.append(discord.ui.ActionRow(TypeSelect()))

    class LbView(discord.ui.LayoutView):
        container = discord.ui.Container(*comps, accent_colour=config.EMBED_COLOR)
        def __init__(self):
            super().__init__(timeout=300)

    return LbView()


# ═════════════════════════════════════════════════════════════════════════════
# HELP VIEW
# ═════════════════════════════════════════════════════════════════════════════

def _build_help_view() -> discord.ui.LayoutView:
    text = "\n".join([
        "**Commands:**",
        f"{REPLY} `j!stats [@user]` — auction stats for yourself or another user",
        f"{REPLY} `j!lb sellers` — top sellers by money earned",
        f"{REPLY} `j!lb bidders` — top bidders by money spent",
        f"{REPLY} `j!lb listed` — most auctions listed",
        f"{REPLY} `j!lb won` — most auctions won",
        f"{REPLY} `j!lb pokemon [normal|shiny|gmax|overall]` — most auctioned Pokémon",
        f"{REPLY} `j!lb expensive` — biggest single sales ever",
        f"{REPLY} `j!market` — server-wide market insights",
        "",
        "**Examples:**",
        f"{REPLY} `j!stats @user`",
        f"{REPLY} `j!lb pokemon shiny`",
        f"{REPLY} `j!lb sellers`",
        f"{REPLY} `j!market`",
    ])

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


# ═════════════════════════════════════════════════════════════════════════════
# COG
# ═════════════════════════════════════════════════════════════════════════════

class Stats(commands.Cog):
    """Auction statistics and leaderboards"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── j!stats ───────────────────────────────────────────────────────────────
    @commands.hybrid_command(name="auction_stats", aliases=["stats"])
    @app_commands.describe(user="User to look up (leave empty for yourself)")
    async def stats_cmd(
        self,
        ctx: commands.Context,
        user: discord.Member | discord.User | None = None,
    ):
        """Show detailed auction stats for a user"""
        target = user or ctx.author
        async with ctx.typing():
            view = _build_user_stats_view(target)
        await ctx.reply(view=view, mention_author=False)

    # ── j!lb ──────────────────────────────────────────────────────────────────
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
            await ctx.reply(view=_build_help_view(), mention_author=False)
            return

        if variant not in {"normal", "shiny", "gmax", "overall"}:
            variant = "overall"

        async with ctx.typing():
            view = _build_lb_view(lb_type, variant, caller_id=ctx.author.id)
        await ctx.reply(view=view, mention_author=False, allowed_mentions=SAFE_MENTIONS)

    # ── j!market ──────────────────────────────────────────────────────────────
    @commands.hybrid_command(name="auction_insights", aliases=["market", "ai"])
    async def market_cmd(self, ctx: commands.Context):
        """poketwo auction insights"""
        async with ctx.typing():
            view = _build_market_view()
        await ctx.reply(view=view, mention_author=False)


# ═════════════════════════════════════════════════════════════════════════════
# SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
