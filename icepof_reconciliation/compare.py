"""
Aligns expected (spec-reconstructed) vs. actual (real CLIENT_ORDERS/CLIENT_TRADES
parquet output) rows and computes field-level mismatch statistics, following the same
methodology as the existing ICEPOF_Prod_UAT_0907_Comparison_Report.html: group by
ORIG_TRAN_ID, sort each group by TRAN_DATETIME (then original order) before pairing
rows, and analyze lifecycle event-count mismatches (different row counts per
ORIG_TRAN_ID) separately from field-level accuracy (which only makes sense for
same-count groups, otherwise row pairing is arbitrary).
"""

from __future__ import annotations

import re
from decimal import Decimal

import pandas as pd

FIX_DATETIME_RE = re.compile(r"^(\d{8})-(\d{2}):(\d{2}):(\d{2})(\.\d+)?$")

ORDER_COMPARABLE_FIELDS = [
    "TRAN_STATUS", "PRICE", "VOLUME", "MARKET_AREA", "TRAN_INS_TYPE", "ORDER_TYPE",
    "TRADER", "BID_ASK", "TRAN_DATETIME", "INS_CLASS", "COMMODITY", "MARKET_PLACE",
    "DELIVERY_PERIOD", "UNIT", "COUNTRY", "DELIVERY_CATEGORY", "LOT_SIZE", "SIDE",
]

TRADE_COMPARABLE_FIELDS = [
    "TRAN_STATUS", "MARKET_AREA", "TRAN_INS_TYPE", "TRADER", "BUY_SELL", "CURRENCY",
    "SIDE", "PRICE", "VOLUME", "TRAN_DATETIME", "INS_CLASS", "COMMODITY", "MARKET_PLACE",
    "DELIVERY_PERIOD", "UNIT", "COUNTRY", "DELIVERY_CATEGORY", "LOT_SIZE", "FIXED_ROLLOVER",
]

NOT_VALIDATED_ORDERS = [
    "ORIG_INS_TYPE", "ORIGIN", "ORIG_CD_ID", "BOOK", "SORT_ID", "HIDDEN_VOL", "PARTY",
    "SUBPARTY", "COUNTERPARTY", "CLIENT_ID", "EXCLUDED_FROM", "TIMEZONE", "SOURCE",
    "HEDGING_FLAG", "SPREAD", "PREMIUM", "PUT_CALL", "INS_TYPE", "BROKER", "CURRENCY",
]

NOT_VALIDATED_TRADES = [
    "ORIGIN", "ORIG_CD_ID", "BOOK", "SORT_ID", "PARTY", "SUBPARTY", "CLIENT_ID",
    "EXCLUDED_FROM", "TIMEZONE", "SOURCE", "SPREAD", "PREMIUM", "HEDGING_FLAG",
    "PUT_CALL", "INS_TYPE", "BROKER", "DELIVERY_HOURS", "SLEEVE", "ORIG_INS_TYPE",
    "COUNTERPARTY",
]

# Mandatory-field population check: fields the STG mapping tables (5.1/5.2) mark
# Mandatory=Y (or, for Orders, leave the column blank but clearly load-bearing —
# e.g. ORIG_TRAN_ID/TRAN_STATUS) should never be null/blank in the actual
# CLIENT_ORDERS/CLIENT_TRADES output, independent of whether we can reconstruct an
# "expected" value for them (several of these — SORT_ID, PARTY, SOURCE, TIMEZONE,
# ORIGIN, ORIG_INS_TYPE, INS_TYPE — are in NOT_VALIDATED_* above and so never
# surface in the field-mismatch comparison at all; this check is the only place
# a silent gap in one of those would be caught).
MANDATORY_FIELDS_BOTH = [
    "TRAN_STATUS", "COUNTRY", "COMMODITY", "DELIVERY_PERIOD", "TRAN_INS_TYPE",
    "INS_CLASS", "INS_TYPE", "TRADER", "ORIGIN", "ORIG_TRAN_ID", "SIDE", "SORT_ID",
    "TRAN_DATETIME", "TIMEZONE", "VOLUME", "UNIT", "PRICE", "CURRENCY", "PARTY",
    "SOURCE", "ORIG_INS_TYPE", "DELIVERY_CATEGORY",
]
MANDATORY_FIELDS_ORDERS = MANDATORY_FIELDS_BOTH + ["ORDER_TYPE", "BID_ASK"]
MANDATORY_FIELDS_TRADES = MANDATORY_FIELDS_BOTH + ["BUY_SELL"]


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def build_mandatory_field_gaps(actual_df: pd.DataFrame, fields: list[str], table_name: str) -> list[dict]:
    """For each mandatory field, how many actual rows have it null/blank. Includes
    fields with zero gaps too (mirrors the field-mismatch tables, which likewise
    keep zero-rate fields in the data and let the dashboard's hide-zero toggle
    filter them) so a field going from populated to unpopulated is visible as soon
    as it happens, not only once it already has gaps."""
    total = len(actual_df)
    results = []
    for field in fields:
        if field not in actual_df.columns:
            results.append({
                "field": field, "table": table_name, "missing": total, "total": total,
                "column_absent": True, "examples": [],
            })
            continue
        blank_mask = actual_df[field].apply(_is_blank)
        missing = int(blank_mask.sum())
        examples = []
        if missing and "ORIG_TRAN_ID" in actual_df.columns:
            examples = actual_df.loc[blank_mask, "ORIG_TRAN_ID"].astype(str).unique()[:10].tolist()
        results.append({
            "field": field, "table": table_name, "missing": missing, "total": total,
            "column_absent": False, "examples": examples,
        })
    return results


def _normalize(val):
    """Canonicalize a value from either side (raw FIX string on the expected side,
    pandas/parquet-native types — Timestamp, Decimal — on the actual side) so
    equivalent values compare equal regardless of representation."""
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # millisecond precision

    if isinstance(val, str):
        m = FIX_DATETIME_RE.match(val)
        if m:
            yyyymmdd, hh, mm, ss, frac = m.groups()
            millis = (frac or ".000")[1:4].ljust(3, "0")
            return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]} {hh}:{mm}:{ss}.{millis}"

    if isinstance(val, Decimal):
        val = float(val)

    if isinstance(val, (int, float)):
        s = f"{float(val):.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    s = str(val).strip()
    try:
        f = float(s)
        s2 = f"{f:.6f}".rstrip("0").rstrip(".")
        return s2 if s2 else "0"
    except ValueError:
        pass
    return s


# A synthesized V row (see mapping_engine.build_expected_rows) shares its terminal
# counterpart's exact TRAN_DATETIME, and the actual system doesn't consistently store the
# V row before or after it (observed both ways) — so sorting on TRAN_DATETIME alone leaves
# same-timestamp pairs in arbitrary/inconsistent relative order on the expected vs. actual
# side, which would misalign TRAN_STATUS (and other fields) between them. Break ties with
# a fixed lifecycle-stage rank instead, applied identically to both sides.
_STATUS_STAGE_RANK = {"V": 0, "P": 1, "A": 2}


def _group_and_sort(df: pd.DataFrame, key: str = "ORIG_TRAN_ID", time_col: str = "TRAN_DATETIME",
                     status_col: str = "TRAN_STATUS"):
    groups: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        k = row.get(key)
        if k is None:
            continue
        groups.setdefault(str(k), []).append(row.to_dict())
    for k in groups:
        groups[k].sort(key=lambda r: (
            str(r.get(time_col) or ""), _STATUS_STAGE_RANK.get(r.get(status_col), 3),
        ))
    return groups


def compare_tables(expected_rows: list[dict], actual_df: pd.DataFrame, comparable_fields: list[str],
                    key: str = "ORIG_TRAN_ID", table_name: str = "") -> dict:
    """table_name ('orders'|'trades') is stamped onto every example/lifecycle-mismatch
    row so downstream reporting can show which STG table (CLIENT_ORDERS/CLIENT_TRADES)
    a given ORIG_TRAN_ID came from."""
    expected_df = pd.DataFrame(expected_rows)
    expected_groups = _group_and_sort(expected_df, key) if not expected_df.empty else {}
    actual_groups = _group_and_sort(actual_df, key)

    all_keys = set(expected_groups) | set(actual_groups)
    only_expected = sorted(set(expected_groups) - set(actual_groups))
    only_actual = sorted(set(actual_groups) - set(expected_groups))
    common_keys = sorted(set(expected_groups) & set(actual_groups))

    lifecycle_mismatches = []
    aligned_pairs: list[tuple[dict, dict]] = []

    for k in common_keys:
        exp_rows = expected_groups[k]
        act_rows = actual_groups[k]
        if len(exp_rows) != len(act_rows):
            sources = sorted({str(a.get("SOURCE")) for a in act_rows if a.get("SOURCE")})
            lifecycle_mismatches.append({
                "ORIG_TRAN_ID": k, "expected_rows": len(exp_rows), "actual_rows": len(act_rows),
                "table": table_name, "sources": sources,
            })
            continue
        for e, a in zip(exp_rows, act_rows):
            aligned_pairs.append((e, a))

    field_stats = {f: {"mismatches": 0, "compared": 0, "examples": []} for f in comparable_fields}
    for e, a in aligned_pairs:
        for f in comparable_fields:
            ev, av = _normalize(e.get(f)), _normalize(a.get(f))
            field_stats[f]["compared"] += 1
            if ev != av:
                field_stats[f]["mismatches"] += 1
                if len(field_stats[f]["examples"]) < 5:
                    field_stats[f]["examples"].append({
                        "ORIG_TRAN_ID": e.get("ORIG_TRAN_ID"), "expected": ev, "actual": av,
                        "table": table_name, "source": a.get("SOURCE"),
                    })

    return {
        "total_expected_rows": len(expected_rows),
        "total_actual_rows": len(actual_df),
        "expected_keys": len(expected_groups),
        "actual_keys": len(actual_groups),
        "common_keys": len(common_keys),
        "only_expected_keys": only_expected,
        "only_actual_keys": only_actual,
        "lifecycle_mismatches": lifecycle_mismatches,
        "aligned_row_pairs": len(aligned_pairs),
        "field_stats": field_stats,
    }
