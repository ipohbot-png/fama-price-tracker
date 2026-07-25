"""scraper/aggregate.py

CLI: python -m scraper.aggregate [--daily-dir DIR] [--out-dir DIR]

Reads every data/daily/*.csv (Contract A, DESIGN.md section 3) and writes the
dashboard JSONs (Contract B, DESIGN.md section 4):

    site/data/catalog.json
    site/data/series/<product-slug>.json
    site/data/latest.json
    site/data/meta.json

Stdlib only (csv, json, statistics, decimal, ...).

Key rules (see DESIGN.md Contract B for the authoritative spec):

- Product identity = (varieti, gred) when a varieti has more than one distinct
  gred value across the dataset, else varieti alone (grade folded away).
- Slug = lowercase varieti, with gred appended only when needed for
  uniqueness (i.e. exactly the same condition as product identity above);
  non-alnum runs collapse to a single "-".
- Every average (national or by_state) is the mean of `harga` over the raw
  rows matching (date, level, [state], product) -- i.e. mean over ALL rows,
  never a mean of per-state (or per-day) means.
- Rounding to 2dp, half-up, happens only at output time; internal sums use
  Decimal to avoid float drift.
- Missing (date, level) combinations are null, never 0.
- DoD = latest value minus the value on the previous date *that has a
  non-null value* for that product+level (i.e. the previous "available"
  point in that product+level's own series, regardless of any gap in
  calendar days). If there is no earlier non-null point, DoD is null.
- WoW = latest value minus the value exactly 7 calendar days earlier, only
  if that exact date exists in the dataset AND has a non-null value for that
  product+level; otherwise null (no fallback to "nearest available").
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

LEVELS: Tuple[str, ...] = ("Ladang", "Borong", "Runcit")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DAILY_DIR = REPO_ROOT / "data" / "daily"
DEFAULT_OUT_DIR = REPO_ROOT / "site" / "data"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def round2(value: Optional[Decimal]) -> Optional[float]:
    """Round a Decimal (or None) to 2dp half-up; returns a plain float."""
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    return slugify(product_name(key))


# --------------------------------------------------------------------------
# Aggregation core
# --------------------------------------------------------------------------

class ProductAgg:
    """Accumulates raw rows for one product identity."""

    def __init__(self, key: Tuple[str, Optional[str]]):
        self.key = key
        self.kategori_counts: Counter = Counter()
        self.unit_counts: Counter = Counter()
        self.grades: set = set()
        self.levels_seen: set = set()
        self.states_seen: set = set()
        # (date, level) -> list[Decimal] national
        self.national_rows: Dict[Tuple[str, str], List[Decimal]] = defaultdict(list)
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
        if harga is not None:
            if level:
                self.national_rows[(date, level)].append(harga)
            if level and state:
                self.state_rows[(date, level, state)].append(harga)

    def most_common(self, counter: Counter) -> str:
        if not counter:
            return ""
        return counter.most_common(1)[0][0]


def mean_decimal(values: List[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    return sum(values) / len(values)


def aggregate(daily_dir: Path, out_dir: Path) -> dict:
    """Run the full aggregation and write Contract B JSONs to out_dir.

    Returns a small summary dict (useful for tests/manual verification):
    {"row_count", "product_count", "dates": {"min","max"}, "states": [...]}
    """
    daily_dir = Path(daily_dir)
    out_dir = Path(out_dir)
    series_dir = out_dir / "series"
    out_dir.mkdir(parents=True, exist_ok=True)
    series_dir.mkdir(parents=True, exist_ok=True)

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
    min_date = dates_sorted[0] if dates_sorted else None

    # Map date -> index for O(1) lookups (used for DoD/WoW).
    date_index = {d: i for i, d in enumerate(dates_sorted)}

    catalog_products = []
    latest_rows = []

    # Stable ordering: by product name for reproducible output.
    ordered_keys = sorted(products.keys(), key=lambda k: product_name(k))
    used_slugs: Dict[str, Tuple[str, Optional[str]]] = {}

    for key in ordered_keys:
        agg = products[key]
        name = product_name(key)
        slug = product_slug(key)
        if slug in used_slugs and used_slugs[slug] != key:
            # Defensive fallback; the (varieti, gred-when-needed) rule should
            # already guarantee uniqueness, but don't silently collide.
            base = slug
            i = 2
            while slug in used_slugs:
                slug = f"{base}-{i}"
                i += 1
        used_slugs[slug] = key

        kategori = agg.most_common(agg.kategori_counts)
        unit = agg.most_common(agg.unit_counts)
        grades_list = sorted(agg.grades)
        levels_list = [lv for lv in LEVELS if lv in agg.levels_seen]

        # --- national series (always all 3 levels, null-filled) ---
        national: Dict[str, List[Optional[float]]] = {lv: [] for lv in LEVELS}
        # Keep Decimal series per level for DoD/WoW math (avoid float re-round).
        national_decimal: Dict[str, List[Optional[Decimal]]] = {lv: [] for lv in LEVELS}
        for lv in LEVELS:
            for d in dates_sorted:
                vals = agg.national_rows.get((d, lv))
                m = mean_decimal(vals) if vals else None
                national_decimal[lv].append(m)
                national[lv].append(round2(m))

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

        # --- catalog entry ---
        latest_vals = {}
        for lv in LEVELS:
            if latest_date is None:
                latest_vals[lv] = None
            else:
                idx = date_index[latest_date]
                latest_vals[lv] = round2(national_decimal[lv][idx])

        catalog_products.append({
            "id": slug,
            "name": name,
            "kategori": kategori,
            "unit": unit,
            "grades": grades_list,
            "levels": levels_list,
            "latest": latest_vals,
        })

        # --- latest.json rows ---
        if latest_date is not None:
            idx = date_index[latest_date]
            for lv in levels_list:
                price = national_decimal[lv][idx]
                if price is None:
                    continue

                # DoD: previous date (in dates_sorted, any gap) with non-null value.
                dod = None
                for j in range(idx - 1, -1, -1):
                    prev = national_decimal[lv][j]
                    if prev is not None:
                        dod = round2(price - prev)
                        break

                # WoW: exact date 7 calendar days earlier, if present & non-null.
                wow = None
                try:
                    target = (datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
                except ValueError:
                    target = None
                if target is not None and target in date_index:
                    prev7 = national_decimal[lv][date_index[target]]
                    if prev7 is not None:
                        wow = round2(price - prev7)

                latest_rows.append({
                    "id": slug,
                    "name": name,
                    "unit": unit,
                    "level": lv,
                    "price": round2(price),
                    "dod": dod,
                    "wow": wow,
                })

    catalog = {
        "products": catalog_products,
        "states": states_sorted,
        "dates": {"min": min_date, "max": max(dates_sorted) if dates_sorted else None},
    }
    with open(out_dir / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)

    latest_doc = {"date": latest_date, "rows": latest_rows}
    with open(out_dir / "latest.json", "w", encoding="utf-8") as f:
        json.dump(latest_doc, f, ensure_ascii=False, separators=(",", ":"))

    meta = build_meta(systemdate_dts, row_count, min_date, max(dates_sorted) if dates_sorted else None)
    with open(out_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))

    return {
        "row_count": row_count,
        "product_count": len(catalog_products),
        "dates": catalog["dates"],
        "states": states_sorted,
    }


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
