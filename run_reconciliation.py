"""Runs the ICEPOF raw-vs-mapped reconciliation for a given date. Usage:
    python run_reconciliation.py 2026-07-14
"""

import glob
import json
import os
import sys
from datetime import date

import pandas as pd

from icepof_reconciliation.fix_parser import load_raw_messages, load_raw_secdef_messages
from icepof_reconciliation.reference_data import ReferenceData
from icepof_reconciliation.mapping_engine import (
    build_secdef_index, build_uds_index, build_expected_rows, build_orig_tran_id_map,
)
from icepof_reconciliation.compare import compare_tables, ORDER_COMPARABLE_FIELDS, TRADE_COMPARABLE_FIELDS
from icepof_reconciliation.build_report import build_html
from icepof_reconciliation.export_dashboard import build_dashboard_json

DASHBOARD_EXPORT_DIR = "output/dashboard_export"


def run_for_date(report_date: str):
    raw_dir = f"data/{report_date}/raw"
    mapped_dir = f"data/{report_date}/mapped"
    output_path = f"output/icepof_reconciliation_{report_date}.html"

    er = load_raw_messages(raw_dir, report_date, "execution_report")
    sd = load_raw_secdef_messages(raw_dir, report_date)
    ud = load_raw_messages(raw_dir, report_date, "user_defined_strategy")

    secdef_index = build_secdef_index(sd)
    uds_index = build_uds_index(ud)
    ref = ReferenceData()

    today = date(*[int(x) for x in report_date.split("-")])
    expected_orders, expected_trades, diagnostics = build_expected_rows(
        er, secdef_index, uds_index, ref, today=today
    )

    actual_orders = pd.read_parquet(glob.glob(f"{mapped_dir}/CLIENT_ORDERS_*.parquet")[0])
    actual_trades = pd.read_parquet(glob.glob(f"{mapped_dir}/CLIENT_TRADES_*.parquet")[0])
    actual_trades["ORIG_TRAN_ID"] = actual_trades["ORIG_TRAN_ID"].astype(str)
    actual_orders["ORIG_TRAN_ID"] = actual_orders["ORIG_TRAN_ID"].astype(str)

    orders_result = compare_tables(expected_orders, actual_orders, ORDER_COMPARABLE_FIELDS, table_name="orders")
    trades_result = compare_tables(expected_trades, actual_trades, TRADE_COMPARABLE_FIELDS, table_name="trades")

    # --- UNIT/CURRENCY stale-state correlation check ---
    actual_sorted = actual_trades.sort_values("TRAN_DATETIME").reset_index(drop=True)
    exp_by_oid = {r["ORIG_TRAN_ID"]: r for r in expected_trades}
    unit_mismatch_count = 0
    ccy_mismatch_count = 0
    unit_matches_prev = 0
    ccy_matches_prev = 0
    for i, row in actual_sorted.iterrows():
        exp = exp_by_oid.get(row["ORIG_TRAN_ID"])
        if exp is None:
            continue
        prev_unit = actual_sorted.iloc[i - 1]["UNIT"] if i > 0 else None
        prev_ccy = actual_sorted.iloc[i - 1]["CURRENCY"] if i > 0 else None
        if str(exp.get("UNIT")) != str(row["UNIT"]):
            unit_mismatch_count += 1
            if str(row["UNIT"]) == str(prev_unit):
                unit_matches_prev += 1
        if str(exp.get("CURRENCY")) != str(row["CURRENCY"]):
            ccy_mismatch_count += 1
            if str(row["CURRENCY"]) == str(prev_ccy):
                ccy_matches_prev += 1

    unit_currency_finding = {
        "unit_mismatch_count": unit_mismatch_count,
        "ccy_mismatch_count": ccy_mismatch_count,
        "unit_examples": trades_result["field_stats"]["UNIT"]["examples"],
        "currency_examples": trades_result["field_stats"]["CURRENCY"]["examples"],
        "unit_matches_prev": unit_matches_prev,
        "ccy_matches_prev": ccy_matches_prev,
    }

    # --- PRICE diff distribution for trades ---
    price_diffs = []
    for r in expected_trades:
        match = actual_trades[actual_trades["ORIG_TRAN_ID"] == r["ORIG_TRAN_ID"]]
        if match.empty:
            continue
        try:
            exp_p, act_p = float(r.get("PRICE")), float(match.iloc[0]["PRICE"])
            if exp_p != act_p:
                price_diffs.append(round(act_p - exp_p, 4))
        except (TypeError, ValueError):
            continue
    outliers = [d for d in price_diffs if abs(d) >= 1]
    price_diff_summary = {
        "mismatch_count": len(price_diffs),
        "outlier_count": len(outliers),
        "outlier_range": f"{min(outliers):.2f} to {max(outliers):.2f}" if outliers else "n/a",
        "examples": trades_result["field_stats"]["PRICE"]["examples"],
    }

    notes = [
        "Reference/lookup data: COUNTRY Mapping.xlsx and ICEPOF_Commodities mapping.xlsm were provided and used "
        "for COUNTRY/COMMODITY resolution (Steps 1-3 per spec 5.3/5.4). No TAG-50 trader-mapping file was provided; "
        "TRADER falls back to a direct Tag-9139 mapping (per spec 5.1/5.2's own 'Direct Mapping' entry) and SUBPARTY "
        "was not independently validated.",
        "Full-quarter/full-year UDS collapsing and the ProductID->TRAN_INS_TYPE override table were both discovered "
        "only in the mapper source code (MapperBase.cs / OrderMapper.cs / TradeMapper.cs), not in the spec's written "
        "text — flagged per-row via '_notes' in the underlying data rather than assumed correct; whether this "
        "behavior is an intentional business rule or an undocumented deviation is a judgment call for engineering/BA "
        "review, not something this framework can determine on its own.",
        "Single Spread Mode (SSO/Hybrid/Classic, spec section 6) requires a per-client props-file setting not "
        "available to this analysis; Classic mode (no SSO instrument collapsing) was assumed throughout.",
        "Self-trade pairing (ORIG_TRAN_ID _B/_S suffixing on offsetting trades, per TradeMapper.cs CheckSelfTrade) "
        "is not modeled — a small number of trade mismatches may be attributable to this rather than a mapping bug.",
        "Nested UDS-to-UDS traversal is capped at depth 10 (matching the source's own MaxTraversalDepth); deeper "
        "chains would be reported as unresolved rather than silently guessed.",
        "Section 7 (PL/Position Limits Connection) is out of scope — different target table, excluded per original "
        "request.",
        f"{len(diagnostics)} execution reports were excluded/unresolved and are not represented in either the "
        "expected or actual row counts above — see 'Unresolved / excluded' for the breakdown.",
    ]

    html_out = build_html(
        report_date=report_date,
        orders_result=orders_result,
        trades_result=trades_result,
        diagnostics=diagnostics,
        unit_currency_finding=unit_currency_finding,
        price_diff_summary=price_diff_summary,
        notes=notes,
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Report written to {output_path}")
    print(f"Orders: {orders_result['common_keys']}/{orders_result['expected_keys']} keys matched, "
          f"{orders_result['aligned_row_pairs']} rows compared")
    print(f"Trades: {trades_result['common_keys']}/{trades_result['expected_keys']} keys matched, "
          f"{trades_result['aligned_row_pairs']} rows compared")

    # --- Dashboard JSON + targeted per-ID raw-data CSVs, for the Claude Design project ---
    orig_tran_id_map = build_orig_tran_id_map(er)
    dashboard_payload, csv_files = build_dashboard_json(
        report_date=report_date,
        orders_result=orders_result,
        trades_result=trades_result,
        diagnostics=diagnostics,
        unit_currency_finding=unit_currency_finding,
        price_diff_summary=price_diff_summary,
        notes=notes,
        exec_df=er,
        orig_tran_id_map=orig_tran_id_map,
        secdef_index=secdef_index,
        uds_index=uds_index,
    )

    os.makedirs(f"{DASHBOARD_EXPORT_DIR}/data", exist_ok=True)
    os.makedirs(f"{DASHBOARD_EXPORT_DIR}/raw_exports/by_id", exist_ok=True)

    with open(f"{DASHBOARD_EXPORT_DIR}/data/{report_date}.json", "w", encoding="utf-8") as f:
        json.dump(dashboard_payload, f, indent=2)

    print(f"Writing {len(csv_files)} targeted raw-data CSV(s)...")
    for href, csv_text in csv_files.items():
        out_path = os.path.join(DASHBOARD_EXPORT_DIR, href)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)

    print(f"Dashboard export ready at {DASHBOARD_EXPORT_DIR}/ "
          f"(data/{report_date}.json + {len(csv_files)} raw_exports/by_id/*.csv)")

    return dashboard_payload, csv_files


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_reconciliation.py YYYY-MM-DD")
        sys.exit(1)
    run_for_date(sys.argv[1])
