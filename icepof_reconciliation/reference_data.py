"""
Loads the external lookup workbooks referenced by the ICEPOF Functional Specification
(sections 5.3 COUNTRY Mapping and 5.4 COMMODITY Mapping), and provides best-effort
COUNTRY / COMMODITY / TRADER lookup functions following the documented Step 1/2/3 chains.

Files expected under `reference_data/` (relative to project root):
  - "COUNTRY Mapping.xlsx"            (sheets: Step 1_Config, Step 2_Kew Words,
                                        Step 3_Kew Words, "ProductCodesReport ")
  - "ICEPOF_Commodities mapping.xlsm" (sheets: Step 1_Config, Step 2_tag9602,
                                        Step 3_GROUP, TOTAL Commodity)
  - "TRADER_MAPPING_CWTS - EnBW ... .xlsm" (optional — not yet provided; TRADER/SUBPARTY
    fall back to direct tag9139 mapping with SUBPARTY flagged unresolved when absent)

Step 2/3 keyword-matching fallbacks are inherently fuzzy in the source spec ("lookup
keywords ... and search for a match"); this module implements a straightforward
case-insensitive substring match against the documented keyword columns and always
records which step actually resolved the value, so the caller/report can distinguish
a confident Step-1 exact match from a fuzzier Step-2/3 fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd

REF_DIR = "reference_data"

COUNTRY_FILE = "COUNTRY Mapping.xlsx"
COMMODITY_FILE = "ICEPOF_Commodities mapping.xlsm"
TRADER_FILE_GLOB_HINT = "TRADER_MAPPING_CWTS"  # matched by substring in reference_data/ dir


def _clean_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


@dataclass
class LookupResult:
    value: str | None
    step: str  # "step1" | "step2" | "step3" | "unresolved"


class ReferenceData:
    def __init__(self, ref_dir: str = REF_DIR):
        self.ref_dir = ref_dir
        self._load_country()
        self._load_commodity()
        self._load_trader()

    # ---- loading -------------------------------------------------------------------

    def _path(self, filename: str) -> str:
        return os.path.join(self.ref_dir, filename)

    def _load_country(self):
        path = self._path(COUNTRY_FILE)
        if not os.path.exists(path):
            self.country_step1 = {}
            self.country_step2 = pd.DataFrame()
            self.country_step3 = pd.DataFrame()
            self.product_codes_report = pd.DataFrame()
            self._country_available = False
            return
        self._country_available = True

        step1 = _clean_cols(pd.read_excel(path, sheet_name="Step 1_Config"))
        # PRODUCT ID -> COUNTRY, first match wins (spec doesn't describe de-dup rule)
        step1["PRODUCT ID"] = step1["PRODUCT ID"].astype(str).str.strip()
        self.country_step1 = (
            step1.dropna(subset=["PRODUCT ID", "COUNTRY"])
            .drop_duplicates(subset=["PRODUCT ID"], keep="first")
            .set_index("PRODUCT ID")["COUNTRY"]
            .to_dict()
        )

        self.country_step2 = _clean_cols(pd.read_excel(path, sheet_name="Step 2_Kew Words"))
        self.country_step3 = _clean_cols(pd.read_excel(path, sheet_name="Step 3_Kew Words"))

        pcr = _clean_cols(pd.read_excel(path, sheet_name="ProductCodesReport "))
        pcr["PRODUCT ID"] = pcr["PRODUCT ID"].astype(str).str.strip()
        self.product_codes_report = pcr.drop_duplicates(subset=["PRODUCT ID"], keep="first").set_index(
            "PRODUCT ID"
        )

    def _load_commodity(self):
        path = self._path(COMMODITY_FILE)
        if not os.path.exists(path):
            self.commodity_step1 = {}
            self.commodity_step2 = pd.DataFrame()
            self.commodity_step3_group = {}
            self._commodity_available = False
            return
        self._commodity_available = True

        step1 = _clean_cols(pd.read_excel(path, sheet_name="Step 1_Config"))
        step1["PRODUCT ID"] = step1["PRODUCT ID"].astype(str).str.strip()
        step1 = step1.dropna(subset=["PRODUCT ID"]).drop_duplicates(subset=["PRODUCT ID"], keep="first")
        self.commodity_step1 = step1.set_index("PRODUCT ID").to_dict(orient="index")

        self.commodity_step2 = _clean_cols(pd.read_excel(path, sheet_name="Step 2_tag9602"))

        step3 = _clean_cols(pd.read_excel(path, sheet_name="Step 3_GROUP"))
        self.commodity_step3_group = (
            step3.dropna(subset=["GROUP_ProductCodesReport", "STG_COMMODITY"])
            .set_index("GROUP_ProductCodesReport")["STG_COMMODITY"]
            .to_dict()
        )

    def _load_trader(self):
        self._trader_available = False
        self.trader_lookup: dict[str, tuple[str, str]] = {}
        if not os.path.isdir(self.ref_dir):
            return
        for fname in os.listdir(self.ref_dir):
            if TRADER_FILE_GLOB_HINT.lower() in fname.lower():
                # Structure not yet confirmed (file not provided at time of writing) —
                # best-effort: look for TRADER/SUBPARTY-like columns once available.
                try:
                    df = _clean_cols(pd.read_excel(self._path(fname)))
                except Exception:
                    continue
                cols_lower = {c.lower(): c for c in df.columns}
                key_col = next((cols_lower[c] for c in cols_lower if "tag" in c or "50" in c), None)
                trader_col = next((cols_lower[c] for c in cols_lower if "trader" in c), None)
                subparty_col = next((cols_lower[c] for c in cols_lower if "subparty" in c or "party" in c), None)
                if key_col and trader_col:
                    for _, row in df.iterrows():
                        key = str(row[key_col]).strip()
                        self.trader_lookup[key] = (
                            row.get(trader_col),
                            row.get(subparty_col) if subparty_col else None,
                        )
                    self._trader_available = True

    # ---- COUNTRY (spec 5.3) ----------------------------------------------------------

    def lookup_country(self, tag9061: str | None, secdef_scalar: dict) -> LookupResult:
        if not self._country_available:
            return LookupResult(None, "unresolved")

        if tag9061 and tag9061 in self.country_step1:
            return LookupResult(self.country_step1[tag9061], "step1")

        # Step 2: keyword match against ProductCodesReport fields for this product id
        if tag9061 and tag9061 in self.product_codes_report.index and not self.country_step2.empty:
            pcr_row = self.product_codes_report.loc[tag9061]
            haystacks = [
                str(pcr_row.get("PRODUCT (Click to open in Browser)", "")),
                str(pcr_row.get("MARKET TYPE NAME", "")),
                str(pcr_row.get("MIC CODE", "")),
            ]
            hay = " | ".join(haystacks).lower()
            for _, kw_row in self.country_step2.iterrows():
                for col in self.country_step2.columns:
                    if col == "COUNTRY":
                        continue
                    kw = kw_row.get(col)
                    if isinstance(kw, str) and kw.strip() and kw.strip().lower() in hay:
                        return LookupResult(kw_row["COUNTRY"], "step2")

        # Step 3: keyword match against the SECDEF message's own descriptive tags
        if not self.country_step3.empty:
            hay = " | ".join(
                str(secdef_scalar.get(t, "")) for t in (9063, 9062, 9301, 320, 308)
            ).lower()
            for _, kw_row in self.country_step3.iterrows():
                for col in self.country_step3.columns:
                    if col == "COUNTRY":
                        continue
                    kw = kw_row.get(col)
                    if isinstance(kw, str) and kw.strip() and kw.strip().lower() in hay:
                        return LookupResult(kw_row["COUNTRY"], "step3")

        return LookupResult(None, "unresolved")

    # ---- COMMODITY (spec 5.4) --------------------------------------------------------

    def lookup_commodity(self, tag9061: str | None, secdef_scalar: dict) -> LookupResult:
        if not self._commodity_available:
            return LookupResult(None, "unresolved")

        if tag9061 and tag9061 in self.commodity_step1:
            val = self.commodity_step1[tag9061].get("COMMODITY")
            if val is not None and str(val).strip():
                return LookupResult(val, "step1")

        # Step 2: keyword match against tag9062/9063 (ProductName/ProductDesc)
        if not self.commodity_step2.empty:
            hay = " | ".join(str(secdef_scalar.get(t, "")) for t in (9062, 9063)).lower()
            keycol = self.commodity_step2.columns[0]
            for _, kw_row in self.commodity_step2.iterrows():
                kw = kw_row.get(keycol)
                if isinstance(kw, str) and kw.strip() and kw.strip().lower() in hay:
                    return LookupResult(kw_row["COMMODITY"], "step2")

        # Step 3: ProductCodesReport GROUP -> STG_COMMODITY (may be a comma-separated
        # list requiring country disambiguation per spec comments; we take the first
        # value and flag it as approximate).
        if tag9061 and self._country_available and tag9061 in self.product_codes_report.index:
            group = self.product_codes_report.loc[tag9061].get("GROUP")
            if group in self.commodity_step3_group:
                stg = self.commodity_step3_group[group]
                first_val = str(stg).split(",")[0].strip()
                step = "step3" if "," not in str(stg) else "step3_ambiguous"
                return LookupResult(first_val, step)

        return LookupResult(None, "unresolved")

    # ---- TRADER / SUBPARTY ------------------------------------------------------------

    def lookup_trader(self, tag9139: str | None, tag116: str | None) -> tuple[str | None, str | None, str]:
        """Returns (trader, subparty, resolution_note)."""
        if tag116:
            trader_string = tag116.split("|")[-1] if "|" in tag116 else tag116
        else:
            trader_string = tag9139

        if self._trader_available and trader_string in self.trader_lookup:
            trader, subparty = self.trader_lookup[trader_string]
            return trader, subparty, "mapping_file"

        # No mapping file available yet: direct tag9139 fallback for TRADER,
        # SUBPARTY left unresolved (spec section 5.1.5 requires the trader file for a
        # confident value; guessing SUBPARTY == TRADER would be unfounded).
        return trader_string, None, "unresolved_no_mapping_file"
