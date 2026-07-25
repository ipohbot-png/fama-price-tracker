"""Fetch the FAMA rolling price window into ``data/daily/YYYY-MM-DD.csv``.

Usage::

    python -m scraper.scrape                 # full rolling window
    python -m scraper.scrape --dates 2026-07-25
    python -m scraper.scrape --limit-dates 3 --dry-run

Strategy
--------
1. Discover the available price dates with a grouped count aggregation
   (``COUNTNONNULL(priceid)`` grouped by ``tarikh harga``). That same query
   doubles as the **control total** for verification.
2. For each date, fetch every detail row with a single query capped at
   ``--top`` (default 30000). If the response signals truncation
   (``DLEx`` / missing ``HAD`` / ``RT`` / row count at the cap) or the row
   count disagrees with the control count, re-fetch that date chunked by
   ``negeri``.
3. Merge into ``data/daily/<date>.csv`` keyed on ``priceid`` — existing rows are
   overwritten with the freshly fetched values, new rows appended, output sorted
   by ``priceid``. Re-running with unchanged upstream data is a no-op.
   **Empty incoming fields never blank a non-empty archived field** (see
   :func:`merge_records`): the archive only ever gains information.

Verification & exit codes
-------------------------
The archive is a superset of the upstream rolling window: upstream rows can be
deleted (or the window can roll past them) while our committed CSV keeps them
forever. So the archive row count is **not** a verification signal — comparing
it against the upstream control count made every upstream deletion a permanent
hard failure. Verification therefore compares the **fetch** against the control
aggregation, and the file count is logged for information only:

* ``0`` — OK. ``len(fetched rows) == control count`` for every selected date.
  This includes the archive-superset case (file row count > control count),
  which is logged as an informational note, not an anomaly.
* ``1`` — genuine verification mismatch: a date's fetched row count disagrees
  with the control aggregation, or a per-``negeri`` chunk disagreed with its own
  control count. The data we just fetched is incomplete/unexpected.
* ``2`` — fetch/transport/decode failure (:class:`~scraper.powerbi.PowerBIError`,
  including :class:`~scraper.powerbi.QueryError`), or the source returned no
  dates at all. Nothing was verified because nothing usable arrived.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys

from .powerbi import (
    ENTITY,
    PowerBIClient,
    PowerBIError,
    aggregation,
    column_ref,
    datetime_equals,
    detail_query,
    string_equals,
)

#: Source column -> CSV field (Contract A, DESIGN.md section 3).
FIELD_MAP = [
    ("priceid", "priceid"),
    ("tarikh harga", "tarikh_harga"),
    ("systemdate", "systemdate"),
    ("negeri", "negeri"),
    ("daerah", "daerah"),
    ("lokasi", "lokasi"),
    ("peringkat", "peringkat"),
    ("sublevel", "sublevel"),
    ("kategori", "kategori"),
    ("kumpulan", "kumpulan"),
    ("jenis", "jenis"),
    ("varieti", "varieti"),
    ("gred MID", "gred"),
    ("unit", "unit"),
    ("harga", "harga"),
    ("average14", "average14"),
    ("supply", "supply"),
]

SOURCE_COLUMNS = [src for src, _ in FIELD_MAP]
CSV_COLUMNS = [dst for _, dst in FIELD_MAP]

DEFAULT_OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "daily"
)


# --------------------------------------------------------------------------
# value formatting
# --------------------------------------------------------------------------
def format_value(value) -> str:
    """Render one decoded DSR value as a CSV cell (empty string for null)."""
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    return str(value)


def priceid_sort_key(priceid: str):
    """Numeric where possible, lexicographic otherwise — always deterministic."""
    text = priceid or ""
    if text.isdigit():
        return (0, len(text), text)
    return (1, 0, text)


def row_to_record(row: dict) -> dict:
    return {dst: format_value(row.get(src)) for src, dst in FIELD_MAP}


# --------------------------------------------------------------------------
# CSV merge (Contract A)
# --------------------------------------------------------------------------
def read_csv(path: str) -> dict:
    """Read an existing daily CSV into ``{priceid: record}``."""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            record = {col: (row.get(col) or "") for col in CSV_COLUMNS}
            if record["priceid"]:
                out[record["priceid"]] = record
    return out


def merge_records(existing: dict, incoming, stats: dict | None = None) -> list:
    """Merge by ``priceid``, add new rows, return sorted list of records.

    Non-empty incoming values overwrite the archived ones (late corrections are
    the point of re-fetching the whole window). **Empty incoming values do not**:
    a degraded upstream response — right row count, blanked columns — would
    otherwise silently destroy good archived data, pass the control-count check
    and get committed, becoming unrecoverable once the source's 30-day window
    rolls past. When the incoming value for a column is empty and the archived
    one is not, the archived value is kept.

    ``stats`` (optional dict) is filled with:

    * ``preserved_fields`` — number of individual field values kept
    * ``preserved_rows``   — number of rows in which at least one was kept
    """
    merged = dict(existing)
    preserved_fields = 0
    preserved_rows = 0
    for record in incoming:
        priceid = record.get("priceid") or ""
        if not priceid:
            continue
        new = {col: record.get(col, "") for col in CSV_COLUMNS}
        old = merged.get(priceid)
        if old is not None:
            kept = 0
            for col in CSV_COLUMNS:
                if not (new.get(col) or "").strip() and (old.get(col) or "").strip():
                    new[col] = old[col]
                    kept += 1
            if kept:
                preserved_fields += kept
                preserved_rows += 1
        merged[priceid] = new
    if stats is not None:
        stats["preserved_fields"] = preserved_fields
        stats["preserved_rows"] = preserved_rows
    return sorted(merged.values(), key=lambda r: priceid_sort_key(r["priceid"]))


def write_csv(path: str, records) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow(record)
    os.replace(tmp, path)


def merge_into_file(path: str, incoming) -> dict:
    """Merge ``incoming`` records into ``path``. Returns a small stats dict."""
    existing = read_csv(path)
    merge_stats: dict = {}
    merged = merge_records(existing, incoming, stats=merge_stats)
    before = len(existing)
    changed = merged != sorted(
        existing.values(), key=lambda r: priceid_sort_key(r["priceid"])
    )
    if changed:
        write_csv(path, merged)
    return {
        "path": path,
        "before": before,
        "after": len(merged),
        "added": len(merged) - before,
        "changed": changed,
        "preserved_fields": merge_stats.get("preserved_fields", 0),
        "preserved_rows": merge_stats.get("preserved_rows", 0),
    }


# --------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------
def fetch_date_counts(client: PowerBIClient, entity: str = ENTITY) -> list:
    """Control aggregation: ``[(YYYY-MM-DD, count), ...]`` sorted by date."""
    command = {
        "Query": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
            "Select": [
                column_ref("t", "tarikh harga"),
                aggregation("t", "priceid", 5, "cnt"),
            ],
        },
        "Binding": {
            "Primary": {"Groupings": [{"Projections": [0, 1]}]},
            "DataReduction": {"DataVolume": 3, "Primary": {"Top": {"Count": 5000}}},
            "Version": 1,
        },
    }
    result = client.run_command(command)
    out = []
    for row in result.dicts():
        stamp = row.get("tarikh harga")
        count = row.get("cnt")
        if stamp is None:
            continue
        out.append((stamp.strftime("%Y-%m-%d"), int(count or 0)))
    out.sort()
    return out


def fetch_negeri_counts(client: PowerBIClient, date_str: str, entity: str = ENTITY) -> list:
    """Control aggregation per state for one date: ``[(negeri, count), ...]``."""
    command = {
        "Query": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
            "Select": [
                column_ref("t", "negeri"),
                aggregation("t", "priceid", 5, "cnt"),
            ],
            "Where": [datetime_equals("t", "tarikh harga", date_str)],
        },
        "Binding": {
            "Primary": {"Groupings": [{"Projections": [0, 1]}]},
            "DataReduction": {"DataVolume": 3, "Primary": {"Top": {"Count": 5000}}},
            "Version": 1,
        },
    }
    result = client.run_command(command)
    return [(row.get("negeri"), int(row.get("cnt") or 0)) for row in result.dicts()]


def fetch_rows(client: PowerBIClient, where, top: int, entity: str = ENTITY):
    """Run a detail query; returns ``(records, QueryResult)``."""
    command = detail_query(entity, SOURCE_COLUMNS, where=where, top=top)
    result = client.run_command(command)
    return [row_to_record(row) for row in result.dicts()], result


def fetch_date(
    client: PowerBIClient,
    date_str: str,
    expected: int | None = None,
    top: int = 30000,
    entity: str = ENTITY,
    log=print,
):
    """Fetch all rows for one price date, chunking by ``negeri`` if needed.

    Returns ``(records, info)`` where ``info`` records how the fetch went.
    """
    where = [datetime_equals("t", "tarikh harga", date_str)]
    records, result = fetch_rows(client, where, top, entity)
    info = {
        "date": date_str,
        "expected": expected,
        "single_pass_rows": len(records),
        "chunked": False,
        "signals": {
            "DLEx": result.data_limit_exceeded,
            "HAD": result.has_all_data,
            "RT": bool(result.restart_tokens),
            "at_cap": result.row_limit is not None and len(records) >= result.row_limit,
        },
        "chunk_mismatches": [],
    }

    count_mismatch = expected is not None and len(records) != expected
    if not result.truncated and not count_mismatch:
        info["rows"] = len(records)
        return records, info

    reason = []
    if result.truncated:
        reason.append("truncation signal %s" % info["signals"])
    if count_mismatch:
        reason.append("count mismatch got=%d expected=%s" % (len(records), expected))
    log("  ! %s: %s -> chunking by negeri" % (date_str, "; ".join(reason)))

    info["chunked"] = True
    info["chunk_reason"] = "; ".join(reason)
    by_id = {r["priceid"]: r for r in records}
    for negeri, ncount in fetch_negeri_counts(client, date_str, entity):
        cwhere = [datetime_equals("t", "tarikh harga", date_str)]
        if negeri is None:
            # Null state cannot be matched by equality; keep the single-pass rows.
            info["chunk_mismatches"].append(("<null negeri>", ncount, None))
            continue
        cwhere.append(string_equals("t", "negeri", negeri))
        crecords, cresult = fetch_rows(client, cwhere, top, entity)
        if cresult.truncated or len(crecords) != ncount:
            info["chunk_mismatches"].append((negeri, ncount, len(crecords)))
        for record in crecords:
            by_id[record["priceid"]] = record
    records = list(by_id.values())
    info["rows"] = len(records)
    return records, info


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def run(argv=None) -> int:
    """CLI entry point. See the module docstring for the exit-code contract.

    Exit codes: ``0`` OK (archive-superset included), ``1`` genuine verification
    mismatch (fetched != control), ``2`` fetch/transport/decode failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scraper.scrape",
        description="Fetch the FAMA rolling price window into data/daily/*.csv",
    )
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--dates", nargs="*", default=None,
        help="only these YYYY-MM-DD dates (default: every available date)",
    )
    parser.add_argument(
        "--limit-dates", type=int, default=None,
        help="only the N most recent available dates",
    )
    parser.add_argument("--top", type=int, default=30000)
    parser.add_argument(
        "--min-interval", type=float, default=0.4,
        help="minimum seconds between API calls (politeness)",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and verify but write nothing"
    )
    args = parser.parse_args(argv)

    client = PowerBIClient(timeout=args.timeout, min_interval=args.min_interval)

    print("discovering available dates (control aggregation)...")
    try:
        date_counts = fetch_date_counts(client)
    except PowerBIError as exc:
        print("ERROR: control aggregation failed: %s" % (exc,), file=sys.stderr)
        return 2
    if not date_counts:
        print("ERROR: no dates returned by the source", file=sys.stderr)
        return 2
    print(
        "  %d dates, %s .. %s, control total %d rows"
        % (
            len(date_counts),
            date_counts[0][0],
            date_counts[-1][0],
            sum(c for _, c in date_counts),
        )
    )

    selected = date_counts
    if args.dates:
        wanted = set(args.dates)
        selected = [d for d in date_counts if d[0] in wanted]
        missing = wanted - {d[0] for d in date_counts}
        for date_str in sorted(missing):
            print("  ! requested date not available upstream: %s" % date_str)
    if args.limit_dates:
        selected = selected[-args.limit_dates:]

    table = []
    anomalies = []
    notes = []
    total_written = 0
    total_preserved_fields = 0
    total_preserved_rows = 0
    for date_str, expected in selected:
        try:
            records, info = fetch_date(
                client, date_str, expected=expected, top=args.top
            )
        except PowerBIError as exc:
            print("ERROR: fetch failed for %s: %s" % (date_str, exc), file=sys.stderr)
            return 2
        path = os.path.join(args.out_dir, "%s.csv" % date_str)
        if args.dry_run:
            stats = {
                "after": len(records), "added": 0, "changed": False,
                "preserved_fields": 0, "preserved_rows": 0,
            }
        else:
            stats = merge_into_file(path, records)
        written = stats["after"]
        fetched = len(records)
        total_written += written
        total_preserved_fields += stats.get("preserved_fields", 0)
        total_preserved_rows += stats.get("preserved_rows", 0)

        # --- verification: the FETCH against the control aggregation ---------
        status = "ok"
        if fetched != expected:
            status = "MISMATCH"
            anomalies.append(
                "%s: control=%d fetched=%d (file=%d)"
                % (date_str, expected, fetched, written)
            )
        if info["chunk_mismatches"]:
            status = "MISMATCH"
            anomalies.append(
                "%s: per-negeri mismatches %r" % (date_str, info["chunk_mismatches"])
            )

        # --- informational: archive vs upstream ------------------------------
        # The archive keeps rows upstream has since deleted; that is expected
        # and must never fail the run.
        if written > expected:
            if status == "ok":
                status = "ok+archive"
            notes.append(
                "%s: archive is a superset of upstream (file=%d > control=%d, "
                "+%d row(s) upstream no longer serves)"
                % (date_str, written, expected, written - expected)
            )
        elif written < expected and status == "ok":
            # Fetch matched control but fewer rows landed: duplicate priceids.
            notes.append(
                "%s: file=%d < control=%d although fetched=%d matched "
                "(duplicate priceids in the response?)"
                % (date_str, written, expected, fetched)
            )
        if stats.get("preserved_fields"):
            notes.append(
                "%s: kept %d archived field value(s) across %d row(s) where the "
                "incoming response was empty"
                % (date_str, stats["preserved_fields"], stats["preserved_rows"])
            )

        table.append(
            {
                "date": date_str,
                "expected": expected,
                "fetched": fetched,
                "written": written,
                "chunked": info["chunked"],
                "status": status,
            }
        )
        print(
            "  %s expected=%-6d fetched=%-6d file=%-6d chunked=%-5s %s"
            % (date_str, expected, fetched, written, info["chunked"], status)
        )

    print("")
    print("%-12s %10s %10s %10s %8s %s" % ("date", "expected", "fetched", "written", "chunked", "status"))
    for entry in table:
        print(
            "%-12s %10d %10d %10d %8s %s"
            % (
                entry["date"],
                entry["expected"],
                entry["fetched"],
                entry["written"],
                entry["chunked"],
                entry["status"],
            )
        )
    print(
        "%-12s %10d %10d %10d"
        % (
            "TOTAL",
            sum(e["expected"] for e in table),
            sum(e["fetched"] for e in table),
            total_written,
        )
    )

    if total_preserved_fields:
        print(
            "\nWARNING: kept %d archived field value(s) across %d row(s) because the "
            "incoming response had them empty (possible degraded upstream response)."
            % (total_preserved_fields, total_preserved_rows),
            file=sys.stderr,
        )

    if notes:
        print("\nNOTES (informational, not failures):")
        for line in notes:
            print("  " + line)

    if anomalies:
        print("\nANOMALIES (fetch does not match control aggregation):", file=sys.stderr)
        for line in anomalies:
            print("  " + line, file=sys.stderr)
        return 1
    return 0


def main():  # pragma: no cover
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
