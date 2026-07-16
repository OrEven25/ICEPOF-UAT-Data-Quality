"""
Produces the data/YYYY-MM-DD.json payload for the "ICEPOF Reconciliation" Claude
Design dashboard, extended (per user request) with:
  - which STG table (orders/trades) each example/lifecycle ORIG_TRAN_ID came from
  - a set of targeted raw-data links per ORIG_TRAN_ID: the specific execution
    report(s) that make up its lifecycle, the actual STG (CLIENT_ORDERS/CLIENT_TRADES)
    record(s) already booked for it, plus whichever Security Definition and/or
    User Defined Strategy message resolved its instrument (via the same linkage
    cascade the reconciliation itself uses — see targeted_export.build_targeted_links)
  - findings that touch multiple fields on the same ORIG_TRAN_ID (e.g. UNIT and
    CURRENCY both wrong on the same trade) are merged into ONE row instead of
    repeating ORIG_TRAN_ID/Table/Source once per field.
  - a mandatory-field-population check (compare.build_mandatory_field_gaps) run
    directly against the actual CLIENT_ORDERS/CLIENT_TRADES output — independent
    of whether we can reconstruct an "expected" value for a field, several
    mandatory fields (SORT_ID, PARTY, SOURCE, TIMEZONE, ORIGIN, ORIG_INS_TYPE,
    INS_TYPE) are excluded from the field-mismatch comparison entirely, so this
    is the only place a silent null/blank gap in one of those would surface.

Also returns the full set of {href: csv_text} to upload (collected while building the
rows, since each targeted CSV is generated once per referenced ORIG_TRAN_ID).
"""

from __future__ import annotations

from .compare import MANDATORY_FIELDS_ORDERS, MANDATORY_FIELDS_TRADES, build_mandatory_field_gaps
from .targeted_export import build_targeted_links


class LinkCollector:
    """Accumulates targeted CSV exports across all findings/lifecycle rows, keyed by
    href, so the same (table, ORIG_TRAN_ID) pair is only computed once even if it
    appears in multiple findings."""

    def __init__(self, exec_df, orig_tran_id_map, secdef_index, uds_index, actual_orders, actual_trades):
        self.exec_df = exec_df
        self.orig_tran_id_map = orig_tran_id_map
        self.secdef_index = secdef_index
        self.uds_index = uds_index
        self.actual_orders = actual_orders
        self.actual_trades = actual_trades
        self.csv_files: dict[str, str] = {}
        self._cache: dict[tuple[str, str], list[dict]] = {}

    def links_for(self, table: str, orig_tran_id: str) -> list[dict]:
        key = (table, orig_tran_id)
        if key not in self._cache:
            actual_df = self.actual_orders if table == "orders" else self.actual_trades
            links, csv_files = build_targeted_links(
                self.exec_df, self.orig_tran_id_map, self.secdef_index, self.uds_index,
                actual_df, table, orig_tran_id,
            )
            self._cache[key] = links
            self.csv_files.update(csv_files)
        return self._cache[key]


def _merge_examples_by_id(field_examples: list[tuple[str, list[dict]]], links: LinkCollector) -> list[list]:
    """field_examples: [(field_name, examples[]), ...] where each example has
    ORIG_TRAN_ID/table/expected/actual. Returns rows with one entry per distinct
    ORIG_TRAN_ID (union across all fields): {cells: [...], links: [{label,href}]}."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for field_name, examples in field_examples:
        for e in examples:
            oid = e.get("ORIG_TRAN_ID")
            if oid not in by_id:
                table = "orders" if e.get("table") == "orders" else "trades"
                by_id[oid] = {"table": table, "fields": {}}
                order.append(oid)
            by_id[oid]["fields"][field_name] = (e.get("expected"), e.get("actual"))

    field_names = [f for f, _ in field_examples]
    rows = []
    for oid in order:
        rec = by_id[oid]
        table_label = "Orders" if rec["table"] == "orders" else "Trades"
        cells = [oid, table_label]
        for fn in field_names:
            exp, act = rec["fields"].get(fn, ("", ""))
            cells.extend([exp, act])
        rows.append({"cells": cells, "links": links.links_for(rec["table"], oid)})
    return rows


def _mandatory_gap_rows(gaps: list[dict], links: LinkCollector) -> list[dict]:
    """One entry per (table, field) with at least one blank row, carrying a capped
    sample of affected ORIG_TRAN_IDs with the same targeted raw-data links used
    everywhere else — so a gap in a field like SORT_ID or PARTY (both excluded from
    the field-mismatch comparison entirely) is still drillable down to raw FIX."""
    out = []
    for g in gaps:
        if g["missing"] == 0 or g["column_absent"]:
            continue
        table_label = "Orders" if g["table"] == "orders" else "Trades"
        sample_rows = [
            {"cells": [oid, table_label], "links": links.links_for(g["table"], oid)}
            for oid in g["examples"]
        ]
        out.append({
            "field": g["field"], "table": g["table"], "missing": g["missing"], "total": g["total"],
            "sampleRows": sample_rows,
        })
    return out


def _lifecycle_rows(mismatches: list[dict], links: LinkCollector) -> list[list]:
    rows = []
    for m in mismatches[:15]:
        table = m.get("table") or "orders"
        row_links = links.links_for(table, m["ORIG_TRAN_ID"])
        rows.append({
            "cells": [m["ORIG_TRAN_ID"], m["expected_rows"], m["actual_rows"]],
            "links": row_links,
        })
    return rows


def build_dashboard_json(*, report_date: str, orders_result: dict, trades_result: dict,
                          diagnostics: list[dict], unit_currency_finding: dict,
                          price_diff_summary: dict, notes: list[str],
                          exec_df, orig_tran_id_map, secdef_index, uds_index,
                          actual_orders, actual_trades) -> tuple[dict, dict[str, str]]:
    """Returns (payload, csv_files) — csv_files is {href: csv_text} to upload
    alongside the JSON."""
    links = LinkCollector(exec_df, orig_tran_id_map, secdef_index, uds_index, actual_orders, actual_trades)

    diag_reasons: dict[str, int] = {}
    for d in diagnostics:
        key = d["reason"].split(":")[0].split(" for ")[0]
        diag_reasons[key] = diag_reasons.get(key, 0) + 1

    unit_currency_rows = _merge_examples_by_id([
        ("UNIT", unit_currency_finding["unit_examples"]),
        ("CURRENCY", unit_currency_finding["currency_examples"]),
    ], links)
    price_rows = _merge_examples_by_id([("PRICE", price_diff_summary["examples"])], links)

    mandatory_orders = build_mandatory_field_gaps(actual_orders, MANDATORY_FIELDS_ORDERS, "orders")
    mandatory_trades = build_mandatory_field_gaps(actual_trades, MANDATORY_FIELDS_TRADES, "trades")

    findings = [
        {
            "severity": "critical",
            "label": "Critical",
            "title": "UNIT and CURRENCY silently inherit the previous trade's values",
            "body": (
                f"Every UNIT mismatch ({unit_currency_finding['unit_mismatch_count']} of "
                f"{trades_result['aligned_row_pairs']} trade rows) and every CURRENCY mismatch "
                f"({unit_currency_finding['ccy_mismatch_count']}) exactly equals the immediately "
                "preceding trade row's value — a 100% correlation. A Crude Oil trade (North Sea, IFEU) "
                "is landing with UNIT=MWh / CURRENCY=EUR — a Natural Gas / European-power unit and "
                "currency — while its COMMODITY, COUNTRY, MARKET_AREA and MARKET_PLACE are all "
                "correctly derived. Consistent with the code-review finding on MapperBase.cs:45, "
                "\"mutable per-call state on shared instance bleeds between calls\"."
            ),
            "sampleColumns": ["ORIG_TRAN_ID", "Table", "Expected UNIT", "Actual UNIT",
                               "Expected CURRENCY", "Actual CURRENCY"],
            "sampleRows": unit_currency_rows,
        },
        {
            "severity": "serious",
            "label": "Serious",
            "title": "Trades' PRICE mostly off by tick-size, consistent with using order price instead of fill price",
            "body": (
                f"{price_diff_summary['mismatch_count']} of {trades_result['aligned_row_pairs']} trade "
                "PRICE values don't match the spec-documented value (Tag-31 / fill price). Most "
                "differences are small (0.01–0.03) — consistent with the actual code using the order's "
                "limit price (Tag-44) instead of the trade's fill price, matching the code-review finding "
                f"on TradeMapper.cs:138. {price_diff_summary['outlier_count']} trades differ by a much "
                f"larger margin ({price_diff_summary['outlier_range']}) and likely need individual "
                "investigation rather than being explained by the same tick-size pattern."
            ),
            "sampleColumns": ["ORIG_TRAN_ID", "Table", "Expected PRICE", "Actual PRICE"],
            "sampleRows": price_rows,
        },
    ]

    payload = {
        "date": report_date,
        "summary": {
            "orderIdsMatched": [orders_result["common_keys"], orders_result["expected_keys"]],
            "orderRowsCompared": orders_result["aligned_row_pairs"],
            "orderFieldsMismatched": sum(1 for s in orders_result["field_stats"].values() if s["mismatches"] > 0),
            "tradeIdsMatched": [trades_result["common_keys"], trades_result["expected_keys"]],
            "tradeRowsCompared": trades_result["aligned_row_pairs"],
            "tradeFieldsMismatched": sum(1 for s in trades_result["field_stats"].values() if s["mismatches"] > 0),
        },
        "findings": findings,
        "orderTotal": orders_result["aligned_row_pairs"],
        "orderFields": [[f, s["mismatches"]] for f, s in sorted(
            orders_result["field_stats"].items(), key=lambda kv: -kv[1]["mismatches"])],
        "tradeTotal": trades_result["aligned_row_pairs"],
        "tradeFields": [[f, s["mismatches"]] for f, s in sorted(
            trades_result["field_stats"].items(), key=lambda kv: -kv[1]["mismatches"])],
        "lifecycle": {
            "ordersAffected": len(orders_result["lifecycle_mismatches"]),
            "tradesAffected": len(trades_result["lifecycle_mismatches"]),
            "orderColumns": ["ORIG_TRAN_ID", "Expected rows", "Actual rows"],
            "tradeColumns": ["ORIG_TRAN_ID", "Expected rows", "Actual rows"],
            "orders": _lifecycle_rows(orders_result["lifecycle_mismatches"], links),
            "trades": _lifecycle_rows(trades_result["lifecycle_mismatches"], links),
        },
        "unresolved": {
            "total": len(diagnostics),
            "reasons": [[k, v] for k, v in diag_reasons.items()],
        },
        "mandatoryFields": {
            "orderTotal": len(actual_orders),
            "tradeTotal": len(actual_trades),
            "orders": [[g["field"], g["missing"]] for g in
                       sorted(mandatory_orders, key=lambda g: -g["missing"])],
            "trades": [[g["field"], g["missing"]] for g in
                       sorted(mandatory_trades, key=lambda g: -g["missing"])],
            "orderGaps": _mandatory_gap_rows(
                sorted(mandatory_orders, key=lambda g: -g["missing"]), links),
            "tradeGaps": _mandatory_gap_rows(
                sorted(mandatory_trades, key=lambda g: -g["missing"]), links),
        },
        "notes": notes,
    }

    return payload, links.csv_files
