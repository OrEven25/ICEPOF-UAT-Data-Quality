"""
Flattens a raw ICEPOF execution-report parquet file (as referenced by a CLIENT_ORDERS/
CLIENT_TRADES row's SOURCE column) into a human-readable CSV — one row per FIX message,
one column per tag number — so a reviewer can click through from a finding/lifecycle
mismatch straight to the underlying raw FIX data without needing Python.
"""

from __future__ import annotations

import csv
import io
import os

import pandas as pd

from .fix_parser import parse_fix_message

# A short human label for the most common tags, to make the CSV self-explanatory
# without needing the FIX dictionary open — purely cosmetic, doesn't affect parsing.
TAG_LABELS = {
    11: "ClOrdID", 14: "CumQty", 17: "ExecID", 31: "LastPx", 32: "LastQty",
    38: "OrderQty", 40: "OrdType", 41: "OrigClOrdID", 44: "Price", 54: "Side",
    55: "Symbol", 59: "TimeInForce", 60: "TransactTime", 150: "ExecType",
    151: "LeavesQty", 210: "MaxShow", 442: "MultiLegReportingType",
    828: "TrdType", 9068: "ContraFirm", 9066: "BrokerCompName", 9139: "OriginatorUserID",
    5364: "MemberName", 9175: "OrderState",
}


# Prefixes the mapper prepends to SOURCE for synthetically-created rows that have no
# underlying raw FIX message — currently only seen on CLIENT_ORDERS, from the overnight
# "Open Orders Handling" close-out (spec section 8.1.2): the real file named after the
# prefix is the ORIGINAL order's execution report, kept for context.
SYNTHETIC_SOURCE_PREFIXES = ["order-open-"]


def resolve_source_filename(raw_dir: str, source_filename: str) -> tuple[str, bool]:
    """Returns (actual_filename_to_read, was_synthetic). Strips known synthetic
    prefixes and falls back to the underlying real file if present."""
    path = os.path.join(raw_dir, source_filename)
    if os.path.exists(path):
        return source_filename, False
    for prefix in SYNTHETIC_SOURCE_PREFIXES:
        if source_filename.startswith(prefix):
            underlying = source_filename[len(prefix):]
            if os.path.exists(os.path.join(raw_dir, underlying)):
                return underlying, True
    raise FileNotFoundError(f"Raw source file not found: {path}")


def flatten_execution_report_file(raw_dir: str, source_filename: str) -> str:
    """Returns CSV text for every execution report message in the given raw parquet
    file. Columns: ReceivedAtUtc, SequenceNumber, then every tag seen (as
    'tag_<N> (Label)'), sorted numerically. Transparently resolves synthetic
    SOURCE values (see SYNTHETIC_SOURCE_PREFIXES) to their underlying real file."""
    resolved_filename, was_synthetic = resolve_source_filename(raw_dir, source_filename)
    path = os.path.join(raw_dir, resolved_filename)

    df = pd.read_parquet(path)
    parsed_rows = []
    all_tags: set[int] = set()
    for _, r in df.iterrows():
        pm = parse_fix_message(str(r["Body"]))
        all_tags.update(pm.scalar.keys())
        parsed_rows.append((r["ReceivedAtUtc"], r["SequenceNumber"], pm.scalar))

    sorted_tags = sorted(all_tags)
    header = ["ReceivedAtUtc", "SequenceNumber"] + [
        f"tag_{t}" + (f" ({TAG_LABELS[t]})" if t in TAG_LABELS else "") for t in sorted_tags
    ]

    buf = io.StringIO()
    writer = csv.writer(buf)
    if was_synthetic:
        writer.writerow([
            f"# SOURCE was '{source_filename}' — a synthetically-created row (e.g. overnight "
            f"Open-Orders close-out) with no raw message of its own. Showing the ORIGINAL "
            f"order's execution report ('{resolved_filename}') for context instead."
        ])
    writer.writerow(header)
    for received_at, seq, scalar in parsed_rows:
        writer.writerow([received_at, seq] + [scalar.get(t, "") for t in sorted_tags])

    return buf.getvalue()


def csv_link_for_source(source_filename: str | None) -> str | None:
    """Relative path (within the dashboard project) that a flattened CSV for this
    source file will be published to. None if source_filename is falsy."""
    if not source_filename:
        return None
    base = source_filename
    if base.endswith(".parquet"):
        base = base[: -len(".parquet")]
    return f"raw_exports/{base}.csv"
