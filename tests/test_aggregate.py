"""Unit tests for scraper/aggregate.py using synthetic CSV fixtures.

Fixtures are written to pytest's tmp_path (never touches the real
data/daily/ or site/data/ directories).
"""
import csv
import json
from pathlib import Path

import pytest

from scraper import aggregate

HEADER = [
    "priceid", "tarikh_harga", "systemdate", "negeri", "daerah", "lokasi",
    "peringkat", "sublevel", "kategori", "kumpulan", "jenis", "varieti",
    "gred", "unit", "harga", "average14", "supply",
]


def write_daily_csv(daily_dir: Path, date: str, rows: list) -> None:
    """rows: list of dicts with any subset of HEADER keys; others default ''."""
    path = daily_dir / f"{date}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        for r in rows:
            full = {k: "" for k in HEADER}
            full.update(r)
            writer.writerow(full)


def make_row(pid, date, negeri="PERAK", peringkat="Runcit", varieti="BAYAM",
             gred="", unit="Kilogram", harga="1.00", kategori="SAYUR",
             systemdate=None, **extra):
    row = {
        "priceid": pid,
        "tarikh_harga": date,
        "systemdate": systemdate or f"{date} 09:15:00.000000",
        "negeri": negeri,
        "daerah": "DAERAH1",
        "lokasi": "PASAR1",
        "peringkat": peringkat,
        "sublevel": "",
        "kategori": kategori,
        "kumpulan": "SAYUR-SAYURAN",
        "jenis": "SAYUR",
        "varieti": varieti,
        "gred": gred,
        "unit": unit,
        "harga": harga,
        "average14": "",
        "supply": "",
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------
# National average = mean over ALL rows, not mean of per-state means
# ---------------------------------------------------------------------

def test_national_average_is_mean_over_rows_not_state_means(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    # State PERAK has two rows (1.00, 2.00); state SELANGOR has one row (10.00).
    # Mean over ALL rows = (1+2+10)/3 = 4.333... -> 4.33
    # Mean of per-state means would be (1.5 + 10)/2 = 5.75 -- must NOT match.
    rows = [
        make_row("1", "2026-07-20", negeri="PERAK", harga="1.00"),
        make_row("2", "2026-07-20", negeri="PERAK", harga="2.00"),
        make_row("3", "2026-07-20", negeri="SELANGOR", harga="10.00"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    aggregate.aggregate(daily_dir, out_dir)

    series = json.loads((out_dir / "series" / "bayam.json").read_text(encoding="utf-8"))
    idx = series["dates"].index("2026-07-20")
    assert series["national"]["Runcit"][idx] == 4.33
    assert series["national"]["Runcit"][idx] != 5.75

    # by_state values are the per-state means (mean over that state's rows only)
    assert series["by_state"]["PERAK"]["Runcit"][idx] == 1.50
    assert series["by_state"]["SELANGOR"]["Runcit"][idx] == 10.00


# ---------------------------------------------------------------------
# Product identity: varieti alone unless it has >1 distinct grade
# ---------------------------------------------------------------------

def test_product_identity_splits_by_grade_only_when_multiple_grades(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    rows = [
        # TELUR AYAM has 3 distinct grades -> splits into 3 products
        make_row("1", "2026-07-20", varieti="TELUR AYAM", gred="A", harga="0.45", kategori="TELUR"),
        make_row("2", "2026-07-20", varieti="TELUR AYAM", gred="B", harga="0.40", kategori="TELUR"),
        make_row("3", "2026-07-20", varieti="TELUR AYAM", gred="C", harga="0.35", kategori="TELUR"),
        # BAYAM has a single (empty) grade -> one product, "BAYAM" alone
        make_row("4", "2026-07-20", varieti="BAYAM", gred="", harga="1.00"),
        make_row("5", "2026-07-20", varieti="BAYAM", gred="", harga="1.20"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    summary = aggregate.aggregate(daily_dir, out_dir)
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    ids = {p["id"]: p for p in catalog["products"]}

    assert "telur-ayam-a" in ids
    assert "telur-ayam-b" in ids
    assert "telur-ayam-c" in ids
    assert ids["telur-ayam-a"]["name"] == "TELUR AYAM A"
    assert ids["telur-ayam-a"]["latest"]["Runcit"] == 0.45

    assert "bayam" in ids
    assert ids["bayam"]["name"] == "BAYAM"
    assert ids["bayam"]["latest"]["Runcit"] == 1.10  # mean(1.00, 1.20)

    # 5 rows total -> 4 products (3 telur ayam grades + bayam)
    assert summary["product_count"] == 4
    assert summary["row_count"] == 5


# ---------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------

def test_slug_generation_non_alnum_collapses_to_dash(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    rows = [
        make_row("1", "2026-07-20", varieti="CILI  API / Padi (Merah)", gred="", harga="5.00"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    aggregate.aggregate(daily_dir, out_dir)
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    assert len(catalog["products"]) == 1
    slug = catalog["products"][0]["id"]
    assert slug == "cili-api-padi-merah"
    assert (out_dir / "series" / f"{slug}.json").exists()


# ---------------------------------------------------------------------
# DoD / WoW change math
# ---------------------------------------------------------------------

def test_dod_uses_previous_available_date_wow_needs_exact_7_days(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    # Dates: 07-13 (price 10), 07-19 (price 12, gap - no 07-18 etc), 07-20 (price 15, latest).
    # DoD for latest (07-20) should compare vs 07-19 (previous AVAILABLE date), not 07-13.
    # WoW should compare vs exactly 07-13 (7 days before 07-20) since it exists -> 15-10=5.00
    write_daily_csv(daily_dir, "2026-07-13", [make_row("1", "2026-07-13", harga="10.00")])
    write_daily_csv(daily_dir, "2026-07-19", [make_row("2", "2026-07-19", harga="12.00")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("3", "2026-07-20", harga="15.00")])

    aggregate.aggregate(daily_dir, out_dir)
    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["date"] == "2026-07-20"
    row = next(r for r in latest["rows"] if r["id"] == "bayam" and r["level"] == "Runcit")
    assert row["price"] == 15.00
    assert row["dod"] == 3.00   # 15 - 12 (07-19, previous available, gap tolerated)
    assert row["wow"] == 5.00   # 15 - 10 (07-13 is exactly 7 days earlier and present)


def test_wow_is_null_when_exact_7_days_earlier_date_missing(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    # Only two dates, 6 days apart -> no exact 7-day-earlier date for WoW.
    write_daily_csv(daily_dir, "2026-07-14", [make_row("1", "2026-07-14", harga="10.00")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("2", "2026-07-20", harga="15.00")])

    aggregate.aggregate(daily_dir, out_dir)
    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    row = next(r for r in latest["rows"] if r["id"] == "bayam" and r["level"] == "Runcit")
    assert row["dod"] == 5.00
    assert row["wow"] is None


def test_dod_is_null_when_no_earlier_date_exists(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", harga="10.00")])

    aggregate.aggregate(daily_dir, out_dir)
    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    row = next(r for r in latest["rows"] if r["id"] == "bayam" and r["level"] == "Runcit")
    assert row["dod"] is None
    assert row["wow"] is None


# ---------------------------------------------------------------------
# Null handling for missing (date, level) combos
# ---------------------------------------------------------------------

def test_missing_date_level_combo_is_null_not_zero(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    # 07-20 only has Runcit rows for BAYAM; no Ladang/Borong rows at all.
    # 07-21 has a Runcit row too, so the dates axis includes both days.
    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", peringkat="Runcit", harga="1.00")])
    write_daily_csv(daily_dir, "2026-07-21", [make_row("2", "2026-07-21", peringkat="Runcit", harga="2.00")])

    aggregate.aggregate(daily_dir, out_dir)
    series = json.loads((out_dir / "series" / "bayam.json").read_text(encoding="utf-8"))
    assert series["dates"] == ["2026-07-20", "2026-07-21"]
    assert series["national"]["Ladang"] == [None, None]
    assert series["national"]["Borong"] == [None, None]
    assert series["national"]["Runcit"] == [1.00, 2.00]

    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    product = catalog["products"][0]
    assert product["levels"] == ["Runcit"]
    assert product["latest"]["Ladang"] is None
    assert product["latest"]["Borong"] is None
    assert product["latest"]["Runcit"] == 2.00


def test_product_with_fewer_than_two_rows_still_gets_catalog_entry(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", varieti="RARE ITEM", harga="9.00")])

    aggregate.aggregate(daily_dir, out_dir)
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    ids = {p["id"] for p in catalog["products"]}
    assert "rare-item" in ids


# ---------------------------------------------------------------------
# update_times histogram / median from systemdate
# ---------------------------------------------------------------------

def test_update_times_histogram_and_median(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    rows = [
        make_row("1", "2026-07-20", systemdate="2026-07-20 08:00:00.000000", harga="1.00"),
        make_row("2", "2026-07-20", systemdate="2026-07-20 08:30:00.000000", harga="1.00"),
        make_row("3", "2026-07-20", systemdate="2026-07-20 09:00:00.000000", harga="1.00"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    aggregate.aggregate(daily_dir, out_dir)
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))

    assert meta["update_times"]["by_hour"]["08"] == 2
    assert meta["update_times"]["by_hour"]["09"] == 1
    assert meta["update_times"]["by_hour"]["00"] == 0
    # Median of 08:00, 08:30, 09:00 (by seconds-of-day) -> the middle one, 08:30
    assert meta["update_times"]["median_entry_local"] == "08:30"
    assert "MYT" in meta["update_times"]["note"] or "Malaysia" in meta["update_times"]["note"]
    assert meta["row_count"] == 3


def test_meta_window_and_row_count(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir()

    write_daily_csv(daily_dir, "2026-07-19", [make_row("1", "2026-07-19", harga="1.00")])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", harga="1.00"),
        make_row("3", "2026-07-20", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["window"] == {"min": "2026-07-19", "max": "2026-07-20"}
    assert meta["row_count"] == 3

    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["dates"] == {"min": "2026-07-19", "max": "2026-07-20"}
