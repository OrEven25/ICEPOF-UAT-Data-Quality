"""
Targeted, per-ORIG_TRAN_ID raw-data extraction — instead of exporting an entire raw
batch file (a SECDEF file can hold ~100 securities, a UDS file up to ~10,000
messages), pull out just:
  - the execution report(s) that make up this specific order/trade's lifecycle
  - the Security Definition entry and/or User Defined Strategy message used to
    resolve its instrument, via the exact same linkage cascade the reconciliation
    itself uses (get_matching_instrument)

Which raw execution reports are "relevant" depends on which STG table the
ORIG_TRAN_ID came from (see spec 5.1.1 / 5.2 and OrderMapper/TradeMapper.cs):
  - CLIENT_ORDERS: every execution report whose ClOrdID (tag 11) resolves, via the
    recursive OrigClOrdID chain, to this ORIG_TRAN_ID (its full lifecycle — New,
    Partial Fills, Cancel, etc., possibly spanning several raw source files).
  - CLIENT_TRADES: the single execution report whose ExecID (tag 17) equals this
    ORIG_TRAN_ID directly.
"""

from __future__ import annotations

import csv
import io

from .csv_export import TAG_LABELS
from .mapping_engine import get_matching_instrument

SECDEF_TAG_LABELS = {
    311: "UnderlyingSymbol", 305: "SecurityType", 307: "UnderlyingSecurityDesc",
    308: "UnderlyingSecurityExchange", 309: "UnderlyingSecurityID", 318: "UnderlyingCurrency",
    326: "SecurityStatus", 463: "UnderlyingCFICode", 542: "UnderlyingMaturityDate",
    763: "SecuritySubType", 916: "StartDate", 917: "EndDate", 998: "UnderlyingUnitOfMeasure",
    9017: "LotSize", 9061: "ProductID", 9062: "ProductName", 9063: "ProductDesc",
    9202: "StripName", 9300: "HubName", 9301: "HubAliasFull", 9302: "HubAlias", 9303: "ContractCode",
}

UDS_TAG_LABELS = {
    48: "SecurityID", 55: "Symbol", 167: "SecurityType", 207: "SecurityExchange",
    555: "NoLegs", 600: "LegSymbol", 609: "LegSecurityType", 623: "LegSide",
    624: "LegRatioQty", 762: "SecuritySubType", 996: "UnitOfMeasure", 9061: "ProductID",
    9100: "PriceDenomination", 9202: "StripName", 9302: "HubAlias",
}


def find_order_exec_report_rows(exec_df, orig_tran_id_map: dict, orig_tran_id: str) -> list[tuple]:
    matching_tag11 = {t11 for t11, root in orig_tran_id_map.items() if root == orig_tran_id}
    rows = []
    for _, r in exec_df.iterrows():
        pm = r["ParsedMessage"]
        if pm.scalar.get(11) in matching_tag11:
            rows.append((r["ReceivedAtUtc"], r["SequenceNumber"], r["SourceFile"], pm.scalar))
    rows.sort(key=lambda x: (str(x[0]), x[1]))
    return rows


def find_trade_exec_report_rows(exec_df, orig_tran_id: str) -> list[tuple]:
    rows = []
    for _, r in exec_df.iterrows():
        pm = r["ParsedMessage"]
        if pm.scalar.get(17) == orig_tran_id:
            rows.append((r["ReceivedAtUtc"], r["SequenceNumber"], r["SourceFile"], pm.scalar))
    return rows


def flatten_exec_rows_to_csv(rows: list[tuple]) -> str:
    all_tags: set[int] = set()
    for _, _, _, scalar in rows:
        all_tags.update(scalar.keys())
    sorted_tags = sorted(all_tags)
    header = ["ReceivedAtUtc", "SequenceNumber", "SourceFile"] + [
        f"tag_{t}" + (f" ({TAG_LABELS[t]})" if t in TAG_LABELS else "") for t in sorted_tags
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for received_at, seq, source, scalar in rows:
        writer.writerow([received_at, seq, source] + [scalar.get(t, "") for t in sorted_tags])
    return buf.getvalue()


def flatten_secdef_entry_to_csv(entry, source_hint: str | None = None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tag", "Value"])
    if source_hint:
        writer.writerow(["# matched via", source_hint])
    for tag in sorted(entry.fields.keys()):
        label = SECDEF_TAG_LABELS.get(tag, TAG_LABELS.get(tag))
        writer.writerow([f"tag_{tag}" + (f" ({label})" if label else ""), entry.fields[tag]])
    return buf.getvalue()


def find_stg_rows(actual_df, orig_tran_id: str):
    """The actual staging-table row(s) (CLIENT_ORDERS or CLIENT_TRADES, whichever
    `actual_df` is) already booked for this ORIG_TRAN_ID — i.e. what the mapper
    actually produced, as opposed to the raw FIX input or the reference data used
    to resolve it. Usually one row; can be >1 or 0 when this ID is also a
    lifecycle-count mismatch (see Lifecycle integrity)."""
    if actual_df is None:
        return None
    return actual_df[actual_df["ORIG_TRAN_ID"].astype(str) == str(orig_tran_id)]


def flatten_stg_rows_to_csv(stg_rows) -> str:
    buf = io.StringIO()
    stg_rows.to_csv(buf, index=False)
    return buf.getvalue()


def flatten_uds_message_to_csv(pm, source_hint: str | None = None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tag", "Value"])
    if source_hint:
        writer.writerow(["# matched via", source_hint])
    for tag in sorted(pm.scalar.keys()):
        label = UDS_TAG_LABELS.get(tag, TAG_LABELS.get(tag))
        writer.writerow([f"tag_{tag}" + (f" ({label})" if label else ""), pm.scalar[tag]])
    if pm.legs:
        writer.writerow([])
        writer.writerow(["LEGS"])
        leg_tags = sorted({t for leg in pm.legs for t in leg.keys()})
        writer.writerow([
            f"tag_{t}" + (f" ({UDS_TAG_LABELS[t]})" if t in UDS_TAG_LABELS else "") for t in leg_tags
        ])
        for leg in pm.legs:
            writer.writerow([leg.get(t, "") for t in leg_tags])
    return buf.getvalue()


def csv_link_for_id(orig_tran_id: str, kind: str) -> str:
    """kind: 'exec' | 'stg' | 'secdef' | 'uds' — one small targeted CSV per (ID, kind)."""
    safe_id = str(orig_tran_id).replace("/", "_").replace(" ", "_")
    return f"raw_exports/by_id/{safe_id}_{kind}.csv"


def build_targeted_links(exec_df, orig_tran_id_map: dict, secdef_index: dict, uds_index: dict,
                          actual_df, table: str, orig_tran_id: str) -> tuple[list[dict], dict[str, str]]:
    """Returns (links: [{label, href}], csv_files: {href: csv_text}) for one
    ORIG_TRAN_ID. `table` is 'orders' or 'trades'. `actual_df` is the actual
    CLIENT_ORDERS/CLIENT_TRADES DataFrame matching `table` (whatever the mapper
    actually produced, for the STG Record link)."""
    links: list[dict] = []
    csv_files: dict[str, str] = {}

    if table == "orders":
        exec_rows = find_order_exec_report_rows(exec_df, orig_tran_id_map, orig_tran_id)
    else:
        exec_rows = find_trade_exec_report_rows(exec_df, orig_tran_id)

    tag55 = None
    if exec_rows:
        href = csv_link_for_id(orig_tran_id, "exec")
        csv_files[href] = flatten_exec_rows_to_csv(exec_rows)
        links.append({"label": f"Execution Report{'s' if len(exec_rows) > 1 else ''} ({len(exec_rows)})", "href": href})
        tag55 = exec_rows[0][3].get(55)

    stg_rows = find_stg_rows(actual_df, orig_tran_id)
    if stg_rows is not None and not stg_rows.empty:
        href = csv_link_for_id(orig_tran_id, "stg")
        csv_files[href] = flatten_stg_rows_to_csv(stg_rows)
        links.append({"label": f"STG Record{'s' if len(stg_rows) > 1 else ''} ({len(stg_rows)})", "href": href})

    if tag55:
        match = get_matching_instrument({55: tag55}, secdef_index, uds_index)
        if match["link_type"] == "secdef_direct" and match["secdef"] is not None:
            href = csv_link_for_id(orig_tran_id, "secdef")
            csv_files[href] = flatten_secdef_entry_to_csv(match["secdef"], f"direct match on tag55={tag55}")
            links.append({"label": "Security Definition", "href": href})
        elif match["link_type"] == "uds":
            if match["uds"] is not None:
                href = csv_link_for_id(orig_tran_id, "uds")
                csv_files[href] = flatten_uds_message_to_csv(match["uds"], f"tag55={tag55}")
                links.append({"label": "User Defined Strategy", "href": href})
            if match["secdef"] is not None:
                href = csv_link_for_id(orig_tran_id, "secdef")
                csv_files[href] = flatten_secdef_entry_to_csv(match["secdef"], f"via UDS leg for tag55={tag55}")
                links.append({"label": "Security Definition", "href": href})

    return links, csv_files
