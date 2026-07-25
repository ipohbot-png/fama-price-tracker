"""scraper/aggregate.py

CLI: python -m scraper.aggregate [--daily-dir DIR] [--out-dir DIR]

Reads every data/daily/*.csv (Contract A, DESIGN.md section 3) and writes the
dashboard JSONs (Contract B v2, DESIGN.md section 4):

    site/data/catalog.json
    site/data/series/<product-slug>.json
    site/data/latest.json
    site/data/latest_state/<STATE>.json
    site/data/slugs.json
    site/data/meta.json

Stdlib only (csv, json, statistics, decimal, ...).

Key rules (see DESIGN.md Contract B for the authoritative spec):

- Product identity = (varieti, gred) when a varieti has more than one distinct
  gred value across the dataset, else varieti alone (grade folded away).
- Slug = lowercase product name, non-alnum runs collapsed to a single "-", but
  **slug assignments are sticky**: they are persisted in ``slugs.json`` and are
  never reassigned or reused. See `assign_slugs`.
- Every average (national or by_state) is the mean of `harga` over the raw
  rows matching (date, level, [state], product) -- i.e. mean over ALL rows,
  never a mean of per-state (or per-day) means.
- Rounding to 2dp, half-up, happens only at output time; internal sums use
  Decimal to avoid float drift.
- Missing (date, level) combinations are null, never 0, in the series files.

latest.json / latest_state/<STATE>.json (Contract B v2)
-------------------------------------------------------
A product is not priced every day at every level, so "the value on the max
archive date" left ~3/4 of the catalog blank. Each row is instead the product's
**last known** reading, with the date it came from disclosed:

- ``price`` = last non-null mean on or before the max archive date, looking back
  at most ``LOOKBACK_DAYS`` (14) days; ``date`` = the date of that reading;
  ``n`` = number of raw rows behind that mean. Product x level combinations with
  nothing in the lookback window are omitted.
- ``dod`` = ``price`` minus the previous available (non-null) point strictly
  before ``date``; ``dod_from`` = that point's date; null when there is none.
  The gap may be more than one day -- hence ``dod_from`` must be disclosed.
- ``wow`` = ``price`` minus the nearest available point on or before
  ``date - 7 days``; ``wow_from`` = that point's date; null when there is none.
  (Unified rule: nearest-on-or-before, never "exactly 7 days or nothing".)
- ``reliable`` = false when the DoD comparison is distorted by a change in the
  basket of rows behind the two means: the previous point's ``n`` differs by
  more than ``RELIABILITY_N_TOLERANCE`` (50%, measured against the larger of the
  two counts so the test is symmetric), or the Jaccard overlap of the states
  contributing rows falls below ``RELIABILITY_STATE_OVERLAP`` (50%). UI must
  exclude ``reliable: false`` rows from Top Movers.

``latest_state/<STATE>.json`` has exactly the same shape, computed over that
state's rows only (the state set test degenerates -- only the ``n`` test bites).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

LEVELS: Tuple[str, ...] = ("Ladang", "Borong", "Runcit")

#: How far back latest.json may look for a product's last known price.
LOOKBACK_DAYS = 14
#: WoW compares against the nearest point on or before (date - WOW_DAYS).
WOW_DAYS = 7
#: DoD is flagged unreliable when |n - prev_n| exceeds this share of max(n, prev_n).
RELIABILITY_N_TOLERANCE = 0.5
#: ...or when the Jaccard overlap of contributing state sets falls below this.
#: (Exact equality proved too strict: FAMA rotates surveyed states week to week,
#: which disqualified 81.5% of rows from Top Movers.)
RELIABILITY_STATE_OVERLAP = 0.5

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DAILY_DIR = REPO_ROOT / "data" / "daily"
DEFAULT_OUT_DIR = REPO_ROOT / "site" / "data"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def round2(value: Optional[Decimal]) -> Optional[float]:
    """Round a Decimal (or None) to 2dp half-up; returns a plain float.

    Normalises negative zero: a DoD of -0.004 must serialise as ``0.0``, not
    ``-0.0`` (which a UI would render as a spurious "-0.00" fall).
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    out = float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return 0.0 if out == 0 else out


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_decimal(raw: str) -> Optional[Decimal]:
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def parse_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    return raw or None


def parse_day(raw: str) -> Optional[_date]:
    """'YYYY-MM-DD' -> date, or None when unparseable."""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_systemdate(raw: str) -> Optional[datetime]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Reading Contract A CSVs
# --------------------------------------------------------------------------

def read_rows(daily_dir: Path) -> List[dict]:
    rows: List[dict] = []
    for path in sorted(Path(daily_dir).glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
    return rows


# --------------------------------------------------------------------------
# Product identity
# --------------------------------------------------------------------------

def compute_split_varieti(rows: Iterable[dict]) -> set:
    """Varieti names that have >1 distinct gred value -> identity splits by grade."""
    gred_by_varieti: Dict[str, set] = defaultdict(set)
    for r in rows:
        varieti = (r.get("varieti") or "").strip()
        gred = (r.get("gred") or "").strip()
        gred_by_varieti[varieti].add(gred)
    return {v for v, greds in gred_by_varieti.items() if len(greds) > 1}


def product_key(row: dict, split_varieti: set) -> Tuple[str, Optional[str]]:
    varieti = (row.get("varieti") or "").strip()
    gred = (row.get("gred") or "").strip()
    if varieti in split_varieti:
        return (varieti, gred)
    return (varieti, None)


def product_name(key: Tuple[str, Optional[str]]) -> str:
    varieti, gred = key
    if gred:
        return f"{varieti} {gred}"
    return varieti


def product_slug(key: Tuple[str, Optional[str]]) -> str:
    """The slug this identity *would* get from scratch (see assign_slugs)."""
    return slugify(product_name(key))


# --------------------------------------------------------------------------
# Slug stability (Contract B v2)
# --------------------------------------------------------------------------

SLUGS_FILENAME = "slugs.json"


def identity_key_str(key: Tuple[str, Optional[str]]) -> str:
    """JSON-encode a product identity so it can be a slugs.json object key."""
    varieti, gred = key
    return json.dumps([varieti, gred], ensure_ascii=False)


def load_slug_store(path: Path) -> Dict[str, str]:
    """Read slugs.json ({identity-key: slug}); missing/corrupt -> {}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def assign_slugs(
    ordered_keys: List[Tuple[str, Optional[str]]],
    stored: Dict[str, str],
    first_seen: Optional[Dict[Tuple[str, Optional[str]], str]] = None,
) -> Tuple[Dict[Tuple[str, Optional[str]], str], Dict[str, str]]:
    """Assign a stable slug to every product identity.

    Rules:

    1. An identity already in ``stored`` keeps its slug, forever, even if the
       naming rule would now produce a different one.
    2. Slugs in ``stored`` are reserved even when their identity has vanished
       from the archive, so a returning product gets its old slug back and a
       different product can never inherit it.
    3. When a varieti *gains* a second grade, its identity changes from
       ``(varieti, None)`` to ``(varieti, gred)``. The original grade -- the one
       whose rows appear earliest in the archive (ties broken by grade name) --
       inherits the original slug; the new grade(s) get fresh slugs.
    4. Anything still unassigned gets ``slugify(name)``, suffixed ``-2``, ``-3``
       ... if that is already taken or reserved.

    Returns ``(assignments, updated_store)``.
    """
    first_seen = first_seen or {}
    assignments: Dict[Tuple[str, Optional[str]], str] = {}
    store = dict(stored)
    taken = set(store.values())

    # 1. existing identities keep their slug
    pending: List[Tuple[str, Optional[str]]] = []
    for key in ordered_keys:
        slug = store.get(identity_key_str(key))
        if slug:
            assignments[key] = slug
        else:
            pending.append(key)

    # 3. grade split: hand the legacy (varieti, None) slug to the original grade
    by_varieti: Dict[str, List[Tuple[str, Optional[str]]]] = defaultdict(list)
    for key in pending:
        if key[1] is not None:
            by_varieti[key[0]].append(key)
    for varieti, keys in by_varieti.items():
        legacy = store.get(identity_key_str((varieti, None)))
        if not legacy or legacy in set(assignments.values()):
            continue
        heir = min(keys, key=lambda k: (first_seen.get(k, "9999-99-99"), k[1] or ""))
        assignments[heir] = legacy
        store[identity_key_str(heir)] = legacy
        pending.remove(heir)

    # 4. fresh slugs for the rest
    for key in pending:
        base = product_slug(key) or "product"
        slug = base
        i = 2
        while slug in taken or slug in set(assignments.values()):
            slug = f"{base}-{i}"
            i += 1
        assignments[key] = slug
        store[identity_key_str(key)] = slug
        taken.add(slug)

    return assignments, store


# --------------------------------------------------------------------------
# Aggregation core
# --------------------------------------------------------------------------

class Point(NamedTuple):
    """One available (non-null) reading in a product x level series."""

    day: _date
    date: str
    mean: Decimal
    n: int
    states: frozenset


class ProductAgg:
    """Accumulates raw rows for one product identity."""

    def __init__(self, key: Tuple[str, Optional[str]]):
        self.key = key
        self.kategori_counts: Counter = Counter()
        self.unit_counts: Counter = Counter()
        self.grades: set = set()
        self.levels_seen: set = set()
        self.states_seen: set = set()
        self.first_date: Optional[str] = None
        # (date, level) -> list[Decimal] national
        self.national_rows: Dict[Tuple[str, str], List[Decimal]] = defaultdict(list)
        # (date, level) -> set of states contributing rows (basket-change check)
        self.national_states: Dict[Tuple[str, str], set] = defaultdict(set)
        # (date, level, state) -> list[Decimal]
        self.state_rows: Dict[Tuple[str, str, str], List[Decimal]] = defaultdict(list)

    def add(self, date: str, level: str, state: str, gred: str, kategori: str,
            unit: str, harga: Optional[Decimal]) -> None:
        if kategori:
            self.kategori_counts[kategori] += 1
        if unit:
            self.unit_counts[unit] += 1
        if gred:
            self.grades.add(gred)
        if level:
            self.levels_seen.add(level)
        if state:
            self.states_seen.add(state)
        if date and (self.first_date is None or date < self.first_date):
            self.first_date = date
        if harga is not None:
            if level:
                self.national_rows[(date, level)].append(harga)
                if state:
                    self.national_states[(date, level)].add(state)
            if level and state:
                self.state_rows[(date, level, state)].append(harga)

    def most_common(self, counter: Counter) -> str:
        if not counter:
            return ""
        return counter.most_common(1)[0][0]

    # -- point series ------------------------------------------------------
    def national_points(self, dates_sorted: List[str], level: str) -> List[Point]:
        out: List[Point] = []
        for d in dates_sorted:
            vals = self.national_rows.get((d, level))
            if not vals:
                continue
            day = parse_day(d)
            if day is None:
                continue
            out.append(Point(
                day=day, date=d, mean=mean_decimal(vals), n=len(vals),
                states=frozenset(self.national_states.get((d, level), ())),
            ))
        return out

    def state_points(self, dates_sorted: List[str], level: str, state: str) -> List[Point]:
        out: List[Point] = []
        for d in dates_sorted:
            vals = self.state_rows.get((d, level, state))
            if not vals:
                continue
            day = parse_day(d)
            if day is None:
                continue
            out.append(Point(
                day=day, date=d, mean=mean_decimal(vals), n=len(vals),
                states=frozenset((state,)),
            ))
        return out


def mean_decimal(values: List[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    return sum(values) / len(values)


def latest_entry(
    points: List[Point],
    latest_day: _date,
    lookback_days: int = LOOKBACK_DAYS,
    wow_days: int = WOW_DAYS,
) -> Optional[dict]:
    """Contract B v2 last-known reading + DoD/WoW/reliable for one series.

    ``points`` must be ascending by date and contain only available (non-null)
    readings. Returns None when there is nothing within the lookback window.
    """
    idx = None
    for i in range(len(points) - 1, -1, -1):
        if points[i].day > latest_day:
            continue
        if (latest_day - points[i].day).days > lookback_days:
            return None  # ascending: everything earlier is even staler
        idx = i
        break
    if idx is None:
        return None

    cur = points[idx]

    dod = dod_from = None
    reliable = True
    if idx > 0:
        prev = points[idx - 1]
        dod = round2(cur.mean - prev.mean)
        dod_from = prev.date
        denom = max(cur.n, prev.n)
        if denom and abs(cur.n - prev.n) > RELIABILITY_N_TOLERANCE * denom:
            reliable = False
        union = cur.states | prev.states
        if union:
            jaccard = len(cur.states & prev.states) / len(union)
            if jaccard < RELIABILITY_STATE_OVERLAP:
                reliable = False

    wow = wow_from = None
    target = cur.day - timedelta(days=wow_days)
    for j in range(idx - 1, -1, -1):
        if points[j].day <= target:
            wow = round2(cur.mean - points[j].mean)
            wow_from = points[j].date
            break

    return {
        "price": round2(cur.mean),
        "date": cur.date,
        "n": cur.n,
        "dod": dod,
        "dod_from": dod_from,
        "wow": wow,
        "wow_from": wow_from,
        "reliable": reliable,
    }


def aggregate(daily_dir: Path, out_dir: Path) -> dict:
    """Run the full aggregation and write Contract B JSONs to out_dir.

    Returns a small summary dict (useful for tests/manual verification).
    """
    daily_dir = Path(daily_dir)
    out_dir = Path(out_dir)
    series_dir = out_dir / "series"
    state_dir = out_dir / "latest_state"
    out_dir.mkdir(parents=True, exist_ok=True)
    series_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    rows = read_rows(daily_dir)
    split_varieti = compute_split_varieti(rows)

    all_dates: set = set()
    all_states: set = set()
    products: Dict[Tuple[str, Optional[str]], ProductAgg] = {}
    systemdate_dts: List[datetime] = []
    row_count = 0

    for r in rows:
        row_count += 1
        date = parse_date(r.get("tarikh_harga", ""))
        level = (r.get("peringkat") or "").strip()
        state = (r.get("negeri") or "").strip()
        gred = (r.get("gred") or "").strip()
        kategori = (r.get("kategori") or "").strip()
        unit = (r.get("unit") or "").strip()
        harga = parse_decimal(r.get("harga", ""))

        if date:
            all_dates.add(date)
        if state:
            all_states.add(state)

        sd = parse_systemdate(r.get("systemdate", ""))
        if sd is not None:
            systemdate_dts.append(sd)

        key = product_key(r, split_varieti)
        agg = products.get(key)
        if agg is None:
            agg = ProductAgg(key)
            products[key] = agg
        if date and level:
            agg.add(date, level, state, gred, kategori, unit, harga)

    dates_sorted = sorted(all_dates)
    states_sorted = sorted(all_states)
    latest_date = dates_sorted[-1] if dates_sorted else None
    latest_day = parse_day(latest_date) if latest_date else None
    min_date = dates_sorted[0] if dates_sorted else None

    # Stable ordering: by product name for reproducible output.
    ordered_keys = sorted(products.keys(), key=lambda k: product_name(k))

    # --- stable slugs (Contract B v2) -------------------------------------
    slug_path = out_dir / SLUGS_FILENAME
    stored_slugs = load_slug_store(slug_path)
    first_seen = {k: (products[k].first_date or "9999-99-99") for k in ordered_keys}
    slug_by_key, slug_store = assign_slugs(ordered_keys, stored_slugs, first_seen)

    catalog_products = []
    latest_rows: List[dict] = []
    state_latest_rows: Dict[str, List[dict]] = {s: [] for s in states_sorted}
    live_slugs: set = set()

    for key in ordered_keys:
        agg = products[key]
        name = product_name(key)
        slug = slug_by_key[key]
        live_slugs.add(slug)

        kategori = agg.most_common(agg.kategori_counts)
        unit = agg.most_common(agg.unit_counts)
        grades_list = sorted(agg.grades)
        levels_list = [lv for lv in LEVELS if lv in agg.levels_seen]

        # --- national series (always all 3 levels, null-filled) ---
        national: Dict[str, List[Optional[float]]] = {lv: [] for lv in LEVELS}
        for lv in LEVELS:
            for d in dates_sorted:
                vals = agg.national_rows.get((d, lv))
                national[lv].append(round2(mean_decimal(vals)) if vals else None)

        # --- by_state series (only states that have any data for product) ---
        by_state: Dict[str, Dict[str, List[Optional[float]]]] = {}
        for state in states_sorted:
            if state not in agg.states_seen:
                continue
            state_levels: Dict[str, List[Optional[float]]] = {}
            has_any = False
            for lv in LEVELS:
                col = []
                for d in dates_sorted:
                    vals = agg.state_rows.get((d, lv, state))
                    m = mean_decimal(vals) if vals else None
                    if m is not None:
                        has_any = True
                    col.append(round2(m))
                state_levels[lv] = col
            if has_any:
                by_state[state] = state_levels

        series_doc = {
            "name": name,
            "unit": unit,
            "dates": dates_sorted,
            "national": national,
            "by_state": by_state,
        }
        with open(series_dir / f"{slug}.json", "w", encoding="utf-8") as f:
            json.dump(series_doc, f, ensure_ascii=False, separators=(",", ":"))

        # --- catalog entry (only fields the UI reads) ---
        catalog_products.append({
            "id": slug,
            "name": name,
            "kategori": kategori,
            "unit": unit,
            "grades": grades_list,
        })

        if latest_day is None:
            continue

        head = {"id": slug, "name": name, "unit": unit}

        # --- latest.json rows (national, last-known) ---
        for lv in levels_list:
            entry = latest_entry(agg.national_points(dates_sorted, lv), latest_day)
            if entry is None:
                continue
            latest_rows.append({**head, "level": lv, **entry})

        # --- latest_state/<STATE>.json rows ---
        for state in states_sorted:
            if state not in agg.states_seen:
                continue
            for lv in levels_list:
                entry = latest_entry(
                    agg.state_points(dates_sorted, lv, state), latest_day
                )
                if entry is None:
                    continue
                state_latest_rows[state].append({**head, "level": lv, **entry})

    # --- write catalog / latest / latest_state / slugs / meta --------------
    catalog = {
        "products": catalog_products,
        "states": states_sorted,
        "dates": {"min": min_date, "max": latest_date},
    }
    with open(out_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)

    latest_doc = {"date": latest_date, "rows": latest_rows}
    with open(out_dir / "latest.json", "w", encoding="utf-8") as f:
        json.dump(latest_doc, f, ensure_ascii=False, separators=(",", ":"))

    for state in states_sorted:
        doc = {"date": latest_date, "state": state, "rows": state_latest_rows[state]}
        with open(state_dir / f"{state}.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    with open(slug_path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(slug_store.items())), f, ensure_ascii=False, indent=1)

    # --- prune generated files that no longer belong ----------------------
    removed_series = prune_dir(series_dir, {f"{s}.json" for s in live_slugs})
    removed_states = prune_dir(state_dir, {f"{s}.json" for s in states_sorted})

    meta = build_meta(systemdate_dts, row_count, min_date, latest_date)
    with open(out_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

    return {
        "row_count": row_count,
        "product_count": len(catalog_products),
        "dates": catalog["dates"],
        "states": states_sorted,
        "latest_row_count": len(latest_rows),
        "latest_product_count": len({r["id"] for r in latest_rows}),
        "latest_state_files": len(states_sorted),
        "slug_count": len(slug_store),
        "removed_series": removed_series,
        "removed_state_files": removed_states,
    }


def prune_dir(directory: Path, keep: set) -> List[str]:
    """Delete *.json files in ``directory`` whose name is not in ``keep``."""
    removed = []
    for path in sorted(directory.glob("*.json")):
        if path.name not in keep:
            path.unlink()
            removed.append(path.name)
    return removed


def build_meta(systemdate_dts: List[datetime], row_count: int,
               min_date: Optional[str], max_date: Optional[str]) -> dict:
    by_hour = {f"{h:02d}": 0 for h in range(24)}
    for dt in systemdate_dts:
        by_hour[f"{dt.hour:02d}"] += 1

    median_entry_local = None
    if systemdate_dts:
        # Median of time-of-day, expressed as seconds since midnight, so the
        # median is insensitive to which calendar day each entry landed on.
        seconds = sorted(
            dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
            for dt in systemdate_dts
        )
        med = statistics.median(seconds)
        total_minutes = int(med // 60) % (24 * 60)
        median_entry_local = f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"min": min_date, "max": max_date},
        "row_count": row_count,
        "update_times": {
            "by_hour": by_hour,
            "median_entry_local": median_entry_local,
            "note": "Times are as recorded by FAMA's system (assumed Malaysia Time, MYT / UTC+8).",
        },
        "source": "FAMA (Lembaga Pemasaran Pertanian Persekutuan) - https://www.fama.gov.my/harga-pasaran-terkini",
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Aggregate FAMA daily CSVs into dashboard JSONs.")
    parser.add_argument("--daily-dir", type=Path, default=DEFAULT_DAILY_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    summary = aggregate(args.daily_dir, args.out_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
