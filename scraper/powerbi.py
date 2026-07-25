"""Power BI "publish to web" querydata client + DSR response decoder.

Pure standard library (urllib, gzip, json). No third-party dependencies.

The FAMA report at https://www.fama.gov.my/harga-pasaran-terkini embeds a public
Power BI report whose semantic-query endpoint is reachable without auth as long
as the resource key is sent in the ``X-PowerBI-ResourceKey`` header.

DSR (Data Shape Result) encoding notes, verified empirically against the live
endpoint on 2026-07-25:

* ``ValueDicts`` maps a dictionary name (``D0``…``Dn``) to a list of strings.
  **The service caps each dictionary at 100 entries.** Values beyond the cap are
  emitted inline in ``C`` as literal strings, so a dict-encoded column can carry
  either an ``int`` (index into its dictionary) or a ``str`` (literal value).
* Every row is an object with a compressed ``C`` array holding only the values
  that are neither repeated nor null.
* ``R`` is the repeat bitmask: bit *i* set means column *i* repeats the previous
  row's (already resolved) value.
* ``Ø`` (U+00D8) is the null bitmask: bit *i* set means column *i* is null.
* Columns flagged by ``R`` or ``Ø`` consume **no** slot in ``C``.
* The first row carries an ``S`` array describing each column: ``N`` (the
  descriptor key, e.g. ``G0``/``M0``), ``T`` (type code) and optionally ``DN``
  (its value dictionary).
* Type codes seen here: 1 = string, 3 = double, 4 = int64, 7 = datetime
  (epoch milliseconds, UTC).

Truncation signals on a data shape:

* ``DLEx`` present  -> a data-reduction limit was hit (rows were dropped).
* ``HAD``  truthy   -> the shape carries all data.
* ``RT``            -> restart tokens for continuation.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import random
import time
import urllib.error
import urllib.request

__all__ = [
    "PowerBIError",
    "QueryError",
    "PowerBIClient",
    "QueryResult",
    "decode_data",
    "decode_data_shape",
    "column_ref",
    "aggregation",
    "datetime_equals",
    "string_equals",
    "detail_query",
    "API_URL",
    "RESOURCE_KEY",
    "MODEL_ID",
    "DATASET_ID",
    "ENTITY",
]

# --------------------------------------------------------------------------
# FAMA report coordinates (see DESIGN.md section 1)
# --------------------------------------------------------------------------
API_URL = (
    "https://wabi-south-east-asia-api.analysis.windows.net"
    "/public/reports/querydata?synchronous=true"
)
RESOURCE_KEY = "b41dccd7-d9f7-4f56-80fe-127696493f53"
MODEL_ID = 6546643
DATASET_ID = "185b7047-f327-4ef7-897a-3168956a1850"
ENTITY = "API Harga (30hari)"

USER_AGENT = (
    "fama-price-tracker/1.0 (+https://github.com/; open-data archiver; "
    "polite, low rate)"
)

#: Null bitmask key used by DSR payloads (LATIN CAPITAL LETTER O WITH STROKE).
NULL_MASK_KEY = "Ø"
#: Repeat bitmask key.
REPEAT_MASK_KEY = "R"

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


class PowerBIError(RuntimeError):
    """Transport / protocol level failure."""


class QueryError(PowerBIError):
    """The service accepted the request but rejected the query."""


# --------------------------------------------------------------------------
# Semantic query building helpers
# --------------------------------------------------------------------------
def column_ref(source: str, prop: str, name: str | None = None) -> dict:
    """A ``Select`` entry projecting a raw column."""
    return {
        "Column": {
            "Expression": {"SourceRef": {"Source": source}},
            "Property": prop,
        },
        "Name": name or prop,
    }


def aggregation(source: str, prop: str, function: int, name: str) -> dict:
    """A ``Select`` entry projecting an aggregate.

    ``function``: 0=Sum, 1=Avg, 2=Min, 3=Max, 5=CountNonNull.
    """
    return {
        "Aggregation": {
            "Expression": {
                "Column": {
                    "Expression": {"SourceRef": {"Source": source}},
                    "Property": prop,
                }
            },
            "Function": function,
        },
        "Name": name,
    }


def datetime_equals(source: str, prop: str, date_str: str) -> dict:
    """``Where`` condition ``prop == datetime'YYYY-MM-DDT00:00:00'``.

    The literal format is the one the service accepts (verified empirically).
    """
    return {
        "Condition": {
            "Comparison": {
                "ComparisonKind": 0,
                "Left": {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": source}},
                        "Property": prop,
                    }
                },
                "Right": {"Literal": {"Value": "datetime'%sT00:00:00'" % date_str}},
            }
        }
    }


def string_equals(source: str, prop: str, value: str) -> dict:
    """``Where`` condition ``prop == 'value'``."""
    escaped = value.replace("'", "''")
    return {
        "Condition": {
            "Comparison": {
                "ComparisonKind": 0,
                "Left": {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": source}},
                        "Property": prop,
                    }
                },
                "Right": {"Literal": {"Value": "'%s'" % escaped}},
            }
        }
    }


def detail_query(
    entity: str,
    properties,
    where=None,
    top: int = 30000,
    source: str = "t",
) -> dict:
    """Build a ``SemanticQueryDataShapeCommand`` projecting raw columns."""
    select = [column_ref(source, p) for p in properties]
    query = {
        "Version": 2,
        "From": [{"Name": source, "Entity": entity, "Type": 0}],
        "Select": select,
    }
    if where:
        query["Where"] = list(where)
    return {
        "Query": query,
        "Binding": {
            "Primary": {"Groupings": [{"Projections": list(range(len(select)))}]},
            "DataReduction": {"DataVolume": 3, "Primary": {"Top": {"Count": top}}},
            "Version": 1,
        },
    }


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------
class QueryResult:
    """Decoded rows plus the truncation signals that came with them."""

    __slots__ = (
        "columns",
        "rows",
        "data_limit_exceeded",
        "has_all_data",
        "restart_tokens",
        "row_limit",
    )

    def __init__(
        self,
        columns,
        rows,
        data_limit_exceeded=False,
        has_all_data=None,
        restart_tokens=None,
        row_limit=None,
    ):
        self.columns = list(columns)
        self.rows = rows
        self.data_limit_exceeded = data_limit_exceeded
        self.has_all_data = has_all_data
        self.restart_tokens = restart_tokens
        self.row_limit = row_limit

    def __len__(self):
        return len(self.rows)

    def dicts(self):
        """Yield each row as a ``{column_name: value}`` dict."""
        cols = self.columns
        for row in self.rows:
            yield dict(zip(cols, row))

    @property
    def truncated(self) -> bool:
        """True when the service signalled that rows were dropped."""
        if self.data_limit_exceeded:
            return True
        if self.restart_tokens:
            return True
        if self.has_all_data is False:
            return True
        if self.row_limit is not None and len(self.rows) >= self.row_limit:
            return True
        return False

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<QueryResult rows=%d truncated=%s>" % (len(self.rows), self.truncated)


def _epoch_ms_to_datetime(value):
    return _EPOCH + _dt.timedelta(milliseconds=value)


def _coerce(value, seg, value_dicts):
    """Resolve one raw ``C`` entry using its column descriptor."""
    dict_name = seg.get("DN")
    if dict_name is not None and isinstance(value, int) and not isinstance(value, bool):
        try:
            table = value_dicts[dict_name]
        except KeyError:
            raise PowerBIError("missing ValueDict %r" % dict_name) from None
        try:
            return table[value]
        except IndexError:
            raise PowerBIError(
                "ValueDict %s index %d out of range (size %d)"
                % (dict_name, value, len(table))
            ) from None
    type_code = seg.get("T")
    if type_code == 7 and isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_ms_to_datetime(value)
    if type_code == 3 and isinstance(value, str):
        # Power BI serialises high-precision doubles (typically aggregate
        # results) as JSON strings to avoid precision loss. Normalise them so a
        # numeric column always yields a number.
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _decode_rows(raw_rows, value_dicts):
    """Expand DSR compressed rows into full python lists."""
    segments = None
    previous = None
    out = []
    for raw in raw_rows:
        if "S" in raw:
            segments = raw["S"]
            previous = None  # a new segment layout restarts repeat tracking
        if segments is None:
            raise PowerBIError("DSR rows started without an 'S' descriptor")
        ncols = len(segments)
        repeat_mask = raw.get(REPEAT_MASK_KEY, 0) or 0
        null_mask = raw.get(NULL_MASK_KEY, 0) or 0
        values = raw.get("C", ())
        row = [None] * ncols
        cursor = 0
        for i, seg in enumerate(segments):
            if (repeat_mask >> i) & 1:
                row[i] = previous[i] if previous is not None else None
            elif (null_mask >> i) & 1:
                row[i] = None
            else:
                if cursor >= len(values):
                    raise PowerBIError(
                        "DSR row ran out of values: need column %d of %d, "
                        "C has %d entries (R=%d, null=%d)"
                        % (i, ncols, len(values), repeat_mask, null_mask)
                    )
                row[i] = _coerce(values[cursor], seg, value_dicts)
                cursor += 1
        if cursor != len(values):
            raise PowerBIError(
                "DSR row had %d unconsumed values in C (consumed %d of %d)"
                % (len(values) - cursor, cursor, len(values))
            )
        previous = row
        out.append(row)
    return out


def _name_map(descriptor):
    """Map DSR segment keys (``G0``/``M0``) to human column names."""
    mapping = {}
    for entry in (descriptor or {}).get("Select", []):
        key = entry.get("Value")
        if key:
            mapping[key] = entry.get("Name", key)
    return mapping


def decode_data_shape(shape, descriptor=None):
    """Decode a single ``dsr.DS`` / ``dsr.DataShapes`` entry."""
    error = shape.get("odata.error")
    if error:
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        raise QueryError(message or json.dumps(error, ensure_ascii=False))

    value_dicts = shape.get("ValueDicts") or {}
    raw_rows = []
    for bucket in shape.get("PH") or []:
        for key in sorted(bucket):
            value = bucket[key]
            if isinstance(value, list):
                raw_rows.extend(value)

    rows = _decode_rows(raw_rows, value_dicts)

    columns = []
    if raw_rows and "S" in raw_rows[0]:
        names = _name_map(descriptor)
        for seg in raw_rows[0]["S"]:
            key = seg.get("N")
            columns.append(names.get(key, key))

    primary_limit = ((descriptor or {}).get("Limits") or {}).get("Primary") or {}
    top = (primary_limit.get("Top") or {}).get("Count")
    row_limit = top if isinstance(top, int) else None

    return QueryResult(
        columns=columns,
        rows=rows,
        data_limit_exceeded=bool(shape.get("DLEx")),
        has_all_data=shape.get("HAD"),
        restart_tokens=shape.get("RT"),
        row_limit=row_limit,
    )


def decode_data(data):
    """Decode ``results[i].result.data`` from a querydata response."""
    dsr = data.get("dsr") or {}
    shapes = dsr.get("DS")
    if shapes is None:
        shapes = dsr.get("DataShapes")
    if not shapes:
        raise PowerBIError("response contained no data shapes: %s" % list(dsr))
    return decode_data_shape(shapes[0], data.get("descriptor"))


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class PowerBIClient:
    """Minimal public-report querydata client with retry/backoff."""

    def __init__(
        self,
        resource_key: str = RESOURCE_KEY,
        model_id: int = MODEL_ID,
        dataset_id: str = DATASET_ID,
        api_url: str = API_URL,
        timeout: float = 120.0,
        attempts: int = 3,
        backoff: float = 2.0,
        min_interval: float = 0.0,
        user_agent: str = USER_AGENT,
        opener=None,
    ):
        self.resource_key = resource_key
        self.model_id = model_id
        self.dataset_id = dataset_id
        self.api_url = api_url
        self.timeout = timeout
        self.attempts = attempts
        self.backoff = backoff
        self.min_interval = min_interval
        self.user_agent = user_agent
        self._opener = opener or urllib.request.urlopen
        self._last_call = 0.0

    # -- transport ---------------------------------------------------------
    def _sleep_for_rate_limit(self):
        if self.min_interval <= 0:
            return
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "X-PowerBI-ResourceKey": self.resource_key,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": self.user_agent,
        }
        last_error = None
        for attempt in range(1, self.attempts + 1):
            self._sleep_for_rate_limit()
            request = urllib.request.Request(self.api_url, data=body, headers=headers)
            try:
                response = self._opener(request, timeout=self.timeout)
                try:
                    raw = response.read()
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                self._last_call = time.monotonic()
                # The service gzips regardless of Accept-Encoding: sniff.
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._last_call = time.monotonic()
                detail = b""
                try:
                    detail = exc.read()[:2000]
                except Exception:  # pragma: no cover - defensive
                    pass
                last_error = PowerBIError(
                    "HTTP %s from querydata: %s"
                    % (exc.code, detail.decode("utf-8", "replace"))
                )
                # 4xx other than 429 will not get better by retrying.
                if exc.code < 500 and exc.code != 429:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                self._last_call = time.monotonic()
                last_error = PowerBIError("querydata request failed: %r" % (exc,))
            if attempt < self.attempts:
                delay = self.backoff * (2 ** (attempt - 1))
                delay += random.uniform(0, 0.25 * delay)
                time.sleep(delay)
        raise last_error or PowerBIError("querydata request failed")

    # -- queries -----------------------------------------------------------
    def run_command(self, command: dict) -> QueryResult:
        """Run one ``SemanticQueryDataShapeCommand`` and decode the result."""
        payload = {
            "version": "1.0.0",
            "queries": [
                {
                    "Query": {"Commands": [{"SemanticQueryDataShapeCommand": command}]},
                    "QueryId": "",
                    "ApplicationContext": {"DatasetId": self.dataset_id},
                }
            ],
            "cancelQueries": [],
            "modelId": self.model_id,
        }
        response = self._post(payload)
        results = response.get("results")
        if not results:
            raise PowerBIError(
                "no results in response: %s"
                % json.dumps(response, ensure_ascii=False)[:800]
            )
        result = results[0].get("result") or {}
        if "error" in results[0]:
            raise QueryError(
                json.dumps(results[0]["error"], ensure_ascii=False)[:800]
            )
        data = result.get("data")
        if data is None:
            raise PowerBIError(
                "no data in result: %s" % json.dumps(result, ensure_ascii=False)[:800]
            )
        decoded = decode_data(data)
        if decoded.row_limit is None:
            top = (
                command.get("Binding", {})
                .get("DataReduction", {})
                .get("Primary", {})
                .get("Top", {})
                .get("Count")
            )
            if isinstance(top, int):
                decoded.row_limit = top
        return decoded
