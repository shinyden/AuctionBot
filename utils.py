"""
utils.py – shared utilities for name resolution, evo families, query building.

All MongoDB field names use the shortened schema:
  mid  = message_id          aid  = auction_id
  ts   = unix_timestamp      pn   = pokemon_name
  lv   = level               sh   = shiny
  gx   = gmax                nat  = nature
  gen  = gender              hi   = held_item
  iv   = total_iv_percent    hp/atk/def/spa/spd/spe = individual IVs
  mv   = moves               bid  = winning_bid
  bdr  = bidder_id           sn   = seller_name
  sid  = seller_id
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from config import (
    POKEMON_NAMES_FILE, EVOLUTION_CSV_FILE, CDN_MAPPING_CSV_FILE,
    CDN_BASE_URL, CDN_SHINY_URL, IV_BAR_FILLED, IV_BAR_EMPTY, IV_BAR_LENGTH,
    get_gender_emoji,
)
from filters import resolve_flag, get_flag_info, is_flag


# ─────────────────────────────────────────────────────────────────────────────
# TEXT NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Lowercase + strip accents (é→e, etc.)."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', s.lower())
        if unicodedata.category(c) != 'Mn'
    )


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON NAME DATA  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class PokemonNameDB:
    """Maps every name/alias (all languages, normalized) → canonical English name."""

    def __init__(self, json_path: Path):
        self._map: dict[str, str] = {}
        self._load(json_path)

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_names.json not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        for entry in data:
            canonical = entry.get("name", "")
            if not canonical:
                continue
            self._map[normalize(canonical)] = canonical
            for _lang, value in entry.get("other_names", {}).items():
                if isinstance(value, list):
                    for v in value:
                        self._map[normalize(str(v))] = canonical
                elif value:
                    self._map[normalize(str(value))] = canonical

    def resolve(self, user_input: str) -> str | None:
        return self._map.get(normalize(user_input))

    def all_names(self) -> list[str]:
        return list(set(self._map.values()))


_name_db: PokemonNameDB | None = None

def get_name_db() -> PokemonNameDB:
    global _name_db
    if _name_db is None:
        _name_db = PokemonNameDB(POKEMON_NAMES_FILE)
    return _name_db


def resolve_pokemon_name(user_input: str) -> str | None:
    return get_name_db().resolve(user_input)


# ─────────────────────────────────────────────────────────────────────────────
# EVOLUTION FAMILIES  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class EvolutionDB:
    def __init__(self, csv_path: Path, name_db: PokemonNameDB):
        self._family: dict[str, frozenset[str]] = {}
        self._load(csv_path, name_db)

    def _load(self, path: Path, name_db: PokemonNameDB):
        if not path.exists():
            print(f"[WARN] evolution.csv not found at {path}")
            return

        families: list[frozenset[str]] = []

        with open(path, encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)

            for row in reader:
                if len(row) < 2:
                    continue
                members: set[str] = set()
                for cell in row[1:]:
                    cell = cell.strip()
                    if not cell or cell.startswith("Pokemon"):
                        continue
                    canonical = name_db.resolve(cell) or cell
                    members.add(canonical)

                if members:
                    families.append(frozenset(members))

        for fam in families:
            for name in fam:
                existing = self._family.get(name)
                if existing is None or len(fam) < len(existing):
                    self._family[name] = fam

    def get_family(self, canonical_name: str) -> frozenset[str] | None:
        return self._family.get(canonical_name)


_evo_db: EvolutionDB | None = None

def get_evo_db() -> EvolutionDB:
    global _evo_db
    if _evo_db is None:
        _evo_db = EvolutionDB(EVOLUTION_CSV_FILE, get_name_db())
    return _evo_db


def get_evo_family(user_input: str) -> frozenset[str] | None:
    canonical = resolve_pokemon_name(user_input) or user_input
    return get_evo_db().get_family(canonical)


# ─────────────────────────────────────────────────────────────────────────────
# CDN MAPPING  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class CdnDB:
    def __init__(self, csv_path: Path):
        self._map: dict[str, int] = {}
        self._load(csv_path)

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_cdn_mapping.csv not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                cdn  = row.get("cdn_number", "").strip()
                if name and cdn.isdigit():
                    self._map[normalize(name)] = int(cdn)

    def get_cdn(self, canonical_name: str) -> int | None:
        return self._map.get(normalize(canonical_name))


_cdn_db: CdnDB | None = None

def get_cdn_db() -> CdnDB:
    global _cdn_db
    if _cdn_db is None:
        _cdn_db = CdnDB(CDN_MAPPING_CSV_FILE)
    return _cdn_db


def get_pokemon_image_url(pokemon_name: str, shiny: bool = False) -> str | None:
    cdn_num = get_cdn_db().get_cdn(pokemon_name)
    if cdn_num is None:
        return None
    return (CDN_SHINY_URL if shiny else CDN_BASE_URL).format(cdn_num)


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC OPERATOR PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_numeric_operator(val: str, field: str) -> dict | None:
    """
    Parse expressions like:
      31          → {field: {"$gte": 31}}   (IV/level: at-least match)
      >30         → {field: {"$gt": 30}}
      >=30        → {field: {"$gte": 30}}
      <100        → {field: {"$lt": 100}}
      <=100       → {field: {"$lte": 100}}
      =100        → {field: {"$eq": 100}}
      30-100      → {field: {"$gte": 30, "$lte": 100}}
    """
    val = val.strip().replace(",", "")

    try:
        if val.startswith(">="):
            return {field: {"$gte": float(val[2:])}}
        if val.startswith("<="):
            return {field: {"$lte": float(val[2:])}}
        if val.startswith(">"):
            return {field: {"$gt": float(val[1:])}}
        if val.startswith("<"):
            return {field: {"$lt": float(val[1:])}}
        if val.startswith("="):
            return {field: {"$eq": float(val[1:])}}
        if "-" in val and not val.startswith("-"):
            lo, hi = val.split("-", 1)
            return {field: {"$gte": float(lo), "$lte": float(hi)}}
        return {field: {"$eq": float(val)}}  # fallback: at-least
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-IV COUNT QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

# The six individual IV short field names stored in the DB.
_IV_FIELDS = ("hp", "atk", "def", "spa", "spd", "spe")


def build_iv_count_query(iv_value: str, min_count: int) -> dict | None:
    """
    Return a MongoDB $expr clause that asserts at least `min_count` of the
    six individual IV fields equal `iv_value`.

    `iv_value` is the raw user token — a plain integer 0-31 only (no operators,
    no ranges).  Returns None if the value is not a valid integer.

    The generated query uses $expr / $sum / $cond so it works without any
    special index:

        {
          "$expr": {
            "$gte": [
              {
                "$sum": [
                  {"$cond": [{"$eq": ["$hp",  <n>]}, 1, 0]},
                  {"$cond": [{"$eq": ["$atk", <n>]}, 1, 0]},
                  ...
                ]
              },
              <min_count>
            ]
          }
        }
    """
    raw = iv_value.strip().replace(",", "")
    # Only plain integers make sense for "exactly equal" IV matching
    try:
        target = int(raw)
    except ValueError:
        return None

    if not (0 <= target <= 31):
        return None

    cond_exprs = [
        {"$cond": [{"$eq": [f"${field}", target]}, 1, 0]}
        for field in _IV_FIELDS
    ]

    return {
        "$expr": {
            "$gte": [
                {"$sum": cond_exprs},
                min_count,
            ]
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN READER  (handles multi-word flag values)
# ─────────────────────────────────────────────────────────────────────────────

class TokenReader:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str | None:
        t = self.peek()
        if t is not None:
            self.pos += 1
        return t

    def read_value_greedy(self) -> str:
        """Consume tokens until the next flag or end, joining with spaces."""
        parts = []
        while self.pos < len(self.tokens) and not is_flag(self.tokens[self.pos]):
            parts.append(self.next())
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_query(raw_args: list[str]) -> tuple[dict, list]:
    """
    Parse raw CLI tokens into a (mongo_query_dict, mongo_sort_list).

    All generated query keys use the short DB field names.
    Returns (query, sort).
    """
    from categories import resolve_category

    query: dict       = {}
    sort: list        = [("ts", -1)]   # default: newest first (ts = unix_timestamp)
    and_clauses: list = []

    reader = TokenReader(raw_args)

    while reader.peek() is not None:
        token    = reader.next()
        canonical = resolve_flag(token)

        if canonical is None:
            continue

        info = get_flag_info(canonical)

        # ── Boolean flags (shiny, gmax, noshiny, nogmax) ─────────────────────
        if not info.get("takes_arg"):
            mongo_field = info.get("mongo_field")
            if mongo_field:
                if canonical in ("--noshiny", "--nogmax"):
                    query[mongo_field] = {"$ne": True}
                else:
                    query[mongo_field] = True
            continue

        val = reader.read_value_greedy()
        if not val:
            continue

        # ── Sort ─────────────────────────────────────────────────────────────
        if canonical == "--sort":
            sv = val.lower().strip()
            sort_field_map = {
                "iv":    "iv",    # total_iv_percent → iv
                "price": "bid",   # winning_bid      → bid
                "level": "lv",    # level            → lv
                "date":  "ts",    # unix_timestamp   → ts
            }
            if sv.endswith("+"):
                direction, key = 1, sv[:-1]
            elif sv.endswith("-"):
                direction, key = -1, sv[:-1]
            else:
                direction, key = -1, sv

            field = sort_field_map.get(key)
            if field:
                sort = [(field, direction)]
            continue

        # ── Category ─────────────────────────────────────────────────────────
        if canonical == "--category":
            cat = resolve_category(val)
            if cat:
                query["pn"] = {"$in": cat["pokemon"]}   # pn = pokemon_name
            continue

        # ── Evo family ───────────────────────────────────────────────────────
        if canonical == "--evo":
            family = get_evo_family(val)
            if family:
                existing_in = query.get("pn", {}).get("$in")
                if existing_in is not None:
                    query["pn"] = {"$in": list(set(existing_in) & family)}
                else:
                    query["pn"] = {"$in": list(family)}
            continue

        # ── Name (multi-language) ─────────────────────────────────────────────
        if canonical == "--name":
            resolved = resolve_pokemon_name(val)
            if resolved:
                query["pn"] = {"$regex": f"^{re.escape(resolved)}$", "$options": "i"}
            else:
                query["pn"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Nature ───────────────────────────────────────────────────────────
        if canonical == "--nature":
            query["nat"] = {"$regex": f"^{re.escape(val)}$", "$options": "i"}
            continue

        # ── Move (stackable) ─────────────────────────────────────────────────
        if canonical == "--move":
            and_clauses.append({
                "mv": {"$elemMatch": {"$regex": f"^{re.escape(val)}$", "$options": "i"}}
            })
            continue

        # ── Gender ───────────────────────────────────────────────────────────────
        if canonical == "--gender":
            gender_map = {
                "male":    "Male",
                "m":       "Male",
                "female":  "Female",
                "f":       "Female",
                "unknown": "Unknown",
                "unk":     "Unknown",
                "none":    "Unknown",
            }
            mapped = gender_map.get(val.strip().lower())
            if mapped:
                query["gen"] = {"$regex": f"^{re.escape(mapped)}$", "$options": "i"}
            else:
                query["gen"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Bidder ───────────────────────────────────────────────────────────
        if canonical == "--bidder":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["bdr"] = int(clean)   # bdr = bidder_id
            continue

        # ── Seller ───────────────────────────────────────────────────────────
        # Supports:
        #   --seller @mention or numeric ID  → match on sid (int)
        #   --seller some name               → match on sn (seller_name, case-insensitive)
        if canonical == "--seller":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["sid"] = int(clean)
            else:
                query["sn"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Price ────────────────────────────────────────────────────────────
        if canonical in ("--price", "--maxprice", "--minprice"):
            if canonical == "--maxprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = "<=" + val
            elif canonical == "--minprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = ">=" + val
            elif canonical == "--price" and not any(val.startswith(op) for op in (">", "<", "=")) and "-" not in val:
                val = "=" + val
            cond = parse_numeric_operator(val, "bid")   # bid = winning_bid
            if cond:
                existing = query.get("bid", {})
                existing.update(cond.get("bid", {}))
                query["bid"] = existing
            continue

        # ── Multi-IV count filters (triple / quad / penta / hex) ─────────────
        if info.get("iv_count") is not None:
            clause = build_iv_count_query(val, info["iv_count"])
            if clause:
                and_clauses.append(clause)
            continue

        # ── Numeric IV / level fields (mongo_field from filters.py) ──────────
        mongo_field = info.get("mongo_field")
        if mongo_field:
            cond = parse_numeric_operator(val, mongo_field)
            if cond:
                existing = query.get(mongo_field, {})
                existing.update(cond.get(mongo_field, {}))
                query[mongo_field] = existing

    if and_clauses:
        query.setdefault("$and", []).extend(and_clauses)

    return query, sort


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def format_date(record: dict) -> str:
    ts = record.get("ts")   # ts = unix_timestamp
    if not ts:
        return "Unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%-m/%-d/%y")


def iv_bar(value: int | None) -> str:
    """Return a text progress bar for a 0-31 IV value."""
    if value is None:
        return IV_BAR_EMPTY * IV_BAR_LENGTH
    filled = round(value / 31 * IV_BAR_LENGTH)
    return IV_BAR_FILLED * filled + IV_BAR_EMPTY * (IV_BAR_LENGTH - filled)


def iv_line(label: str, value: int | None) -> str:
    val_s = str(value) if value is not None else "???"
    return f"`{label:<4}` {iv_bar(value)} `{val_s}/31`"


def format_winning_bid(record: dict) -> str:
    bid = record.get("bid")   # bid = winning_bid
    return f"{bid:,} pc" if bid is not None else "??? pc"


def format_winning_bid_long(record: dict) -> str:
    bid = record.get("bid")   # bid = winning_bid
    return f"{bid:,} Pokécoins" if bid is not None else "???"


def shiny_prefix(record: dict) -> str:
    return "✨ " if record.get("sh") else ""   # sh = shiny
