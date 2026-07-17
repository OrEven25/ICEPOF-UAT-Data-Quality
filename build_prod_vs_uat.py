"""One-off: compute a PROD vs UAT structural comparison for 2026-07-16 from the
xlsx export (both clients' CLIENT_ORDERS/CLIENT_TRADES side by side, since PROD
is a different client [Axpo] than UAT [CUBELOGIC] with zero overlapping
ORIG_TRAN_IDs, so the comparison must be structural rather than row-level) and
merge it into dashboard_project/data/2026-07-16.json under "prodVsUat"."""
import json
import sys

import pandas as pd

sys.path.insert(0, ".")
from icepof_reconciliation.compare import (
    MANDATORY_FIELDS_ORDERS, MANDATORY_FIELDS_TRADES, build_mandatory_field_gaps,
)

XLSX = (
    r"C:\Users\OR11AD~1.EVE\AppData\Local\Temp\claude\C--Users-or-even"
    r"\b8634ffa-ed7b-475d-ae34-0f30d36ff9c1\scratchpad\ICEPOf_20260716.xlsx"
)
OUT_JSON = "dashboard_project/data/2026-07-16.json"


def mandatory_gap_count(df, fields):
    gaps = build_mandatory_field_gaps(df, fields, "")
    return sum(g["missing"] for g in gaps)


def orphan_stats(df):
    sizes = df.groupby("ORIG_TRAN_ID").size()
    single_ids = sizes[sizes == 1].index
    single = df[df["ORIG_TRAN_ID"].isin(single_ids)]
    by_status = single["TRAN_STATUS"].value_counts().to_dict()
    examples = single[["ORIG_TRAN_ID", "TRAN_STATUS"]].astype(str).values.tolist()[:15]
    return {
        "orphanIds": int(len(single_ids)), "totalIds": int(len(sizes)),
        "byStatus": by_status, "examples": examples,
    }


def amend_stats(df):
    sizes = df.groupby("ORIG_TRAN_ID").size()
    multi_ids = sizes[sizes > 1].index
    examples = []
    for oid, grp in df[df["ORIG_TRAN_ID"].isin(multi_ids)].groupby("ORIG_TRAN_ID"):
        examples.append(str(oid) + ":" + ",".join(grp.sort_values("TRAN_DATETIME")["TRAN_STATUS"].tolist()))
    return {
        "amendedIds": int(len(multi_ids)), "totalIds": int(len(sizes)),
        "examples": examples[:15],
    }


def status_dist(df):
    return df["TRAN_STATUS"].value_counts().to_dict()


def main():
    uat_o = pd.read_excel(XLSX, sheet_name="UAT_Orders")
    prod_o = pd.read_excel(XLSX, sheet_name="Prod_Orders")
    uat_t = pd.read_excel(XLSX, sheet_name="UAT_Trades")
    prod_t = pd.read_excel(XLSX, sheet_name="Prod_Trades")

    payload = {
        "note": (
            "PROD (Axpo) and UAT (CUBELOGIC) are different clients with no "
            "overlapping ORIG_TRAN_IDs on 2026-07-16, so this is a structural "
            "comparison rather than row-level reconciliation."
        ),
        "orderCounts": {
            "uatRows": len(uat_o), "uatIds": int(uat_o["ORIG_TRAN_ID"].nunique()),
            "prodRows": len(prod_o), "prodIds": int(prod_o["ORIG_TRAN_ID"].nunique()),
        },
        "tradeCounts": {
            "uatRows": len(uat_t), "uatIds": int(uat_t["ORIG_TRAN_ID"].nunique()),
            "prodRows": len(prod_t), "prodIds": int(prod_t["ORIG_TRAN_ID"].nunique()),
        },
        "mandatoryGaps": {
            "uatOrders": mandatory_gap_count(uat_o, MANDATORY_FIELDS_ORDERS),
            "prodOrders": mandatory_gap_count(prod_o, MANDATORY_FIELDS_ORDERS),
            "uatTrades": mandatory_gap_count(uat_t, MANDATORY_FIELDS_TRADES),
            "prodTrades": mandatory_gap_count(prod_t, MANDATORY_FIELDS_TRADES),
        },
        "lifecycleOrphans": {
            "uat": orphan_stats(uat_o),
            "prod": orphan_stats(prod_o),
        },
        "tradeAmendments": {
            "uat": amend_stats(uat_t),
            "prod": amend_stats(prod_t),
        },
        "statusDist": {
            "orders": {"uat": status_dist(uat_o), "prod": status_dist(prod_o)},
            "trades": {"uat": status_dist(uat_t), "prod": status_dist(prod_t)},
        },
    }

    with open(OUT_JSON, encoding="utf-8") as f:
        day = json.load(f)
    day["prodVsUat"] = payload
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(day, f, indent=2)
    print("wrote prodVsUat into", OUT_JSON)


if __name__ == "__main__":
    main()
