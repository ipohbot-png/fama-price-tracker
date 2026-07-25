"""Unit tests for CSV merge / formatting logic. No network access."""

from __future__ import annotations

import csv
import datetime as _dt
import os

import pytest

from scraper import scrape
from scraper.scrape import (
    PowerBIError,
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


def test_merge_keeps_archived_value_when_incoming_field_is_empty():
    """A degraded upstream response must never blank good archived data."""
    existing = {"1": make_record("1", negeri="PERAK", harga="1.00", lokasi="PASAR1")}
    incoming = [make_record("1", negeri="", harga="", lokasi="")]

    merged = merge_records(existing, incoming)
    assert merged[0]["negeri"] == "PERAK"
    assert merged[0]["harga"] == "1.00"
    assert merged[0]["lokasi"] == "PASAR1"


def test_merge_still_overwrites_with_non_empty_incoming_values():
    existing = {"1": make_record("1", harga="1.00", negeri="PERAK")}
    incoming = [make_record("1", harga="1.25", negeri="")]

    merged = merge_records(existing, incoming)
    assert merged[0]["harga"] == "1.25"   # correction applied
    assert merged[0]["negeri"] == "PERAK"  # empty did not blank it


def test_merge_reports_preserved_field_and_row_counts():
    existing = {
        "1": make_record("1", negeri="PERAK", harga="1.00"),
        "2": make_record("2", negeri="KEDAH", harga="2.00"),
    }
    incoming = [
        make_record("1", negeri="", harga=""),      # 2 fields preserved
        make_record("2", negeri="KEDAH", harga=""),  # 1 field preserved
        make_record("3", negeri="", harga=""),      # new row: nothing to preserve
    ]
    stats = {}
    merge_records(existing, incoming, stats=stats)
    assert stats == {"preserved_fields": 3, "preserved_rows": 2}


def test_merge_preserves_nothing_when_incoming_is_complete():
    existing = {"1": make_record("1", negeri="PERAK", harga="1.00")}
    stats = {}
    merge_records(existing, [make_record("1", negeri="PERAK", harga="1.10")], stats=stats)
    assert stats == {"preserved_fields": 0, "preserved_rows": 0}


def test_whitespace_only_incoming_counts_as_empty():
    existing = {"1": make_record("1", negeri="PERAK")}
    merged = merge_records(existing, [make_record("1", negeri="   ")])
    assert merged[0]["negeri"] == "PERAK"


def test_merge_into_file_does_not_blank_archived_row(tmp_path):
    path = str(tmp_path / "2026-07-25.csv")
    merge_into_file(path, [make_record("1", negeri="PERAK", harga="1.00")])

    # Upstream returns the row with everything blanked out.
    stats = merge_into_file(path, [make_record("1", negeri="", harga="")])
    assert stats["preserved_fields"] == 2
    assert stats["preserved_rows"] == 1
    assert stats["changed"] is False          # nothing was lost -> nothing rewritten
    assert read_csv(path)["1"]["harga"] == "1.00"
    assert read_csv(path)["1"]["negeri"] == "PERAK"


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


# --------------------------------------------------------------------------
# run() verification + exit codes (stubbed client, no network)
#
# Exit codes: 0 = OK (archive-superset included), 1 = fetched != control,
#             2 = fetch/transport/decode failure.
# --------------------------------------------------------------------------
def stub_run(monkeypatch, date_counts, fetch_impl):
    monkeypatch.setattr(scrape, "PowerBIClient", lambda **kwargs: object())
    monkeypatch.setattr(scrape, "fetch_date_counts", lambda client: date_counts)
    monkeypatch.setattr(scrape, "fetch_date", fetch_impl)


def _fetch_ok(rows, chunk_mismatches=()):
    def impl(client, date_str, expected=None, top=30000, entity=None, log=print):
        info = {
            "date": date_str, "expected": expected, "chunked": False,
            "chunk_mismatches": list(chunk_mismatches), "rows": len(rows),
        }
        return list(rows), info
    return impl


def test_run_exits_zero_when_upstream_deleted_rows_the_archive_still_has(
    tmp_path, monkeypatch, capsys
):
    """Regression for the pipeline-killing bug.

    The archive is a superset of upstream: a row deleted upstream stays in our
    committed CSV forever, so file (3) > control (2). That must be a note, not
    a failure -- the old code compared the merged file count against the control
    count and exited 1 on every subsequent run, skipping the aggregate/commit
    steps of the daily workflow.
    """
    path = str(tmp_path / "2026-07-25.csv")
    write_csv(path, [make_record(p) for p in ("1", "2", "3")])

    # Upstream now serves only rows 1 and 2 -- row 3 was deleted upstream.
    stub_run(monkeypatch, [("2026-07-25", 2)],
             _fetch_ok([make_record("1"), make_record("2")]))

    code = scrape.run(["--out-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert code == 0
    assert "archive is a superset" in out.out
    # The deleted row is still archived.
    assert set(read_csv(path)) == {"1", "2", "3"}


def test_run_exits_one_when_fetch_disagrees_with_control(tmp_path, monkeypatch, capsys):
    stub_run(monkeypatch, [("2026-07-25", 5)],
             _fetch_ok([make_record("1"), make_record("2")]))

    code = scrape.run(["--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "control=5 fetched=2" in err


def test_run_exits_one_on_per_negeri_chunk_mismatch(tmp_path, monkeypatch, capsys):
    stub_run(monkeypatch, [("2026-07-25", 1)],
             _fetch_ok([make_record("1")], chunk_mismatches=[("PERAK", 5, 1)]))

    code = scrape.run(["--out-dir", str(tmp_path)])
    assert code == 1
    assert "per-negeri mismatches" in capsys.readouterr().err


def test_run_exits_zero_when_everything_matches(tmp_path, monkeypatch, capsys):
    stub_run(monkeypatch, [("2026-07-25", 2)],
             _fetch_ok([make_record("1"), make_record("2")]))

    code = scrape.run(["--out-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "MISMATCH" not in out


def test_run_exits_two_when_control_aggregation_fails(tmp_path, monkeypatch, capsys):
    def boom(client):
        raise PowerBIError("HTTP 503 from querydata")

    monkeypatch.setattr(scrape, "PowerBIClient", lambda **kwargs: object())
    monkeypatch.setattr(scrape, "fetch_date_counts", boom)

    code = scrape.run(["--out-dir", str(tmp_path)])
    assert code == 2
    assert "control aggregation failed" in capsys.readouterr().err


def test_run_exits_two_when_a_date_fetch_fails(tmp_path, monkeypatch, capsys):
    def boom(client, date_str, expected=None, top=30000, entity=None, log=print):
        raise PowerBIError("querydata request failed")

    stub_run(monkeypatch, [("2026-07-25", 2)], boom)

    code = scrape.run(["--out-dir", str(tmp_path)])
    assert code == 2
    assert "fetch failed for 2026-07-25" in capsys.readouterr().err


def test_run_exits_two_when_source_returns_no_dates(tmp_path, monkeypatch, capsys):
    stub_run(monkeypatch, [], _fetch_ok([]))
    assert scrape.run(["--out-dir", str(tmp_path)]) == 2


def test_run_warns_but_succeeds_when_archived_fields_are_preserved(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "2026-07-25.csv")
    write_csv(path, [make_record("1", negeri="PERAK", harga="1.00")])

    # Degraded response: right row count, blanked columns.
    stub_run(monkeypatch, [("2026-07-25", 1)],
             _fetch_ok([make_record("1", negeri="", harga="")]))

    code = scrape.run(["--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 0
    assert "kept 2 archived field value(s)" in err
    assert read_csv(path)["1"]["harga"] == "1.00"
