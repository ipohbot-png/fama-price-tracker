"""Unit tests for scraper/aggregate.py using synthetic CSV fixtures.

Fixtures are written to pytest's tmp_path (never touches the real
data/daily/ or site/data/ directories).
"""
import csv
import json
import math
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


def dirs(tmp_path):
    daily_dir = tmp_path / "daily"
    out_dir = tmp_path / "out"
    daily_dir.mkdir(exist_ok=True)
    return daily_dir, out_dir


def load(out_dir: Path, *parts) -> dict:
    return json.loads((out_dir.joinpath(*parts)).read_text(encoding="utf-8"))


def latest_row(out_dir: Path, slug: str, level: str = "Runcit", state=None) -> dict:
    if state is None:
        doc = load(out_dir, "latest.json")
    else:
        doc = load(out_dir, "latest_state", f"{state}.json")
    return next(r for r in doc["rows"] if r["id"] == slug and r["level"] == level)


# ---------------------------------------------------------------------
# National average = mean over ALL rows, not mean of per-state means
# ---------------------------------------------------------------------

def test_national_average_is_mean_over_rows_not_state_means(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

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

    series = load(out_dir, "series", "bayam.json")
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
    daily_dir, out_dir = dirs(tmp_path)

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
    catalog = load(out_dir, "catalog.json")
    ids = {p["id"]: p for p in catalog["products"]}

    assert "telur-ayam-a" in ids
    assert "telur-ayam-b" in ids
    assert "telur-ayam-c" in ids
    assert ids["telur-ayam-a"]["name"] == "TELUR AYAM A"
    assert latest_row(out_dir, "telur-ayam-a")["price"] == 0.45

    assert "bayam" in ids
    assert ids["bayam"]["name"] == "BAYAM"
    assert latest_row(out_dir, "bayam")["price"] == 1.10  # mean(1.00, 1.20)

    # 5 rows total -> 4 products (3 telur ayam grades + bayam)
    assert summary["product_count"] == 4
    assert summary["row_count"] == 5


# ---------------------------------------------------------------------
# Slug generation + stability (Contract B v2)
# ---------------------------------------------------------------------

def test_slug_generation_non_alnum_collapses_to_dash(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    rows = [
        make_row("1", "2026-07-20", varieti="CILI  API / Padi (Merah)", gred="", harga="5.00"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    aggregate.aggregate(daily_dir, out_dir)
    catalog = load(out_dir, "catalog.json")
    assert len(catalog["products"]) == 1
    slug = catalog["products"][0]["id"]
    assert slug == "cili-api-padi-merah"
    assert (out_dir / "series" / f"{slug}.json").exists()


def test_slugs_json_is_persisted_keyed_by_identity(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", varieti="BAYAM", harga="1.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    store = load(out_dir, "slugs.json")
    assert store == {'["BAYAM", null]': "bayam"}


def test_existing_slug_is_never_reassigned_when_naming_rule_changes(tmp_path):
    """A varieti gaining a second grade keeps its slug for the original grade."""
    daily_dir, out_dir = dirs(tmp_path)

    # Run 1: TELUR AYAM has one grade -> identity ("TELUR AYAM", None) -> "telur-ayam"
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", varieti="TELUR AYAM", gred="A", harga="0.45"),
    ])
    aggregate.aggregate(daily_dir, out_dir)
    assert {p["id"] for p in load(out_dir, "catalog.json")["products"]} == {"telur-ayam"}

    # Run 2: grade B appears -> identity splits. Grade A (seen first) keeps the
    # original slug; grade B gets a new one. Nothing is reassigned.
    write_daily_csv(daily_dir, "2026-07-21", [
        make_row("2", "2026-07-21", varieti="TELUR AYAM", gred="A", harga="0.46"),
        make_row("3", "2026-07-21", varieti="TELUR AYAM", gred="B", harga="0.40"),
    ])
    aggregate.aggregate(daily_dir, out_dir)

    products = {p["id"]: p for p in load(out_dir, "catalog.json")["products"]}
    assert products["telur-ayam"]["name"] == "TELUR AYAM A"
    assert products["telur-ayam-b"]["name"] == "TELUR AYAM B"

    store = load(out_dir, "slugs.json")
    assert store['["TELUR AYAM", null]'] == "telur-ayam"
    assert store['["TELUR AYAM", "A"]'] == "telur-ayam"
    assert store['["TELUR AYAM", "B"]'] == "telur-ayam-b"


def test_orphan_series_files_are_deleted_but_slug_stays_reserved(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", varieti="BAYAM", harga="1.00"),
        make_row("2", "2026-07-20", varieti="KANGKUNG", harga="2.00"),
    ])
    aggregate.aggregate(daily_dir, out_dir)
    assert (out_dir / "series" / "kangkung.json").exists()

    # KANGKUNG disappears from the archive entirely.
    (daily_dir / "2026-07-20.csv").unlink()
    write_daily_csv(daily_dir, "2026-07-21", [
        make_row("3", "2026-07-21", varieti="BAYAM", harga="1.50"),
        # A different varieti whose natural slug collides with the retired one.
        make_row("4", "2026-07-21", varieti="KANGKUNG!", harga="3.00"),
    ])
    summary = aggregate.aggregate(daily_dir, out_dir)

    assert not (out_dir / "series" / "kangkung.json").exists()
    assert "kangkung.json" in summary["removed_series"]

    # The retired slug stays reserved: the new product cannot take it.
    store = load(out_dir, "slugs.json")
    assert store['["KANGKUNG", null]'] == "kangkung"
    assert store['["KANGKUNG!", null]'] == "kangkung-2"
    assert (out_dir / "series" / "kangkung-2.json").exists()


def test_assign_slugs_keeps_stored_assignment_over_naming_rule():
    stored = {'["BAYAM", null]': "legacy-bayam-slug"}
    assignments, store = aggregate.assign_slugs([("BAYAM", None)], stored)
    assert assignments[("BAYAM", None)] == "legacy-bayam-slug"
    assert store == stored


# ---------------------------------------------------------------------
# catalog.json carries only the fields the UI reads
# ---------------------------------------------------------------------

def test_catalog_products_drop_levels_and_latest(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", harga="1.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    product = load(out_dir, "catalog.json")["products"][0]
    assert set(product) == {"id", "name", "kategori", "unit", "grades"}


# ---------------------------------------------------------------------
# latest.json — Contract B v2
# ---------------------------------------------------------------------

def test_latest_row_has_contract_b_v2_shape(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", harga="1.00")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert set(row) == {
        "id", "name", "unit", "level", "price", "date", "n",
        "dod", "dod_from", "wow", "wow_from", "reliable",
    }
    assert row["price"] == 1.00
    assert row["date"] == "2026-07-20"
    assert row["n"] == 1
    assert row["dod"] is None and row["dod_from"] is None
    assert row["wow"] is None and row["wow_from"] is None
    assert row["reliable"] is True


def test_latest_falls_back_to_last_known_price_within_14_days(tmp_path):
    """The default view must not be limited to products priced on the max date."""
    daily_dir, out_dir = dirs(tmp_path)

    # BAYAM priced only on 07-11 (9 days before the max archive date).
    write_daily_csv(daily_dir, "2026-07-11", [
        make_row("1", "2026-07-11", varieti="BAYAM", harga="4.00"),
    ])
    # KANGKUNG carries the max date so the archive window ends on 07-20.
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", varieti="KANGKUNG", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    doc = load(out_dir, "latest.json")
    assert doc["date"] == "2026-07-20"          # header date = max archive date
    row = latest_row(out_dir, "bayam")
    assert row["price"] == 4.00
    assert row["date"] == "2026-07-11"          # per-row date is disclosed
    assert {r["id"] for r in doc["rows"]} == {"bayam", "kangkung"}


def test_latest_omits_products_older_than_the_lookback_window(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # 07-05 is 15 days before 07-20 -> outside the 14-day lookback.
    write_daily_csv(daily_dir, "2026-07-05", [
        make_row("1", "2026-07-05", varieti="BAYAM", harga="4.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", varieti="KANGKUNG", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    doc = load(out_dir, "latest.json")
    assert {r["id"] for r in doc["rows"]} == {"kangkung"}


def test_latest_lookback_boundary_is_inclusive_at_14_days(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-06", [
        make_row("1", "2026-07-06", varieti="BAYAM", harga="4.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", varieti="KANGKUNG", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    assert latest_row(out_dir, "bayam")["date"] == "2026-07-06"


def test_dod_uses_previous_available_point_and_discloses_its_date(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # 07-13 (10), 07-19 (12), 07-20 (15, latest). DoD compares 07-20 vs 07-19.
    write_daily_csv(daily_dir, "2026-07-13", [make_row("1", "2026-07-13", harga="10.00")])
    write_daily_csv(daily_dir, "2026-07-19", [make_row("2", "2026-07-19", harga="12.00")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("3", "2026-07-20", harga="15.00")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["price"] == 15.00
    assert row["dod"] == 3.00
    assert row["dod_from"] == "2026-07-19"
    # WoW target is 07-13 and that exact date exists -> 15 - 10.
    assert row["wow"] == 5.00
    assert row["wow_from"] == "2026-07-13"


def test_wow_uses_nearest_point_on_or_before_seven_days(tmp_path):
    """Unified WoW rule: nearest on-or-before -7d, not 'exactly -7d or null'."""
    daily_dir, out_dir = dirs(tmp_path)

    # Target for 07-20 is 07-13; no 07-13 row, nearest earlier point is 07-11.
    write_daily_csv(daily_dir, "2026-07-11", [make_row("1", "2026-07-11", harga="10.00")])
    write_daily_csv(daily_dir, "2026-07-19", [make_row("2", "2026-07-19", harga="12.00")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("3", "2026-07-20", harga="15.00")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["wow"] == 5.00
    assert row["wow_from"] == "2026-07-11"


def test_wow_is_null_when_nothing_exists_on_or_before_target(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # Only 07-14 and 07-20 -> target 07-13 has no point at or before it.
    write_daily_csv(daily_dir, "2026-07-14", [make_row("1", "2026-07-14", harga="10.00")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("2", "2026-07-20", harga="15.00")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["dod"] == 5.00
    assert row["dod_from"] == "2026-07-14"
    assert row["wow"] is None
    assert row["wow_from"] is None


def test_dod_is_null_when_no_earlier_point_exists(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", harga="10.00")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["dod"] is None
    assert row["dod_from"] is None
    assert row["wow"] is None
    assert row["reliable"] is True


def test_tiny_negative_change_does_not_serialise_as_negative_zero(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    # Mean drops from 1.001 to 1.000 -> -0.001 -> rounds to zero, not "-0.0".
    write_daily_csv(daily_dir, "2026-07-19", [make_row("1", "2026-07-19", harga="1.001")])
    write_daily_csv(daily_dir, "2026-07-20", [make_row("2", "2026-07-20", harga="1.000")])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["dod"] == 0.0
    assert math.copysign(1, row["dod"]) > 0     # 0.0, never -0.0
    assert json.dumps(row["dod"]) == "0.0"


# ---------------------------------------------------------------------
# reliable flag
# ---------------------------------------------------------------------

def test_reliable_true_when_basket_is_stable(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    for date, prices in (("2026-07-19", ("1.00", "2.00")), ("2026-07-20", ("1.20", "2.20"))):
        write_daily_csv(daily_dir, date, [
            make_row(f"{date}-1", date, negeri="PERAK", harga=prices[0]),
            make_row(f"{date}-2", date, negeri="KEDAH", harga=prices[1]),
        ])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["n"] == 2
    assert row["reliable"] is True


def test_reliable_false_when_row_count_changes_more_than_half(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # Previous point: 4 rows. Latest point: 1 row (same state) -> 75% drop.
    write_daily_csv(daily_dir, "2026-07-19", [
        make_row(f"a{i}", "2026-07-19", negeri="PERAK", harga="1.00") for i in range(4)
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("b1", "2026-07-20", negeri="PERAK", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["n"] == 1
    assert row["dod"] == 1.00
    assert row["reliable"] is False


def test_reliable_false_when_contributing_state_set_differs(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # Same row count (2) both days, but a different pair of states.
    write_daily_csv(daily_dir, "2026-07-19", [
        make_row("a1", "2026-07-19", negeri="PERAK", harga="1.00"),
        make_row("a2", "2026-07-19", negeri="KEDAH", harga="1.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("b1", "2026-07-20", negeri="PERAK", harga="2.00"),
        make_row("b2", "2026-07-20", negeri="JOHOR", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    row = latest_row(out_dir, "bayam")
    assert row["n"] == 2
    assert row["reliable"] is False


def test_reliable_true_at_exactly_fifty_percent_change(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # 2 rows -> 1 row is exactly 50% of the larger count: not *more than* 50%.
    write_daily_csv(daily_dir, "2026-07-19", [
        make_row("a1", "2026-07-19", negeri="PERAK", harga="1.00"),
        make_row("a2", "2026-07-19", negeri="PERAK", harga="1.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("b1", "2026-07-20", negeri="PERAK", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    assert latest_row(out_dir, "bayam")["reliable"] is True


# ---------------------------------------------------------------------
# latest_state/<STATE>.json
# ---------------------------------------------------------------------

def test_latest_state_file_per_state_scoped_to_that_state(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-19", [
        make_row("a1", "2026-07-19", negeri="PERAK", harga="1.00"),
        make_row("a2", "2026-07-19", negeri="SELANGOR", harga="10.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("b1", "2026-07-20", negeri="PERAK", harga="2.00"),
        make_row("b2", "2026-07-20", negeri="SELANGOR", harga="20.00"),
    ])

    summary = aggregate.aggregate(daily_dir, out_dir)
    assert summary["latest_state_files"] == 2
    assert sorted(p.name for p in (out_dir / "latest_state").glob("*.json")) == [
        "PERAK.json", "SELANGOR.json",
    ]

    perak = load(out_dir, "latest_state", "PERAK.json")
    assert perak["date"] == "2026-07-20"
    assert perak["state"] == "PERAK"
    row = latest_row(out_dir, "bayam", state="PERAK")
    assert set(row) == {
        "id", "name", "unit", "level", "price", "date", "n",
        "dod", "dod_from", "wow", "wow_from", "reliable",
    }
    assert row["price"] == 2.00        # PERAK only, not the national 11.00
    assert row["n"] == 1
    assert row["dod"] == 1.00
    assert row["dod_from"] == "2026-07-19"

    assert latest_row(out_dir, "bayam", state="SELANGOR")["price"] == 20.00
    # national mean over all rows on 07-20
    assert latest_row(out_dir, "bayam")["price"] == 11.00


def test_latest_state_omits_products_absent_in_that_state(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", negeri="PERAK", varieti="BAYAM", harga="1.00"),
        make_row("2", "2026-07-20", negeri="SELANGOR", varieti="KANGKUNG", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    assert {r["id"] for r in load(out_dir, "latest_state", "PERAK.json")["rows"]} == {"bayam"}
    assert {r["id"] for r in load(out_dir, "latest_state", "SELANGOR.json")["rows"]} == {"kangkung"}


def test_latest_state_uses_its_own_lookback_per_state(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # PERAK last priced BAYAM on 07-14; SELANGOR prices it on 07-20.
    write_daily_csv(daily_dir, "2026-07-14", [
        make_row("1", "2026-07-14", negeri="PERAK", harga="3.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", negeri="SELANGOR", harga="9.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    perak_row = latest_row(out_dir, "bayam", state="PERAK")
    assert perak_row["price"] == 3.00
    assert perak_row["date"] == "2026-07-14"
    assert latest_row(out_dir, "bayam", state="SELANGOR")["date"] == "2026-07-20"


def test_stale_latest_state_files_are_pruned(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("1", "2026-07-20", negeri="PERAK", harga="1.00"),
        make_row("2", "2026-07-20", negeri="SABAH", harga="1.00"),
    ])
    aggregate.aggregate(daily_dir, out_dir)
    assert (out_dir / "latest_state" / "SABAH.json").exists()

    (daily_dir / "2026-07-20.csv").unlink()
    write_daily_csv(daily_dir, "2026-07-21", [
        make_row("3", "2026-07-21", negeri="PERAK", harga="1.00"),
    ])
    aggregate.aggregate(daily_dir, out_dir)
    assert not (out_dir / "latest_state" / "SABAH.json").exists()


# ---------------------------------------------------------------------
# Null handling for missing (date, level) combos
# ---------------------------------------------------------------------

def test_missing_date_level_combo_is_null_not_zero(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    # 07-20 only has Runcit rows for BAYAM; no Ladang/Borong rows at all.
    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", peringkat="Runcit", harga="1.00")])
    write_daily_csv(daily_dir, "2026-07-21", [make_row("2", "2026-07-21", peringkat="Runcit", harga="2.00")])

    aggregate.aggregate(daily_dir, out_dir)
    series = load(out_dir, "series", "bayam.json")
    assert series["dates"] == ["2026-07-20", "2026-07-21"]
    assert series["national"]["Ladang"] == [None, None]
    assert series["national"]["Borong"] == [None, None]
    assert series["national"]["Runcit"] == [1.00, 2.00]

    # latest.json carries a row only for the level that has data.
    levels = {r["level"] for r in load(out_dir, "latest.json")["rows"]}
    assert levels == {"Runcit"}
    assert latest_row(out_dir, "bayam")["price"] == 2.00


def test_product_with_fewer_than_two_rows_still_gets_catalog_entry(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-20", [make_row("1", "2026-07-20", varieti="RARE ITEM", harga="9.00")])

    aggregate.aggregate(daily_dir, out_dir)
    ids = {p["id"] for p in load(out_dir, "catalog.json")["products"]}
    assert "rare-item" in ids


def test_product_out_of_lookback_still_appears_in_catalog_and_series(tmp_path):
    """Dropping out of latest.json must not drop a product from the catalog."""
    daily_dir, out_dir = dirs(tmp_path)
    write_daily_csv(daily_dir, "2026-07-01", [
        make_row("1", "2026-07-01", varieti="BAYAM", harga="4.00"),
    ])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", varieti="KANGKUNG", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    ids = {p["id"] for p in load(out_dir, "catalog.json")["products"]}
    assert ids == {"bayam", "kangkung"}
    assert (out_dir / "series" / "bayam.json").exists()
    assert "bayam" not in {r["id"] for r in load(out_dir, "latest.json")["rows"]}


# ---------------------------------------------------------------------
# update_times histogram / median from systemdate
# ---------------------------------------------------------------------

def test_update_times_histogram_and_median(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    rows = [
        make_row("1", "2026-07-20", systemdate="2026-07-20 08:00:00.000000", harga="1.00"),
        make_row("2", "2026-07-20", systemdate="2026-07-20 08:30:00.000000", harga="1.00"),
        make_row("3", "2026-07-20", systemdate="2026-07-20 09:00:00.000000", harga="1.00"),
    ]
    write_daily_csv(daily_dir, "2026-07-20", rows)

    aggregate.aggregate(daily_dir, out_dir)
    meta = load(out_dir, "meta.json")

    assert meta["update_times"]["by_hour"]["08"] == 2
    assert meta["update_times"]["by_hour"]["09"] == 1
    assert meta["update_times"]["by_hour"]["00"] == 0
    # Median of 08:00, 08:30, 09:00 (by seconds-of-day) -> the middle one, 08:30
    assert meta["update_times"]["median_entry_local"] == "08:30"
    assert "MYT" in meta["update_times"]["note"] or "Malaysia" in meta["update_times"]["note"]
    assert meta["row_count"] == 3


def test_meta_window_and_row_count(tmp_path):
    daily_dir, out_dir = dirs(tmp_path)

    write_daily_csv(daily_dir, "2026-07-19", [make_row("1", "2026-07-19", harga="1.00")])
    write_daily_csv(daily_dir, "2026-07-20", [
        make_row("2", "2026-07-20", harga="1.00"),
        make_row("3", "2026-07-20", harga="2.00"),
    ])

    aggregate.aggregate(daily_dir, out_dir)
    meta = load(out_dir, "meta.json")
    assert meta["window"] == {"min": "2026-07-19", "max": "2026-07-20"}
    assert meta["row_count"] == 3

    catalog = load(out_dir, "catalog.json")
    assert catalog["dates"] == {"min": "2026-07-19", "max": "2026-07-20"}
