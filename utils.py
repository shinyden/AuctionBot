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
import config

from config import (
    POKEMON_NAMES_FILE, EVENT_NAMES_FILE, EVOLUTION_CSV_FILE, CDN_MAPPING_CSV_FILE,
    POKEMON_DATA_CSV_FILE,
    CDN_BASE_URL, CDN_SHINY_URL,
    IV_BAR_LENGTH,
    FILLED_START, FILLED_MID, FILLED_END,
    EMPTY_START, EMPTY_MID, EMPTY_END,
    get_gender_emoji,
)
from filters import (
    resolve_flag, get_flag_info, is_flag,
    resolve_category_shortcut, is_category_shortcut,
)


# ─────────────────────────────────────────────────────────────────────────────
# TEXT NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Lowercase + strip accents (é→e, etc.). NFC first to handle precomposed chars."""
    s = unicodedata.normalize('NFC', s)
    return ''.join(
        c for c in unicodedata.normalize('NFD', s.lower())
        if unicodedata.category(c) != 'Mn'
    )


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON NAME DATA  (loaded once)
# ─────────────────────────────────────────────────────────────────────────────

class PokemonNameDB:
    """Maps every name/alias (all languages, normalized) → canonical English name."""

    def __init__(self, json_path: Path, extra_paths: list[Path] | None = None):
        self._map: dict[str, str] = {}
        self._load(json_path)
        for p in (extra_paths or []):
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    self._load_entries(json.load(f))
            else:
                print(f"[WARN] Extra names file not found at {p}")

    def _load(self, path: Path):
        if not path.exists():
            print(f"[WARN] pokemon_names.json not found at {path}")
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._load_entries(data)

    def _load_entries(self, data: list):
        """Load a list of name entries into the map."""
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
        """
        Resolve user_input to a canonical English name.
        Exact normalized match only — no partial matching.
        """
        return self._map.get(normalize(user_input))

    def all_names(self) -> list[str]:
        return list(set(self._map.values()))


_name_db: PokemonNameDB | None = None

def get_name_db() -> PokemonNameDB:
    global _name_db
    if _name_db is None:
        _name_db = PokemonNameDB(POKEMON_NAMES_FILE, extra_paths=[EVENT_NAMES_FILE])
    return _name_db


def resolve_pokemon_name(user_input: str) -> str | None:
    return get_name_db().resolve(user_input)


# ─────────────────────────────────────────────────────────────────────────────
# POKEMON DATA  (dex number, types, region — loaded once from pokemon_data.csv)
# ─────────────────────────────────────────────────────────────────────────────

class PokemonDataDB:
    """
    Loads pokemon_data.csv with columns:
      dex_number, name, region, type1, type2

    Provides:
      get_dex_number(canonical_name) → int | None
      get_names_by_dex(dex_number)   → list[str]   all canonical names sharing that dex
      get_names_by_type(types)       → list[str]   names matching ALL given types
      get_names_by_region(region)    → list[str]   names in that region
    """

    def __init__(self, csv_path: Path, name_db: PokemonNameDB):
        # canonical_name (lower) → dex_number
        self._name_to_dex: dict[str, int] = {}
        # dex_number → set of canonical names
        self._dex_to_names: dict[int, set[str]] = {}
        # canonical_name (lower) → (type1_lower, type2_lower|"")
        self._name_to_types: dict[str, tuple[str, str]] = {}
        # canonical_name (lower) → region_lower
        self._name_to_region: dict[str, str] = {}

        self._load(csv_path, name_db)

    def _load(self, path: Path, name_db: PokemonNameDB):
        if not path.exists():
            print(f"[WARN] pokemon_data.csv not found at {path}")
            return

        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw_dex  = row.get("dex_number", "").strip()
                raw_name = row.get("name", "").strip()
                region   = row.get("region", "").strip()
                type1    = row.get("type1", "").strip()
                type2    = row.get("type2", "").strip()

                if not raw_dex.lstrip("-").isdigit() or not raw_name:
                    continue

                dex = int(raw_dex)

                # Try to resolve to canonical name via name_db; fall back to raw
                canonical = name_db.resolve(raw_name) or raw_name
                key = normalize(canonical)

                self._name_to_dex[key] = dex
                self._dex_to_names.setdefault(dex, set()).add(canonical)
                self._name_to_types[key] = (type1.lower(), type2.lower())
                self._name_to_region[key] = region.lower()

    # ── Public API ─────────────────────────────────────────────────────────

    def get_dex_number(self, canonical_name: str) -> int | None:
        return self._name_to_dex.get(normalize(canonical_name))

    def get_names_by_dex(self, dex_number: int) -> list[str]:
        """Return all canonical names that share this dex number."""
        return list(self._dex_to_names.get(dex_number, set()))

    def get_names_by_type(self, types: list[str]) -> list[str]:
        """
        Return canonical names that have ALL given types (1 or 2 types).
        Type matching is case-insensitive. A mon is included if every
        requested type appears in its (type1, type2) tuple.
        """
        wanted = [t.lower() for t in types]
        result = []
        for key, (t1, t2) in self._name_to_types.items():
            mon_types = {t1, t2} - {""}
            if all(w in mon_types for w in wanted):
                # Recover canonical name from dex map
                dex = self._name_to_dex.get(key)
                if dex is not None:
                    for name in self._dex_to_names.get(dex, set()):
                        if normalize(name) == key:
                            result.append(name)
                            break
        return result

    def get_names_by_region(self, region: str) -> list[str]:
        """Return all canonical names in the given region (case-insensitive)."""
        wanted = region.lower()
        result = []
        for key, reg in self._name_to_region.items():
            if reg == wanted:
                dex = self._name_to_dex.get(key)
                if dex is not None:
                    for name in self._dex_to_names.get(dex, set()):
                        if normalize(name) == key:
                            result.append(name)
                            break
        return result


_pokemon_data_db: PokemonDataDB | None = None

def get_pokemon_data_db() -> PokemonDataDB:
    global _pokemon_data_db
    if _pokemon_data_db is None:
        _pokemon_data_db = PokemonDataDB(POKEMON_DATA_CSV_FILE, get_name_db())
    return _pokemon_data_db


def get_dex_family_names(canonical_name: str) -> list[str]:
    """
    Return all canonical names that share the same dex number as
    canonical_name (i.e. all forms/variants of that species).
    """
    db  = get_pokemon_data_db()
    dex = db.get_dex_number(canonical_name)
    if dex is None:
        return [canonical_name]
    names = db.get_names_by_dex(dex)
    return names if names else [canonical_name]


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
      31          → {field: {"$eq": 31}}
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
        return {field: {"$eq": float(val)}}  # fallback: exact
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-IV COUNT QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

_IV_FIELDS = ("hp", "atk", "def", "spa", "spd", "spe")


def build_iv_count_query(iv_value: str, min_count: int) -> dict | None:
    """
    Return a MongoDB $expr clause that asserts at least `min_count` of the
    six individual IV fields equal `iv_value`.
    """
    raw = iv_value.strip().replace(",", "")
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
        """
        Consume tokens until the next flag (or category shortcut) or end,
        joining with spaces.
        """
        parts = []
        while self.pos < len(self.tokens):
            tok = self.tokens[self.pos]
            if is_flag(tok) or is_category_shortcut(tok):
                break
            parts.append(self.next())
        return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# QUERY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_query(
    raw_args: list[str],
    expand_name_by_dex: bool = False,
) -> tuple[dict, list, int | None]:
    """
    Parse raw CLI tokens into a (mongo_query_dict, mongo_sort_list, limit).

    Name-pool logic
    ───────────────
    Two independent sets are built, then combined at the end:

    name_pool  – UNION set, grown by --name and --evo.
                 Each new --name / --evo adds its names to the pool.
                 If the pool is empty at the end, there is no pn filter.

    narrow_set – INTERSECTION set, built by --category / --type / --region.
                 Each narrowing filter intersects the running set.
                 If no narrowing filters were given, narrow_set stays None.

    Final pn filter:
      • Neither supplied            → no pn filter (match everything)
      • Only name_pool              → pn.$in = name_pool
      • Only narrow_set             → pn.$in = narrow_set
      • Both                        → pn.$in = name_pool ∩ narrow_set

    This means:
      --n arceus --n pikachu           → pool = {all Arceus forms} ∪ {all Pikachu forms}
      --ev --n arceus --type electric  → pool = event mons ∪ Arceus forms,
                                         then narrow to electric-type only

    Parameters
    ----------
    raw_args : list[str]
        Tokenised input (result of filters.split()).
    expand_name_by_dex : bool
        When True (auction cog), --name expands to ALL canonical names sharing
        the same dex number. When False (other cogs), resolves to the single
        canonical English name only.

    Returns
    -------
    (query, sort, limit)  — limit is None if --limit was not specified.
    """
    from categories import resolve_category

    query: dict        = {}
    sort: list         = [("ts", -1)]   # default: newest first
    and_clauses: list  = []
    limit: int | None  = None

    # ── Name resolution accumulators ──────────────────────────────────────────
    # name_pool : grown by --name / --evo  (UNION semantics)
    # narrow_set: shrunk by --category / --type / --region  (INTERSECT semantics)
    name_pool:   set[str] | None = None   # None = not yet used
    narrow_set:  set[str] | None = None   # None = not yet used
    type_filters: list[str] = []          # accumulates up to 2 --type values

    def _pool_add(names: set[str]) -> None:
        """Add names to the name_pool (union)."""
        nonlocal name_pool
        if name_pool is None:
            name_pool = set(names)
        else:
            name_pool |= names

    def _narrow_intersect(names: set[str]) -> None:
        """Intersect names into narrow_set."""
        nonlocal narrow_set
        if narrow_set is None:
            narrow_set = set(names)
        else:
            narrow_set &= names

    # ── Token loop ────────────────────────────────────────────────────────────
    reader = TokenReader(raw_args)

    while reader.peek() is not None:
        token = reader.next()

        # ── Category shortcut (e.g. --rares, --starters, --ev) ───────────────
        cat_key = resolve_category_shortcut(token)
        if cat_key is not None:
            cat = resolve_category(cat_key)
            if cat:
                _narrow_intersect(set(cat["pokemon"]))
            continue

        canonical = resolve_flag(token)
        if canonical is None:
            continue

        info = get_flag_info(canonical)

        # ── Boolean flags (shiny, gmax, noshiny, nogmax) ──────────────────────
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

        # ── Limit ─────────────────────────────────────────────────────────────
        if canonical == "--limit":
            try:
                limit = max(1, int(val.strip()))
            except ValueError:
                pass
            continue

        # ── Sort ──────────────────────────────────────────────────────────────
        if canonical == "--sort":
            sv = val.lower().strip()
            sort_field_map = {
                "iv":    "iv",
                "price": "bid",
                "level": "lv",
                "date":  "ts",
                "id":    "aid",
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

        # ── Category (--category <n>) → INTERSECT into narrow_set ────────────────────
        if canonical == "--category":
            cat = resolve_category(val)
            if cat:
                _narrow_intersect(set(cat["pokemon"]))
            continue

        # ── Evo family → UNION into name_pool ─────────────────────────────────
        if canonical == "--evo":
            family = get_evo_family(val)
            if family:
                _pool_add(set(family))
            continue

        # ── Type → INTERSECT into narrow_set ─────────────────────────────────
        if canonical == "--type":
            if len(type_filters) < 2:
                type_filters.append(val.strip())
            # Re-derive every time a new type is added so the set is always
            # based on the full type_filters list (handles --type fire --type flying).
            type_names = set(get_pokemon_data_db().get_names_by_type(type_filters))
            if type_names:
                # Intersect with any pre-existing narrow_set from --region
                # so order of flags doesn't matter.
                if narrow_set is not None:
                    narrow_set = narrow_set & type_names
                else:
                    narrow_set = type_names
            continue

        # ── Region → INTERSECT into narrow_set ───────────────────────────────
        if canonical == "--region":
            names = get_pokemon_data_db().get_names_by_region(val.strip())
            if names:
                _narrow_intersect(set(names))
            continue

        # ── Name → UNION into name_pool ───────────────────────────────────────
        if canonical == "--name":
            resolved = resolve_pokemon_name(val)

            if resolved:
                if expand_name_by_dex:
                    dex_names = get_dex_family_names(resolved)
                    names_to_add = set(dex_names) if dex_names else {resolved}
                else:
                    names_to_add = {resolved}
                _pool_add(names_to_add)
            else:
                # Fallback: normalised substring search (covers accented event
                # names not present in the main name DB).
                norm_words = normalize(val).split()
                name_db    = get_name_db()
                matches: set[str] = {
                    canonical_name
                    for norm_key, canonical_name in name_db._map.items()
                    if all(w in norm_key for w in norm_words)
                }
                if matches:
                    if expand_name_by_dex:
                        expanded: set[str] = set()
                        for m in matches:
                            expanded.update(get_dex_family_names(m))
                        matches = expanded
                    _pool_add(matches)
            continue

        # ── Nature ────────────────────────────────────────────────────────────
        if canonical == "--nature":
            query["nat"] = {"$regex": f"^{re.escape(val)}$", "$options": "i"}
            continue

        # ── Move (stackable) ──────────────────────────────────────────────────
        if canonical == "--move":
            and_clauses.append({
                "mv": {"$elemMatch": {"$regex": f"^{re.escape(val)}$", "$options": "i"}}
            })
            continue

        # ── Gender ────────────────────────────────────────────────────────────
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

        # ── Bidder ────────────────────────────────────────────────────────────
        if canonical == "--bidder":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["bdr"] = int(clean)
            continue

        # ── Seller ────────────────────────────────────────────────────────────
        if canonical == "--seller":
            clean = val.strip("<@!>")
            if clean.isdigit():
                query["sid"] = int(clean)
            else:
                query["sn"] = {"$regex": re.escape(val), "$options": "i"}
            continue

        # ── Price ─────────────────────────────────────────────────────────────
        if canonical in ("--price", "--maxprice", "--minprice"):
            if canonical == "--maxprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = "<=" + val
            elif canonical == "--minprice" and not any(val.startswith(op) for op in (">", "<", "=")):
                val = ">=" + val
            elif canonical == "--price" and not any(val.startswith(op) for op in (">", "<", "=")) and "-" not in val:
                val = "=" + val
            cond = parse_numeric_operator(val, "bid")
            if cond:
                existing = query.get("bid", {})
                existing.update(cond.get("bid", {}))
                query["bid"] = existing
            continue

        # ── Multi-IV count filters ─────────────────────────────────────────────
        if info.get("iv_count") is not None:
            clause = build_iv_count_query(val, info["iv_count"])
            if clause:
                and_clauses.append(clause)
            continue

        # ── Numeric IV / level fields ──────────────────────────────────────────
        mongo_field = info.get("mongo_field")
        if mongo_field:
            cond = parse_numeric_operator(val, mongo_field)
            if cond:
                existing = query.get(mongo_field, {})
                existing.update(cond.get(mongo_field, {}))
                query[mongo_field] = existing

    # ── Assemble final pn filter ──────────────────────────────────────────────
    # name_pool  = union of --name / --evo results
    # narrow_set = intersection of --type / --region results
    #
    # Rules:
    #   pool only   → match exactly those names
    #   narrow only → match exactly those names
    #   both        → match names present in BOTH (pool ∩ narrow)
    #   neither     → no pn filter (match all)
    if name_pool is not None or narrow_set is not None:
        if name_pool is not None and narrow_set is not None:
            final_names = name_pool & narrow_set
        elif name_pool is not None:
            final_names = name_pool
        else:
            final_names = narrow_set  # type: ignore[assignment]
        query["pn"] = {"$in": list(final_names)}

    if and_clauses:
        query.setdefault("$and", []).extend(and_clauses)

    return query, sort, limit


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def format_date(record: dict) -> str:
    ts = record.get("ts")
    if not ts:
        return "Unknown"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%-m/%-d/%y")


def iv_bar(value: int | None, max_val: int = 31, length: int = config.IV_BAR_LENGTH) -> str:
    """Build a progress bar using custom Discord emojis. length = total segments."""
    if value is None:
        value = 0
    filled = round((value / max_val) * length)
    filled = max(0, min(filled, length))
    empty  = length - filled

    # length is: 1 start + (length-2) mids + 1 end
    inner = length - 2  # number of mid slots

    if filled == 0:
        # All empty
        bar = config.EMPTY_START + config.EMPTY_MID * inner + config.EMPTY_END
    elif filled == length:
        # All filled
        bar = config.FILLED_START + config.FILLED_MID * inner + config.FILLED_END
    else:
        # Mixed — filled segments use filled emojis, empty use empty emojis
        # filled covers: start + some mids; empty covers: remaining mids + end
        filled_mids = max(0, filled - 1)       # start takes 1 filled slot
        empty_mids  = max(0, inner - filled_mids)  # remaining mid slots go empty

        if empty > 0:
            # last segment is empty_end
            bar = (
                config.FILLED_START
                + config.FILLED_MID * filled_mids
                + config.EMPTY_MID  * empty_mids
                + config.EMPTY_END
            )
        else:
            # no empty mids, just filled + filled_end
            bar = (
                config.FILLED_START
                + config.FILLED_MID * (inner - 0)
                + config.FILLED_END
            )

    return bar


def iv_line(label: str, value: int | None) -> str:
    val_s = str(value) if value is not None else "?"
    bar   = iv_bar(value)
    return f"`{label:>3}` {bar} `{val_s:>2}`"


def format_winning_bid(record: dict) -> str:
    bid = record.get("bid")
    return f"{bid:,} pc" if bid is not None else "??? pc"


def format_winning_bid_long(record: dict) -> str:
    bid = record.get("bid")
    return f"{bid:,} Pokécoins" if bid is not None else "???"


def shiny_prefix(record: dict) -> str:
    return "✨ " if record.get("sh") else ""
