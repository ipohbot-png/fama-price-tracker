"""Unit tests for the DSR decoder. No network access."""

from __future__ import annotations

import datetime as _dt
import gzip
import importlib
import io
import json
import os

import pytest

from scraper import powerbi
from scraper.powerbi import (
    NULL_MASK_KEY,
    PowerBIClient,
    PowerBIError,
    QueryError,
    decode_data,
    decode_data_shape,
)


def seg(name, type_code, dict_name=None):
    out = {"N": name, "T": type_code}
    if dict_name:
        out["DN"] = dict_name
    return out


def descriptor(*names):
    return {
        "Select": [
            {"Value": "G%d" % i, "Name": name} for i, name in enumerate(names)
        ]
    }


def shape(segments, rows, value_dicts=None, **extra):
    first = dict(rows[0])
    first["S"] = segments
    out = {"N": "DS0", "PH": [{"DM0": [first] + [dict(r) for r in rows[1:]]}]}
    if value_dicts is not None:
        out["ValueDicts"] = value_dicts
    out.update(extra)
    return out


# --------------------------------------------------------------------------
# value dictionaries
# --------------------------------------------------------------------------
def test_value_dict_indices_are_resolved():
    ds = shape(
        [seg("G0", 1, "D0"), seg("G1", 1, "D1")],
        [{"C": [0, 1]}, {"C": [1, 0]}],
        {"D0": ["PERAK", "KEDAH"], "D1": ["Ladang", "Borong"]},
    )
    result = decode_data_shape(ds, descriptor("negeri", "peringkat"))
    assert result.columns == ["negeri", "peringkat"]
    assert result.rows == [["PERAK", "Borong"], ["KEDAH", "Ladang"]]


def test_literal_strings_bypass_the_value_dict():
    """Beyond the 100-entry dict cap the service inlines the literal string."""
    ds = shape(
        [seg("G0", 1, "D0")],
        [{"C": [0]}, {"C": ["30774101"]}, {"C": [1]}],
        {"D0": ["30774001", "30774002"]},
    )
    result = decode_data_shape(ds, descriptor("priceid"))
    assert [r[0] for r in result.rows] == ["30774001", "30774101", "30774002"]


def test_out_of_range_dict_index_raises():
    ds = shape([seg("G0", 1, "D0")], [{"C": [5]}], {"D0": ["a"]})
    with pytest.raises(PowerBIError, match="out of range"):
        decode_data_shape(ds, descriptor("x"))


def test_missing_value_dict_raises():
    ds = shape([seg("G0", 1, "D9")], [{"C": [0]}], {"D0": ["a"]})
    with pytest.raises(PowerBIError, match="missing ValueDict"):
        decode_data_shape(ds, descriptor("x"))


# --------------------------------------------------------------------------
# repeat bitmask
# --------------------------------------------------------------------------
def test_repeat_bitmask_repeats_previous_value():
    # 3 columns; row 2 repeats columns 0 and 2 (mask 0b101 = 5).
    ds = shape(
        [seg("G0", 1, "D0"), seg("G1", 4), seg("G2", 1, "D1")],
        [
            {"C": [0, 10, 0]},
            {"C": [11], "R": 5},
            {"C": [1, 12], "R": 4},
        ],
        {"D0": ["PERAK", "KEDAH"], "D1": ["Kilogram"]},
    )
    result = decode_data_shape(ds, descriptor("negeri", "n", "unit"))
    assert result.rows == [
        ["PERAK", 10, "Kilogram"],
        ["PERAK", 11, "Kilogram"],
        ["KEDAH", 12, "Kilogram"],
    ]


def test_repeat_bitmask_beyond_32_bits():
    segments = [seg("G%d" % i, 4) for i in range(40)]
    row0 = {"C": list(range(40))}
    # repeat every column except the last (bit 39 clear)
    mask = (1 << 39) - 1
    row1 = {"C": [999], "R": mask}
    ds = shape(segments, [row0, row1])
    result = decode_data_shape(ds, descriptor(*["c%d" % i for i in range(40)]))
    assert result.rows[1] == list(range(39)) + [999]


def test_unconsumed_values_raise():
    """Guards against mis-reading the bitmask semantics."""
    ds = shape([seg("G0", 4), seg("G1", 4)], [{"C": [1, 2, 3]}])
    with pytest.raises(PowerBIError, match="unconsumed"):
        decode_data_shape(ds, descriptor("a", "b"))


def test_too_few_values_raise():
    ds = shape([seg("G0", 4), seg("G1", 4)], [{"C": [1]}])
    with pytest.raises(PowerBIError, match="ran out of values"):
        decode_data_shape(ds, descriptor("a", "b"))


# --------------------------------------------------------------------------
# null bitmask
# --------------------------------------------------------------------------
def test_null_bitmask_marks_nulls_and_consumes_no_slot():
    # 3 columns, column 1 null (mask 0b010 = 2): C only carries 2 values.
    ds = shape(
        [seg("G0", 1, "D0"), seg("G1", 1, "D1"), seg("G2", 3)],
        [{"C": [0, 1.5], NULL_MASK_KEY: 2}],
        {"D0": ["PERAK"], "D1": []},
    )
    result = decode_data_shape(ds, descriptor("negeri", "supply", "harga"))
    assert result.rows == [["PERAK", None, 1.5]]


def test_repeat_of_a_null_stays_null():
    ds = shape(
        [seg("G0", 4), seg("G1", 1, "D0")],
        [{"C": [1], NULL_MASK_KEY: 2}, {"C": [2], "R": 2}],
        {"D0": []},
    )
    result = decode_data_shape(ds, descriptor("a", "supply"))
    assert result.rows == [[1, None], [2, None]]


def test_null_and_repeat_masks_combined():
    # 4 columns: col0 new, col1 repeats, col2 null, col3 new.
    ds = shape(
        [seg("G0", 4), seg("G1", 4), seg("G2", 4), seg("G3", 4)],
        [
            {"C": [1, 2, 3, 4]},
            {"C": [10, 40], "R": 0b0010, NULL_MASK_KEY: 0b0100},
        ],
    )
    result = decode_data_shape(ds, descriptor("a", "b", "c", "d"))
    assert result.rows == [[1, 2, 3, 4], [10, 2, None, 40]]


# --------------------------------------------------------------------------
# primitive types
# --------------------------------------------------------------------------
def test_epoch_ms_datetime_columns():
    ds = shape([seg("G0", 7)], [{"C": [1784937600000]}])
    result = decode_data_shape(ds, descriptor("tarikh harga"))
    value = result.rows[0][0]
    assert isinstance(value, _dt.datetime)
    assert value == _dt.datetime(2026, 7, 25, tzinfo=_dt.timezone.utc)
    assert value.strftime("%Y-%m-%d") == "2026-07-25"


def test_numbers_pass_through():
    ds = shape([seg("G0", 3), seg("G1", 4)], [{"C": [0.46, 2681]}])
    result = decode_data_shape(ds, descriptor("harga", "cnt"))
    assert result.rows == [[0.46, 2681]]


def test_high_precision_doubles_sent_as_strings_become_floats():
    """Live quirk: T=3 aggregate values arrive as JSON strings, or as ints."""
    ds = shape(
        [seg("G0", 1), seg("G1", 3)],
        [{"C": ["AYAM HIDUP", "7.0285714285714294"]}, {"C": ["BAWANG", 4]}],
    )
    result = decode_data_shape(ds, descriptor("varieti", "avg_harga"))
    assert result.rows[0][1] == pytest.approx(7.0285714285714294)
    assert isinstance(result.rows[0][1], float)
    assert result.rows[1][1] == 4


def test_non_numeric_string_in_a_double_column_is_left_alone():
    ds = shape([seg("G0", 3)], [{"C": ["n/a"]}])
    assert decode_data_shape(ds, descriptor("harga")).rows == [["n/a"]]


def test_string_typed_columns_keep_numeric_looking_text():
    """average14 is a text column: '30.46' must stay a string."""
    ds = shape([seg("G0", 1, "D0")], [{"C": [0]}], {"D0": ["30.46"]})
    assert decode_data_shape(ds, descriptor("average14")).rows == [["30.46"]]


def test_dicts_helper_yields_named_rows():
    ds = shape(
        [seg("G0", 1, "D0"), seg("G1", 3)],
        [{"C": [0, 1.0]}],
        {"D0": ["TELUR AYAM"]},
    )
    result = decode_data_shape(ds, descriptor("varieti", "harga"))
    assert list(result.dicts()) == [{"varieti": "TELUR AYAM", "harga": 1.0}]


def test_real_payload_shape_from_live_capture():
    """Row layout copied verbatim from a live 2026-07-25 response."""
    segments = [
        seg("G0", 1, "D0"), seg("G1", 7), seg("G2", 1, "D1"),
        seg("G3", 1, "D2"), seg("G4", 1, "D3"), seg("G5", 1, "D4"),
        seg("G6", 1, "D5"), seg("G7", 1, "D6"), seg("G8", 1, "D7"),
        seg("G9", 1, "D8"), seg("G10", 1, "D9"), seg("G11", 1, "D10"),
        seg("G12", 1, "D11"), seg("G13", 1, "D12"), seg("G14", 3),
        seg("G15", 1, "D13"), seg("G16", 1, "D14"),
    ]
    dicts = {
        "D0": ["30774001", "30774002"], "D1": ["2026-07-25 08:00:00.000000"],
        "D2": ["JOHOR"], "D3": ["BATU PAHAT"], "D4": ["PASAR"],
        "D5": ["Borong"], "D6": ["sub"], "D7": ["kat"], "D8": ["kum", "kum2"],
        "D9": ["jen", "jen2"], "D10": ["var", "var2"], "D11": ["gred"],
        "D12": ["Kilogram"], "D13": ["30.46", "9.55625"], "D14": [],
    }
    rows = [
        {"C": [0, 1784937600000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 30, 0],
         NULL_MASK_KEY: 65536},
        {"C": [1, 27], "R": 114686},
    ]
    result = decode_data_shape(shape(segments, rows, dicts), None)
    assert len(result.rows) == 2
    first, second = result.rows
    assert first[0] == "30774001"
    assert first[1] == _dt.datetime(2026, 7, 25, tzinfo=_dt.timezone.utc)
    assert first[14] == 30
    assert first[15] == "30.46"
    assert first[16] is None          # supply null via Ø bitmask
    # R=114686 clears bits 0 and 14 only -> priceid + harga are new values.
    assert second[0] == "30774002"
    assert second[14] == 27
    assert second[15] == "30.46"      # repeated
    assert second[16] is None         # repeated null


# --------------------------------------------------------------------------
# truncation signals
# --------------------------------------------------------------------------
def test_dlex_flags_truncation():
    ds = shape([seg("G0", 4)], [{"C": [1]}], DLEx=[{"N": "L0"}], IC=True)
    result = decode_data_shape(ds, descriptor("a"))
    assert result.data_limit_exceeded is True
    assert result.truncated is True


def test_has_all_data_means_not_truncated():
    ds = shape([seg("G0", 4)], [{"C": [1]}], IC=True, HAD=True)
    result = decode_data_shape(ds, descriptor("a"))
    assert result.truncated is False


def test_row_count_at_limit_flags_truncation():
    desc = descriptor("a")
    desc["Limits"] = {"Primary": {"Id": "L0", "Top": {"Count": 2}}}
    ds = shape([seg("G0", 4)], [{"C": [1]}, {"C": [2]}], HAD=True)
    result = decode_data_shape(ds, desc)
    assert result.row_limit == 2
    assert result.truncated is True


def test_restart_tokens_flag_truncation():
    ds = shape([seg("G0", 4)], [{"C": [1]}], RT=["tok"], HAD=True)
    assert decode_data_shape(ds, descriptor("a")).truncated is True


# --------------------------------------------------------------------------
# envelope handling
# --------------------------------------------------------------------------
def test_decode_data_accepts_both_ds_and_datashapes_keys():
    ds = shape([seg("G0", 4)], [{"C": [7]}], HAD=True)
    for key in ("DS", "DataShapes"):
        data = {"descriptor": descriptor("a"), "dsr": {key: [ds]}}
        assert decode_data(data).rows == [[7]]


def test_query_error_is_surfaced():
    bad = {
        "Id": "DS0",
        "odata.error": {
            "code": "rsDataShapeQueryTranslationError",
            "message": {"lang": "en-US", "value": "The function 'Sum' cannot be invoked"},
        },
    }
    with pytest.raises(QueryError, match="Sum"):
        decode_data({"dsr": {"DataShapes": [bad]}})


def test_missing_shapes_raises():
    with pytest.raises(PowerBIError, match="no data shapes"):
        decode_data({"dsr": {}})


def test_multiple_ph_buckets_are_concatenated():
    segments = [seg("G0", 4)]
    ds = {
        "N": "DS0",
        "PH": [
            {"DM0": [{"S": segments, "C": [1]}, {"C": [2]}]},
            {"DM0": [{"S": segments, "C": [3]}]},
        ],
    }
    result = decode_data_shape(ds, descriptor("a"))
    assert result.rows == [[1], [2], [3]]


# --------------------------------------------------------------------------
# HTTP client (stubbed opener — no network)
# --------------------------------------------------------------------------
class _FakeResponse(io.BytesIO):
    pass


def _envelope(ds, desc):
    return {
        "results": [
            {"result": {"data": {"descriptor": desc, "dsr": {"DS": [ds]}}}}
        ]
    }


def test_client_decompresses_gzip_without_asking():
    payload = _envelope(shape([seg("G0", 4)], [{"C": [42]}], HAD=True), descriptor("a"))
    blob = gzip.compress(json.dumps(payload).encode())
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return _FakeResponse(blob)

    client = PowerBIClient(opener=opener)
    result = client.run_command({"Query": {}, "Binding": {}})
    assert result.rows == [[42]]
    assert calls[0].get_header("X-powerbi-resourcekey") == powerbi.RESOURCE_KEY
    assert "fama-price-tracker" in calls[0].get_header("User-agent")


def test_client_retries_three_times_then_gives_up(monkeypatch):
    monkeypatch.setattr(powerbi.time, "sleep", lambda _s: None)
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        raise powerbi.urllib.error.URLError("boom")

    client = PowerBIClient(opener=opener, backoff=0.0)
    with pytest.raises(PowerBIError):
        client.run_command({"Query": {}, "Binding": {}})
    assert len(attempts) == 3


def test_client_recovers_on_second_attempt(monkeypatch):
    monkeypatch.setattr(powerbi.time, "sleep", lambda _s: None)
    payload = _envelope(shape([seg("G0", 4)], [{"C": [1]}], HAD=True), descriptor("a"))
    state = {"n": 0}

    def opener(request, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise powerbi.urllib.error.URLError("transient")
        return _FakeResponse(json.dumps(payload).encode())

    client = PowerBIClient(opener=opener, backoff=0.0)
    assert client.run_command({"Query": {}, "Binding": {}}).rows == [[1]]
    assert state["n"] == 2


def test_client_does_not_retry_client_errors(monkeypatch):
    monkeypatch.setattr(powerbi.time, "sleep", lambda _s: None)
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        raise powerbi.urllib.error.HTTPError(
            powerbi.API_URL, 400, "Bad Request", {}, io.BytesIO(b"nope")
        )

    client = PowerBIClient(opener=opener, backoff=0.0)
    with pytest.raises(PowerBIError, match="HTTP 400"):
        client.run_command({"Query": {}, "Binding": {}})
    assert len(attempts) == 1


def test_client_falls_back_to_binding_top_for_row_limit():
    payload = _envelope(
        shape([seg("G0", 4)], [{"C": [1]}, {"C": [2]}], HAD=True), descriptor("a")
    )
    client = PowerBIClient(opener=lambda r, timeout=None: _FakeResponse(
        json.dumps(payload).encode()))
    command = {
        "Query": {},
        "Binding": {"DataReduction": {"Primary": {"Top": {"Count": 2}}}},
    }
    result = client.run_command(command)
    assert result.row_limit == 2
    assert result.truncated is True


# --------------------------------------------------------------------------
# query builders
# --------------------------------------------------------------------------
def test_datetime_literal_format():
    cond = powerbi.datetime_equals("t", "tarikh harga", "2026-07-25")
    literal = cond["Condition"]["Comparison"]["Right"]["Literal"]["Value"]
    assert literal == "datetime'2026-07-25T00:00:00'"
    left = cond["Condition"]["Comparison"]["Left"]["Column"]
    assert left["Property"] == "tarikh harga"


def test_string_literal_escapes_quotes():
    cond = powerbi.string_equals("t", "negeri", "O'HARA")
    assert cond["Condition"]["Comparison"]["Right"]["Literal"]["Value"] == "'O''HARA'"


def test_detail_query_projects_every_column():
    command = powerbi.detail_query("E", ["a", "gred MID"], top=123)
    assert command["Query"]["Select"][1]["Column"]["Property"] == "gred MID"
    assert command["Binding"]["Primary"]["Groupings"][0]["Projections"] == [0, 1]
    assert command["Binding"]["DataReduction"]["Primary"]["Top"]["Count"] == 123
    assert "Where" not in command["Query"]


# --------------------------------------------------------------------------
# environment overrides for the report coordinates
# --------------------------------------------------------------------------
ENV_NAMES = ("FAMA_RESOURCE_KEY", "FAMA_MODEL_ID", "FAMA_DATASET_ID", "FAMA_ENTITY")


def test_env_str_prefers_environment_but_ignores_blanks(monkeypatch):
    monkeypatch.delenv("FAMA_ENTITY", raising=False)
    assert powerbi.env_str("FAMA_ENTITY", "default") == "default"
    monkeypatch.setenv("FAMA_ENTITY", "   ")
    assert powerbi.env_str("FAMA_ENTITY", "default") == "default"
    monkeypatch.setenv("FAMA_ENTITY", "  Other Entity  ")
    assert powerbi.env_str("FAMA_ENTITY", "default") == "Other Entity"


def test_env_int_falls_back_on_missing_blank_or_invalid(monkeypatch):
    monkeypatch.delenv("FAMA_MODEL_ID", raising=False)
    assert powerbi.env_int("FAMA_MODEL_ID", 7) == 7
    monkeypatch.setenv("FAMA_MODEL_ID", "")
    assert powerbi.env_int("FAMA_MODEL_ID", 7) == 7
    monkeypatch.setenv("FAMA_MODEL_ID", "not-a-number")
    assert powerbi.env_int("FAMA_MODEL_ID", 7) == 7
    monkeypatch.setenv("FAMA_MODEL_ID", " 42 ")
    assert powerbi.env_int("FAMA_MODEL_ID", 7) == 42


def test_module_constants_are_env_overridable():
    """The real constants are read from the environment at import time."""
    saved = {name: os.environ.get(name) for name in ENV_NAMES}
    os.environ.update({
        "FAMA_RESOURCE_KEY": "key-from-env",
        "FAMA_MODEL_ID": "99",
        "FAMA_DATASET_ID": "dataset-from-env",
        "FAMA_ENTITY": "API Harga (test)",
    })
    try:
        mod = importlib.reload(powerbi)
        assert mod.RESOURCE_KEY == "key-from-env"
        assert mod.MODEL_ID == 99
        assert mod.DATASET_ID == "dataset-from-env"
        assert mod.ENTITY == "API Harga (test)"
        # The client picks them up through its defaults.
        client = mod.PowerBIClient()
        assert client.resource_key == "key-from-env"
        assert client.model_id == 99
        assert client.dataset_id == "dataset-from-env"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        importlib.reload(powerbi)


def test_constants_fall_back_to_the_documented_defaults():
    assert powerbi.RESOURCE_KEY == powerbi.DEFAULT_RESOURCE_KEY == "b41dccd7-d9f7-4f56-80fe-127696493f53"
    assert powerbi.MODEL_ID == powerbi.DEFAULT_MODEL_ID == 6546643
    assert powerbi.DATASET_ID == powerbi.DEFAULT_DATASET_ID == "185b7047-f327-4ef7-897a-3168956a1850"
    assert powerbi.ENTITY == powerbi.DEFAULT_ENTITY == "API Harga (30hari)"
