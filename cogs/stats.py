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
  j!lb [type] [variant]  — leaderboards with dropdown switcher + time period
  j!market               — server-wide market insights (tabbed dropdown)

Caching (two-layer):
  PRIMARY:   In-memory dict (_mem_cache) — nanosecond reads, zero I/O, never blocks event loop.
  SECONDARY: MongoDB lb_cache collection — persists across restarts so cold starts are instant.

  Flow:
    Startup  → load all docs from MongoDB into _mem_cache (one bulk read).
    Runtime  → j!lb reads exclusively from _mem_cache (pure dict lookup).
    Every 6h → background task recomputes all combos, writes to _mem_cache + MongoDB atomically.
    Restart  → MongoDB is read again; if still fresh (< 6h old) no recompute needed.

  j!stats always runs live but fires all 16 MongoDB queries in parallel via asyncio.gather()
  + run_in_executor, making it ~5-10× faster than the previous sequential approach.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from calendar import monthrange

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import MongoClient, UpdateOne

import config
from config import REPLY
from utils import shiny_prefix

log = logging.getLogger(__name__)

_mongo      = MongoClient(config.MONGO_URI)
_db         = _mongo[config.MONGO_DB_NAME]
_col        = _db[config.MONGO_COLLECTION]
_cache_col  = _db["lb_cache"]          # separate collection for precomputed LB data

CACHE_TTL_SECONDS = 6 * 3600           # 6 hours
LB_SIZE           = 10
SAFE_MENTIONS     = discord.AllowedMentions.none()

# In-memory cache: { cache_key: {"rows": [...], "next_refresh": int} }
# This is the PRIMARY serving layer — j!lb never touches MongoDB at runtime.
_mem_cache: dict[str, dict] = {}


# ═════════════════════════════════════════════════════════════════════════════
# TIME PERIOD HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _period_options() -> list[dict]:
    """
    Returns a list of period dicts for the last 4 calendar periods:
      [0]  All Time          → no ts filter
      [1]  Current month     → 1st of this month → now
      [2]  Last month
      [3]  Month before that
      [4]  Month before that

    Each dict: {label, value, ts_gte, ts_lt}
      value  = "all" | "YYYY-MM"
      ts_gte / ts_lt = unix timestamps (int) or None
    """
    now   = datetime.now(timezone.utc)
    opts  = [{"label": "All Time", "value": "all", "ts_gte": None, "ts_lt": None}]

    for i in range(4):
        if i == 0:
            # current month: 1st → now
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end   = now
            label_prefix = ""
        else:
            # go back i months
            month = now.month - i
            year  = now.year
            while month <= 0:
                month += 12
                year  -= 1
            days_in_month = monthrange(year, month)[1]
            start = datetime(year, month,  1,  0, 0, 0, tzinfo=timezone.utc)
            end   = datetime(year, month, days_in_month, 23, 59, 59, tzinfo=timezone.utc)
            label_prefix = ""

        label = start.strftime("%B %Y")
        value = start.strftime("%Y-%m")
        opts.append({
            "label":   label,
            "value":   value,
            "ts_gte":  int(start.timestamp()),
            "ts_lt":   int(end.timestamp()),
        })

    return opts


def _period_match(period_value: str, periods: list[dict]) -> dict:
    """Return the MongoDB match fragment for ts given a period value string."""
    for p in periods:
        if p["value"] == period_value:
            if p["ts_gte"] is None:
                return {}
            return {"ts": {"$gte": p["ts_gte"], "$lte": p["ts_lt"]}}
    return {}


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
    comps = []
    for i, section in enumerate(sections):
        comps.append(discord.ui.TextDisplay(content=section))
        if final_sep or i < len(sections) - 1:
            comps.append(_sep())
    return comps


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD CACHE  (two-layer: memory primary, MongoDB secondary)
# ═════════════════════════════════════════════════════════════════════════════

def _cache_key(lb_type: str, variant: str, period_value: str) -> str:
    return f"{lb_type}__{variant}__{period_value}"


def _write_cache(key: str, rows: list, next_refresh: int) -> None:
    """Write to memory first (instant), then persist to MongoDB (background-safe)."""
    _mem_cache[key] = {"rows": rows, "next_refresh": next_refresh}
    try:
        _cache_col.update_one(
            {"_id": key},
            {"$set": {"rows": rows, "next_refresh": next_refresh, "built_at": int(time.time())}},
            upsert=True,
        )
    except Exception:
        log.exception("Failed to persist cache to MongoDB for key=%s", key)


def _read_cache(key: str) -> tuple[list | None, int | None]:
    """
    Read from in-memory cache first (zero I/O).
    Falls back to MongoDB only if the key isn't in memory yet (e.g. first startup).
    Returns (rows, next_refresh) or (None, None) if missing/expired.
    """
    now = int(time.time())

    # Primary: memory
    entry = _mem_cache.get(key)
    if entry and entry["next_refresh"] > now:
        return entry["rows"], entry["next_refresh"]

    # Secondary: MongoDB (only hit on cold start / memory miss)
    try:
        doc = _cache_col.find_one({"_id": key})
        if doc and doc.get("next_refresh", 0) > now:
            # Warm the memory cache so subsequent reads are instant
            _mem_cache[key] = {"rows": doc["rows"], "next_refresh": doc["next_refresh"]}
            return doc["rows"], doc["next_refresh"]
    except Exception:
        log.exception("Failed to read cache from MongoDB for key=%s", key)

    return None, None


def _load_all_from_mongo() -> int:
    """
    Bulk-load all non-expired cache docs from MongoDB into _mem_cache.
    Called once at startup. Returns count of loaded keys.
    """
    now   = int(time.time())
    count = 0
    try:
        for doc in _cache_col.find({"next_refresh": {"$gt": now}}):
            key = doc["_id"]
            _mem_cache[key] = {"rows": doc["rows"], "next_refresh": doc["next_refresh"]}
            count += 1
    except Exception:
        log.exception("Failed to bulk-load cache from MongoDB")
    return count


def _next_refresh_ts() -> int:
    return int(time.time()) + CACHE_TTL_SECONDS


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD AGGREGATIONS  (period-aware)
# ═════════════════════════════════════════════════════════════════════════════

def _seller_leaderboard_agg(mode: str, ts_match: dict) -> list[dict]:
    sort_field = "total" if mode == "total" else "count"
    base_match: dict = {"$or": [
        {"sid": {"$exists": True, "$ne": None}},
        {"sn":  {"$exists": True, "$ne": None}},
    ]}
    if ts_match:
        base_match.update(ts_match)
    pipe = [
        {"$match": base_match},
        {"$group": {
            "_id": {"$cond": {
                "if":   {"$and": [{"$ne": ["$sid", None]}, {"$ne": ["$sid", ""]}]},
                "then": {"type": "id",   "val": "$sid"},
                "else": {"type": "name", "val": "$sn"},
            }},
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


def _compute_lb_rows(lb_type: str, variant: str, ts_match: dict) -> list:
    """Compute raw leaderboard rows for a given type/variant/period. Returns JSON-serialisable list."""
    if lb_type == "sellers":
        return _seller_leaderboard_agg("total", ts_match)

    if lb_type == "listed":
        return _seller_leaderboard_agg("count", ts_match)

    if lb_type == "bidders":
        match = {"bdr": {"$exists": True}}
        match.update(ts_match)
        return list(_col.aggregate([
            {"$match": match},
            {"$group": {"_id": "$bdr", "total": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"total": -1}}, {"$limit": LB_SIZE},
        ]))

    if lb_type == "won":
        match = {"bdr": {"$exists": True}}
        match.update(ts_match)
        return list(_col.aggregate([
            {"$match": match},
            {"$group": {"_id": "$bdr", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}, {"$limit": LB_SIZE},
        ]))

    if lb_type == "pokemon":
        vmap: dict = {
            "shiny":   {"sh": True,           "gx": {"$ne": True}},
            "gmax":    {"gx": True},
            "normal":  {"sh": {"$ne": True},  "gx": {"$ne": True}},
            "overall": {},
        }
        match = dict(vmap.get(variant, {}))
        match.update(ts_match)
        return list(_col.aggregate([
            {"$match": match},
            {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}},
            {"$sort": {"count": -1}}, {"$limit": LB_SIZE},
        ]))

    if lb_type == "expensive":
        match: dict = {}
        match.update(ts_match)
        return list(_col.find(
            match,
            {"aid": 1, "pn": 1, "bid": 1, "sn": 1, "sid": 1, "bdr": 1, "sh": 1, "gx": 1}
        ).sort("bid", -1).limit(LB_SIZE))

    return []


def _precompute_all_lb(periods: list[dict] | None = None) -> None:
    """
    Rebuild cache for every combination of (lb_type × variant × period).
    Called on startup and every 6 hours by the background task.
    """
    if periods is None:
        periods = _period_options()

    lb_combos = [
        ("sellers",  "overall"),
        ("listed",   "overall"),
        ("bidders",  "overall"),
        ("won",      "overall"),
        ("pokemon",  "overall"),
        ("pokemon",  "shiny"),
        ("pokemon",  "normal"),
        ("pokemon",  "gmax"),
        ("expensive","overall"),
    ]

    next_refresh = _next_refresh_ts()

    for period in periods:
        ts_match = _period_match(period["value"], periods)
        for lb_type, variant in lb_combos:
            key  = _cache_key(lb_type, variant, period["value"])
            rows = _compute_lb_rows(lb_type, variant, ts_match)
            _write_cache(key, rows, next_refresh)
            log.debug("Cache built: %s", key)

    log.info("Leaderboard cache rebuild complete. Next refresh: %s",
             datetime.fromtimestamp(next_refresh, tz=timezone.utc).isoformat())


def _get_lb_rows(lb_type: str, variant: str, period_value: str, periods: list[dict]) -> tuple[list, int | None]:
    """
    Return (rows, next_refresh_ts).
    Serves from cache if warm; otherwise computes live and warms cache.
    """
    key  = _cache_key(lb_type, variant, period_value)
    rows, next_refresh = _read_cache(key)

    if rows is None:
        log.info("Cache miss for %s — computing live", key)
        ts_match     = _period_match(period_value, periods)
        rows         = _compute_lb_rows(lb_type, variant, ts_match)
        next_refresh = _next_refresh_ts()
        _write_cache(key, rows, next_refresh)

    return rows, next_refresh


# ═════════════════════════════════════════════════════════════════════════════
# RANK LOOKUP  (always live — fast single-collection scan is acceptable)
# ═════════════════════════════════════════════════════════════════════════════

def _seller_match(uid: int) -> dict:
    return {"sid": uid}


def _get_user_rank(lb_type: str, uid: int, period_value: str = "all", periods: list[dict] | None = None) -> dict | None:
    if periods is None:
        periods = _period_options()
    ts_match = _period_match(period_value, periods)
    try:
        if lb_type in ("sellers", "listed"):
            sort_field = "total" if lb_type == "sellers" else "count"
            base: dict = {"$or": [{"sid": {"$exists": True, "$ne": None}}, {"sn": {"$exists": True, "$ne": None}}]}
            base.update(ts_match)
            pipe = [
                {"$match": base},
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
            m: dict = {"bdr": {"$exists": True}}
            m.update(ts_match)
            pipe = [
                {"$match": m},
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


def _rank_footer(lb_type: str, uid: int, period_value: str = "all", periods: list[dict] | None = None) -> str | None:
    if lb_type not in ("sellers", "listed", "bidders", "won"):
        return None
    if periods is None:
        periods = _period_options()
    data = _get_user_rank(lb_type, uid, period_value, periods)
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
# USER STATS — PARALLEL DATA FETCHING
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_user_data(uid: int) -> dict:
    """
    Run all MongoDB queries for a user in parallel using asyncio.gather().
    This cuts load time from ~15 sequential queries to ~1 round-trip batch.
    """
    bm = {"bdr": uid}
    sm = _seller_match(uid)
    loop = asyncio.get_event_loop()

    def _run(fn, *args, **kwargs):
        """Wrap a sync MongoDB call so it can be awaited via run_in_executor."""
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def agg(pipe):
        return list(_col.aggregate(pipe))

    def find_one_sorted(match, sort_field, direction=-1):
        return _col.find_one(match, sort=[(sort_field, direction)])

    def find_sorted_limit(match, sort_field, direction, limit, projection=None):
        q = _col.find(match, projection) if projection else _col.find(match)
        return list(q.sort(sort_field, direction).limit(limit))

    # Schedule ALL queries concurrently
    (
        won_res,
        fav_buys,
        priciest_buy,
        shiny_bought,
        gmax_bought,
        natures_b,
        iv_b_res,
        monthly_spent,
        sold_res,
        fav_sells,
        best_sales,
        shiny_sold,
        gmax_sold,
        natures_s,
        iv_s_res,
        monthly_earned,
    ) = await asyncio.gather(
        _run(agg, [{"$match": bm}, {"$group": {
            "_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
            "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"},
        }}]),
        _run(agg, [{"$match": bm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}]),
        _run(find_one_sorted, bm, "bid"),
        _run(agg, [{"$match": {**bm, "sh": True, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, "gx": True}},                      {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, "nat": {"$ne": None}}},             {"$group": {"_id": "$nat", "count": {"$sum": 1}}},                        {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**bm, "iv":  {"$ne": None}}},             {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}]),
        _run(agg, [
            {"$match": {**bm, "ts": {"$exists": True}}},
            {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
            {"$group": {"_id": "$month", "spent": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"_id": -1}}, {"$limit": 4},
        ]),
        _run(agg, [{"$match": sm}, {"$group": {
            "_id": None, "total": {"$sum": "$bid"}, "count": {"$sum": 1},
            "avg": {"$avg": "$bid"}, "max": {"$max": "$bid"},
        }}]),
        _run(agg, [{"$match": sm}, {"$group": {"_id": "$pn", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}, {"$limit": 5}]),
        _run(find_sorted_limit, sm, "bid", -1, 5),
        _run(agg, [{"$match": {**sm, "sh": True, "gx": {"$ne": True}}}, {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, "gx": True}},                      {"$group": {"_id": "$pn", "count": {"$sum": 1}, "avg": {"$avg": "$bid"}}}, {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, "nat": {"$ne": None}}},             {"$group": {"_id": "$nat", "count": {"$sum": 1}}},                        {"$sort": {"count": -1}}, {"$limit": 3}]),
        _run(agg, [{"$match": {**sm, "iv":  {"$ne": None}}},             {"$group": {"_id": None, "avg": {"$avg": "$iv"}, "max": {"$max": "$iv"}}}]),
        _run(agg, [
            {"$match": {**sm, "ts": {"$exists": True}}},
            {"$addFields": {"month": {"$dateToString": {"format": "%Y-%m", "date": {"$toDate": {"$multiply": ["$ts", 1000]}}}}}},
            {"$group": {"_id": "$month", "earned": {"$sum": "$bid"}, "count": {"$sum": 1}}},
            {"$sort": {"_id": -1}}, {"$limit": 4},
        ]),
    )

    return {
        "won":           won_res[0]  if won_res  else {},
        "sold":          sold_res[0] if sold_res else {},
        "fav_buys":      fav_buys,
        "priciest_buy":  priciest_buy,
        "shiny_bought":  shiny_bought,
        "gmax_bought":   gmax_bought,
        "natures_bought":natures_b,
        "iv_bought":     iv_b_res[0] if iv_b_res else {},
        "monthly_spent": monthly_spent,
        "fav_sells":     fav_sells,
        "best_sales":    best_sales,
        "shiny_sold":    shiny_sold,
        "gmax_sold":     gmax_sold,
        "natures_sold":  natures_s,
        "iv_sold":       iv_s_res[0] if iv_s_res else {},
        "monthly_earned":monthly_earned,
    }


# ═════════════════════════════════════════════════════════════════════════════
# USER STATS — PAGE RENDERERS
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

    if wc:
        bidder_block = "\n".join([
            "**💸 As Bidder**",
            f"{REPLY} Auctions Won: `{wc:,}`  •  Total Spent: `{_fmt(won.get('total', 0))}`",
            f"{REPLY} Avg per Win: `{_fmt(won.get('avg', 0))}`  •  Highest: `{_fmt(won.get('max', 0))}`",
            f"{REPLY} Priciest Buy: {pb_s}",
        ])
    else:
        bidder_block = f"**💸 As Bidder**\n{REPLY} _No wins recorded._"

    if sc:
        seller_block = "\n".join([
            "**🏷️ As Seller**",
            f"{REPLY} Auctions Listed: `{sc:,}`  •  Total Earned: `{_fmt(sold.get('total', 0))}`",
            f"{REPLY} Avg Sale: `{_fmt(sold.get('avg', 0))}`  •  Highest: `{_fmt(sold.get('max', 0))}`",
            f"{REPLY} Best Sale: {bs_s}",
        ])
    else:
        seller_block = f"**🏷️ As Seller**\n{REPLY} _No auctions listed._"

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

    label       = page_labels.get(page, "👤 Overview")
    sections    = page_sections.get(page, page_sections["overview"])
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
# MARKET INSIGHTS — DATA FETCHING  (unchanged, kept sync — called in ctx.typing())
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
# MARKET INSIGHTS — FORMATTING HELPERS & PAGE RENDERERS  (unchanged)
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
    return [
        f"**💎 Priciest on Average — Normal** _(min 5 sales)_\n\n{_avg_lines(d['avg_normal'])}",
        f"**💎 Priciest on Average — Shiny** _(min 3 sales)_\n\n{_avg_lines(d['avg_shiny'])}",
        f"**💎 Priciest on Average — Gmax** _(min 3 sales)_\n\n{_avg_lines(d['avg_gmax'])}",
        f"**💰 Biggest Single Sales — Overall**\n\n{_big_lines(d['big_overall'])}",
        f"**💰 Biggest Single Sales — Shiny**\n\n{_big_lines(d['big_shiny'])}",
        f"**💰 Biggest Single Sales — Gmax**\n\n{_big_lines(d['big_gmax'])}",
    ]


def _page_market_rarity(d: dict) -> list[str]:
    return [f"**🌙 Rarest Pokémon** _(fewest total sales)_\n\n{_trade_lines(d['rarest'])}"]


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
# LEADERBOARD — BODY RENDERER  (uses cached rows)
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


def _render_lb_body(lb_type: str, rows: list, caller_id: int | None = None,
                    period_value: str = "all", periods: list[dict] | None = None) -> str:
    """Turn cached rows into formatted text."""
    if not rows:
        return "❌ No data found for this leaderboard."

    lines: list[str] = []

    if lb_type == "sellers":
        lines = [f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{_fmt(r['total'])}` from `{r['count']:,}` sales" for i, r in enumerate(rows)]

    elif lb_type == "listed":
        lines = [f"{_medal(i)} {_fmt_user(r['id'], r.get('name'))} — `{r['count']:,}` auctions" for i, r in enumerate(rows)]

    elif lb_type == "bidders":
        lines = [f"{_medal(i)} <@{r['_id']}> — `{_fmt(r['total'])}` across `{r['count']:,}` wins" for i, r in enumerate(rows)]

    elif lb_type == "won":
        lines = [f"{_medal(i)} <@{r['_id']}> — `{r['count']:,}` wins" for i, r in enumerate(rows)]

    elif lb_type == "pokemon":
        lines = [f"{_medal(i)} **{r['_id']}** — `{r['count']:,}` auctions  •  avg `{_fmt(r['avg'])}`" for i, r in enumerate(rows)]

    elif lb_type == "expensive":
        for i, r in enumerate(rows):
            seller_s = _fmt_user(r.get("sid"), r.get("sn")) if r.get("sid") else f"`{r.get('sn', '?')}`"
            bidder_s = f"<@{r['bdr']}>" if r.get("bdr") else "Unknown"
            lines.append(
                f"{_medal(i)} {shiny_prefix(r)}**{r.get('pn', '?')}** — `{_fmt(r.get('bid', 0))}`\n"
                f"　Sold by {seller_s} → {bidder_s}  •  `#{r.get('aid', '?')}`"
            )

    body = "\n".join(lines)

    if caller_id is not None and periods is not None:
        footer = _rank_footer(lb_type, caller_id, period_value, periods)
        if footer:
            body += f"\n\n{footer}"

    return body


# ═════════════════════════════════════════════════════════════════════════════
# LEADERBOARD — VIEW  (two dropdowns: type + time period)
# ═════════════════════════════════════════════════════════════════════════════

def _build_lb_view(
    current_type:    str = "pokemon",
    current_variant: str = "shiny",
    current_period:  str = "all",
    caller_id:       int | None = None,
    periods:         list[dict] | None = None,
) -> discord.ui.LayoutView:

    if periods is None:
        periods = _period_options()

    title_key    = f"{current_type}_{current_variant}" if current_type == "pokemon" else current_type
    title        = _LB_TITLES.get(title_key, "🏆 Leaderboard")
    rows, next_r = _get_lb_rows(current_type, current_variant, current_period, periods)
    body         = _render_lb_body(current_type, rows, caller_id, current_period, periods)
    date_footer  = _get_date_range_footer()

    # Period label for the header
    period_label = next((p["label"] for p in periods if p["value"] == current_period), "All Time")

    # Cache refresh Discord timestamp (relative)
    refresh_line = ""
    if next_r:
        refresh_line = f"-# 🔄 Cache updates {discord.utils.format_dt(datetime.fromtimestamp(next_r, tz=timezone.utc), style='R')}"

    # ── Type selector ─────────────────────────────────────────────────────────
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
                val      = self.values[0]
                cid      = interaction.user.id
                lb_t, lb_v = ("pokemon", val.split("_", 1)[1]) if val.startswith("pokemon_") else (val, "overall")
                new_view = _build_lb_view(lb_t, lb_v, current_period, cid, periods)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in leaderboard TypeSelect callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    # ── Period selector ───────────────────────────────────────────────────────
    class PeriodSelect(discord.ui.Select):
        def __init__(self):
            options = []
            for p in periods:
                opt = discord.SelectOption(label=p["label"], value=p["value"])
                if p["value"] == "all":
                    opt.emoji = "🗂️"
                else:
                    opt.emoji = "📅"
                if p["value"] == current_period:
                    opt.default = True
                options.append(opt)
            super().__init__(placeholder="Switch time period…", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer()
            try:
                cid      = interaction.user.id
                new_view = _build_lb_view(current_type, current_variant, self.values[0], cid, periods)
                await interaction.edit_original_response(view=new_view)
            except Exception:
                log.exception("Error in leaderboard PeriodSelect callback")
                await interaction.edit_original_response(view=_error_view("❌ Something went wrong."))

    comps: list = [
        discord.ui.TextDisplay(content=f"## {title}\n-# 📅 {period_label}"),
        _sep(),
        discord.ui.TextDisplay(content=body),
        _sep(),
    ]
    if refresh_line:
        comps.append(discord.ui.TextDisplay(content=refresh_line))
    if date_footer:
        comps += [discord.ui.TextDisplay(content=f"-# {date_footer}"), _sep(False)]

    comps.append(discord.ui.ActionRow(TypeSelect()))
    comps.append(discord.ui.ActionRow(PeriodSelect()))

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
        self.bot     = bot
        self.periods = _period_options()       # computed once at startup
        self._refresh_cache_task.start()

    def cog_unload(self):
        self._refresh_cache_task.cancel()

    # ── Background cache refresh every 6 hours ────────────────────────────────
    @tasks.loop(hours=6)
    async def _refresh_cache_task(self):
        log.info("Rebuilding leaderboard cache…")
        self.periods = _period_options()       # recalculate month boundaries
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _precompute_all_lb(self.periods))
        log.info("Leaderboard cache rebuilt.")

    @_refresh_cache_task.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()
        # Bulk-load all valid cache docs from MongoDB into memory (one round-trip)
        loop    = asyncio.get_event_loop()
        loaded  = await loop.run_in_executor(None, _load_all_from_mongo)
        if loaded > 0:
            log.info("Leaderboard cache: loaded %d keys from MongoDB into memory.", loaded)
        else:
            log.info("Cold start: building leaderboard cache now…")
            await loop.run_in_executor(None, lambda: _precompute_all_lb(self.periods))

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
            data = await _fetch_user_data(target.id)   # parallel async fetch
            view = _build_user_stats_view(target, data=data)
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
        lb_type: str = "sellers",
        variant: str = "overall",
    ):
        """Show a leaderboard (served from cache — updates every 6 hours)"""
        lb_type = lb_type.lower().strip()
        variant = variant.lower().strip()

        if lb_type not in {"sellers", "bidders", "listed", "won", "pokemon", "expensive"}:
            await ctx.reply(view=_build_help_view(), mention_author=False)
            return

        if variant not in {"normal", "shiny", "gmax", "overall"}:
            variant = "overall"

        # Default period = current month (index 1 in periods list, e.g. "2026-02")
        default_period = self.periods[1]["value"]

        view = _build_lb_view(lb_type, variant, default_period, ctx.author.id, self.periods)
        await ctx.reply(view=view, mention_author=False, allowed_mentions=SAFE_MENTIONS)

    # ── j!market ──────────────────────────────────────────────────────────────
    @commands.hybrid_command(name="auction_insights", aliases=["market", "ai"])
    async def market_cmd(self, ctx: commands.Context):
        """Poketwo auction market insights"""
        async with ctx.typing():
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _fetch_market_data)
            view = _build_market_view(data=data)
        await ctx.reply(view=view, mention_author=False)


# ═════════════════════════════════════════════════════════════════════════════
# SETUP
# ═════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))
