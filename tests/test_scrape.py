"""Unit tests for CSV merge / formatting logic. No network access."""

from __future__ import annotations

import csv
import datetime as _dt
import os

import pytest

from scraper import scrape
from scraper.scrape import (
    CSV_COLUMNS,
    SOURCE_COLUMNS,
    format_value,
    merge_into_file,
    merge_records,
    priceid_sort_key,
    read_csv,
    row_to_record,
    write_csv,
)


def make_record(priceid, **overrides):
    record = {col: "" for col in CSV_COLUMNS}
    record["priceid"] = priceid
    record["tarikh_harga"] = "2026-07-25"
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# Contract A shape
# --------------------------------------------------------------------------
def test_csv_columns_match_contract_a():
    assert CSV_COLUMNS == [
        "priceid", "tarikh_harga", "systemdate", "negeri", "daerah", "lokasi",
        "peringkat", "sublevel", "kategori", "kumpulan", "jenis", "varieti",
        "gred", "unit", "harga", "average14", "supply",
    ]


def test_source_columns_use_exact_property_names():
    assert "gred MID" in SOURCE_COLUMNS
    assert "tarikh harga" in SOURCE_COLUMNS
    assert "gred" not in SOURCE_COLUMNS


# --------------------------------------------------------------------------
# value formatting
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, ""),
        ("PERAK", "PERAK"),
        (0.46, "0.46"),
        (30, "30"),
        (30.0, "30"),
        (9.55625, "9.55625"),
        (_dt.datetime(2026, 7, 25, tzinfo=_dt.timezone.utc), "2026-07-25"),
    ],
)
def test_format_value(value, expected):
    assert format_value(value) == expected


def test_row_to_record_renames_gred_mid_and_tarikh():
    row = {
        "priceid": "30774001",
        "tarikh harga": _dt.datetime(2026, 7, 25, tzinfo=_dt.timezone.utc),
        "gred MID": "A",
        "harga": 0.46,
        "supply": None,
    }
    record = row_to_record(row)
    assert record["tarikh_harga"] == "2026-07-25"
    assert record["gred"] == "A"
    assert record["harga"] == "0.46"
    assert record["supply"] == ""
    assert set(record) == set(CSV_COLUMNS)


# --------------------------------------------------------------------------
# sorting
# --------------------------------------------------------------------------
def test_numeric_priceids_sort_numerically_not_lexicographically():
    ids = ["100", "99", "1000", "9"]
    assert sorted(ids, key=priceid_sort_key) == ["9", "99", "100", "1000"]


def test_non_numeric_priceids_sort_after_numeric_and_stay_deterministic():
    ids = ["b", "10", "a", "2"]
    assert sorted(ids, key=priceid_sort_key) == ["2", "10", "a", "b"]


# --------------------------------------------------------------------------
# merge semantics
# --------------------------------------------------------------------------
def test_merge_adds_new_and_overwrites_existing_by_priceid():
    existing = {
        "2": make_record("2", harga="1.00"),
        "10": make_record("10", harga="2.00"),
    }
    incoming = [
        make_record("2", harga="1.50"),      # correction
        make_record("3", harga="3.00"),      # new
    ]
    merged = merge_records(existing, incoming)
    assert [r["priceid"] for r in merged] == ["2", "3", "10"]
    assert merged[0]["harga"] == "1.50"
    assert merged[2]["harga"] == "2.00"


def test_merge_drops_rows_without_priceid():
    merged = merge_records({}, [make_record(""), make_record("1")])
    assert [r["priceid"] for r in merged] == ["1"]


def test_merge_result_has_exactly_the_contract_columns():
    merged = merge_records({}, [{"priceid": "1", "harga": "0.5"}])
    assert set(merged[0]) == set(CSV_COLUMNS)
    assert merged[0]["negeri"] == ""


# --------------------------------------------------------------------------
# file round trip + idempotency
# --------------------------------------------------------------------------
def test_write_then_read_round_trips(tmp_path):
    path = str(tmp_path / "2026-07-25.csv")
    records = [make_record("1", negeri="PERAK", harga="0.46")]
    write_csv(path, records)
    assert read_csv(path) == {"1": records[0]}


def test_read_missing_file_returns_empty(tmp_path):
    assert read_csv(str(tmp_path / "nope.csv")) == {}


def test_merge_into_file_is_idempotent(tmp_path):
    path = str(tmp_path / "2026-07-25.csv")
    records = [make_record("10", harga="1"), make_record("2", harga="2")]

    first = merge_into_file(path, records)
    assert first["changed"] is True
    assert first["after"] == 2
    body = open(path, encoding="utf-8").read()

    second = merge_into_file(path, records)
    assert second["changed"] is False
    assert second["added"] == 0
    assert open(path, encoding="utf-8").read() == body


def test_merge_into_file_self_heals_and_corrects(tmp_path):
    path = str(tmp_path / "2026-07-25.csv")
    merge_into_file(path, [make_record("1", harga="1.00")])
    stats = merge_into_file(
        path, [make_record("1", harga="1.25"), make_record("2", harga="9")]
    )
    assert stats["added"] == 1
    assert stats["after"] == 2
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert [r["priceid"] for r in rows] == ["1", "2"]
    assert rows[0]["harga"] == "1.25"


def test_written_file_is_sorted_by_priceid(tmp_path):
    path = str(tmp_path / "d.csv")
    merge_into_file(path, [make_record(p) for p in ("300", "9", "1000", "45")])
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert [r["priceid"] for r in rows] == ["9", "45", "300", "1000"]


def test_header_row_matches_contract(tmp_path):
    path = str(tmp_path / "d.csv")
    merge_into_file(path, [make_record("1")])
    with open(path, encoding="utf-8", newline="") as handle:
        assert next(csv.reader(handle)) == CSV_COLUMNS


def test_utf8_and_embedded_commas_survive(tmp_path):
    path = str(tmp_path / "d.csv")
    merge_into_file(
        path, [make_record("1", lokasi='PASAR "BESAR", SITIAWAN', varieti="TERUNG PANJANG")]
    )
    assert read_csv(path)["1"]["lokasi"] == 'PASAR "BESAR", SITIAWAN'


def test_no_temp_file_left_behind(tmp_path):
    path = str(tmp_path / "d.csv")
    merge_into_file(path, [make_record("1")])
    assert os.listdir(str(tmp_path)) == ["d.csv"]


# --------------------------------------------------------------------------
# fetch_date orchestration (stubbed client, no network)
# --------------------------------------------------------------------------
class _StubResult:
    def __init__(self, rows, truncated=False):
        self._rows = rows
        self._truncated = truncated
        self.data_limit_exceeded = truncated
        self.has_all_data = not truncated
        self.restart_tokens = None
        self.row_limit = 30000

    @property
    def truncated(self):
        return self._truncated

    def dicts(self):
        return iter(self._rows)


def _src_row(priceid, negeri):
    return {
        "priceid": priceid,
        "tarikh harga": _dt.datetime(2026, 7, 25, tzinfo=_dt.timezone.utc),
        "negeri": negeri,
        "harga": 1.0,
    }


def test_fetch_date_single_pass_when_counts_agree(monkeypatch):
    calls = []

    def fake_fetch_rows(client, where, top, entity=None):
        calls.append(where)
        rows = [_src_row("1", "PERAK"), _src_row("2", "KEDAH")]
        return [row_to_record(r) for r in rows], _StubResult(rows)

    monkeypatch.setattr(scrape, "fetch_rows", fake_fetch_rows)
    records, info = scrape.fetch_date(None, "2026-07-25", expected=2, log=lambda *_: None)
    assert len(records) == 2
    assert info["chunked"] is False
    assert len(calls) == 1


def test_fetch_date_chunks_on_truncation(monkeypatch):
    def fake_fetch_rows(client, where, top, entity=None):
        # Presence of a second condition == the per-negeri chunk query.
        if len(where) == 1:
            rows = [_src_row("1", "PERAK")]
            return [row_to_record(r) for r in rows], _StubResult(rows, truncated=True)
        negeri = where[1]["Condition"]["Comparison"]["Right"]["Literal"]["Value"]
        negeri = negeri.strip("'")
        rows = [_src_row("1", "PERAK")] if negeri == "PERAK" else [_src_row("2", "KEDAH")]
        return [row_to_record(r) for r in rows], _StubResult(rows)

    monkeypatch.setattr(scrape, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(
        scrape, "fetch_negeri_counts",
        lambda client, date_str, entity=None: [("PERAK", 1), ("KEDAH", 1)],
    )
    records, info = scrape.fetch_date(None, "2026-07-25", expected=2, log=lambda *_: None)
    assert info["chunked"] is True
    assert sorted(r["priceid"] for r in records) == ["1", "2"]
    assert info["chunk_mismatches"] == []


def test_fetch_date_chunks_on_count_mismatch_and_reports_residual(monkeypatch):
    def fake_fetch_rows(client, where, top, entity=None):
        rows = [_src_row("1", "PERAK")]
        return [row_to_record(r) for r in rows], _StubResult(rows)

    monkeypatch.setattr(scrape, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(
        scrape, "fetch_negeri_counts",
        lambda client, date_str, entity=None: [("PERAK", 5)],
    )
    records, info = scrape.fetch_date(None, "2026-07-25", expected=5, log=lambda *_: None)
    assert info["chunked"] is True
    assert info["chunk_mismatches"] == [("PERAK", 5, 1)]
