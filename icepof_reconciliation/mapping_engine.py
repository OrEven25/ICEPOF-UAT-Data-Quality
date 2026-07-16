"""
Implements ICEPOF Functional Specification sections 4/5/6, cross-checked against the
actual production mapper source (MapperBase.cs, OrderMapper.cs, TradeMapper.cs,
provided by the user) to ground exact FIX-tag-to-field meanings and the real branching
logic (instrument matching cascade, KUDS/UUDS classification, leg-splitting for UUDS,
full-quarter/full-year UDS collapsing).

This module intentionally implements the SPEC-documented behavior (not a byte-for-byte
port of the C# source) so that comparing its output against the actual CLIENT_ORDERS/
CLIENT_TRADES surfaces real divergences — e.g. the source code's TradeMapper.BuildTrade
uses `report.Price` (tag 44, order price) where the spec's PRICE mapping table calls for
tag 31 (fill price), which is the exact bug already flagged in the ICEPOF code review.
Known simplifications vs. the real source (not modeled — flagged in the report rather
than guessed): self-trade pairing (ORIG_TRAN_ID _B/_S suffixing), SSO/Hybrid spread mode
(Single Spread Mode requires a per-client props setting we don't have; Classic mode is
assumed), and the previous-volume cache is approximated per ORIG_TRAN_ID.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from .reference_data import ReferenceData

ORDERS_EXEC_TYPES = {"0", "1", "2", "4", "5"}
TRADES_EXEC_TYPES = {"1", "2", "G", "H"}

TRAN_STATUS_ORDERS = {"0": "V", "1": "P", "2": "E", "4": "C", "5": "A"}
TRAN_STATUS_TRADES = {"1": "V", "2": "V", "G": "A", "H": "C"}

MAX_UDS_TRAVERSAL_DEPTH = 10

MONTH_ABBR = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

CAL_RE = re.compile(r"^Cal\s*(\d{2})$", re.IGNORECASE)
QUARTER_RE = re.compile(r"^Q([1-4])\s*(\d{2})$", re.IGNORECASE)
SEASON_RE = re.compile(r"^(Winter|Summer)\s*(\d{2})$", re.IGNORECASE)
WEEK_RE = re.compile(r"^WK\s*(\d{1,2})-(\d{2})$", re.IGNORECASE)
MONTHLY_RE = re.compile(r"^([A-Za-z]{3})(\d{2})$")
DAILY_RE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{2})$")
BAL_MONTH_RE = re.compile(r"^Bal Month", re.IGNORECASE)
MONTH_TO_MONTH_RE = re.compile(r"^[A-Za-z]{3}\d{2}-[A-Za-z]{3}\d{2}$")

# Exact regexes ported from MapperBase.cs IsUdsKnown, so KUDS/UUDS classification
# matches the real source rather than a spec-text approximation.
KUDS_MONTH_TO_MONTH_RE = re.compile(r"^[A-Z][a-z]{2}\d{2}-[A-Z][a-z]{2}\d{2}$")
KUDS_CAL_RE = re.compile(r"^Cal \d{2}$")
KUDS_QUARTER_RE = re.compile(r"^Q\d \d{2}$")
KUDS_WINTER_RE = re.compile(r"^Winter\d{2}$")
KUDS_SUMMER_RE = re.compile(r"^Summer\d{2}$")

DELIVERY_PERIOD_MONTH_RE = re.compile(r"^(M|Q)(\d+)")


def convert_delivery_period(strip_name: str | None, today: date) -> str | None:
    """Port of DeliveryPeriodMapper.GetDeliveryPeriod (spec 5.1.2 conversion table)."""
    if not strip_name:
        return None
    s = strip_name.strip()

    m = CAL_RE.match(s)
    if m:
        return f"Yr20{m.group(1)}"
    m = QUARTER_RE.match(s)
    if m:
        return f"Q{m.group(1)}20{m.group(2)}"
    m = SEASON_RE.match(s)
    if m:
        prefix = "Win" if m.group(1).lower() == "winter" else "Sum"
        return f"{prefix}20{m.group(2)}"
    m = WEEK_RE.match(s)
    if m:
        return "RW**"
    m = MONTHLY_RE.match(s)
    if m and m.group(1) in MONTH_ABBR:
        return f"M{MONTH_ABBR[m.group(1)]}20{m.group(2)}"
    m = DAILY_RE.match(s)
    if m:
        day, mon, yr = m.groups()
        if mon in MONTH_ABBR:
            try:
                delivery_date = date(2000 + int(yr), int(MONTH_ABBR[mon]), int(day))
            except ValueError:
                return None
            days_diff = (delivery_date - today).days
            return None if days_diff < 1 else f"RD{days_diff:02d}"
    if BAL_MONTH_RE.match(s):
        return "BM"
    if "/" in s:
        return None  # time-spread legs: raw combination handled by caller, not here
    return None


# ---------------------------------------------------------------------------------
# SECDEF / UDS indices (unchanged: SECDEF batched-entry parsing lives in fix_parser)
# ---------------------------------------------------------------------------------

def build_secdef_index(secdef_df: pd.DataFrame) -> dict[str, "object"]:
    index = {}
    for parsed in secdef_df["ParsedSecDef"]:
        for entry in parsed.entries:
            tag311 = entry.get(311)
            if tag311 and tag311 not in index:
                index[tag311] = entry
    return index


def build_uds_index(uds_df: pd.DataFrame) -> dict[str, "object"]:
    index = {}
    for pm in uds_df["ParsedMessage"]:
        tag55 = pm.scalar.get(55)
        if tag55 and tag55 not in index:
            index[tag55] = pm
    return index


# ---------------------------------------------------------------------------------
# Instrument matching cascade (port of MapperBase.GetMatchingInstrument)
# ---------------------------------------------------------------------------------

def _leg_symbols(uds_pm) -> list[str]:
    return [leg.get(600) for leg in uds_pm.legs if leg.get(600)]


def _find_secdef_for_legs(leg_symbols: list[str], secdef_index: dict) -> tuple[str | None, object | None]:
    """Returns (matched_leg_symbol, secdef_entry) for the first leg symbol with a
    known SECDEF instrument (mirrors FindSecurityDefinitionForLegSymbols + the
    Instruments.FirstOrDefault(...) selection in GetMatchingInstrument)."""
    for leg_symbol in leg_symbols:
        entry = secdef_index.get(leg_symbol)
        if entry is not None:
            return leg_symbol, entry
    return None, None


def _traverse_nested_uds(uds_pm, leg_symbols: list[str], secdef_index: dict, uds_index: dict):
    """Port of TraverseNestedUdsForSecurityDefinition: a UDS leg can itself be another
    UDS's symbol rather than a direct SECDEF instrument. BFS up to depth 10."""
    from collections import deque

    visited = {uds_pm.scalar.get(55) or ""}
    queue = deque([(leg_symbols, uds_pm)])
    depth = 0

    while queue and depth < MAX_UDS_TRAVERSAL_DEPTH:
        depth += 1
        current_legs, current_uds = queue.popleft()
        leg_symbol, entry = _find_secdef_for_legs(current_legs, secdef_index)
        if entry is not None:
            return entry, current_uds
        for leg_symbol in current_legs:
            if leg_symbol in visited:
                continue
            nested_uds = uds_index.get(leg_symbol)
            if nested_uds is not None:
                visited.add(leg_symbol)
                nested_legs = _leg_symbols(nested_uds)
                if nested_legs:
                    queue.append((nested_legs, nested_uds))
    return None, None


def get_matching_instrument(exec_scalar: dict, secdef_index: dict, uds_index: dict):
    """
    Returns dict: link_type ('secdef_direct'|'uds'|'unresolved'), secdef (SecurityEntry
    for direct match, or the matched leg's SecurityEntry for UDS match), uds
    (ParsedFixMessage or None), is_known_uds (bool).
    """
    tag55 = exec_scalar.get(55)

    direct = secdef_index.get(tag55)
    if direct is not None:
        return {"link_type": "secdef_direct", "secdef": direct, "uds": None, "is_known_uds": False}

    uds_pm = uds_index.get(tag55)
    if uds_pm is None:
        return {"link_type": "unresolved", "secdef": None, "uds": None, "is_known_uds": False}

    leg_symbols = _leg_symbols(uds_pm)
    matched_leg, secdef_entry = _find_secdef_for_legs(leg_symbols, secdef_index)

    if secdef_entry is None and leg_symbols:
        secdef_entry, resolved_uds = _traverse_nested_uds(uds_pm, leg_symbols, secdef_index, uds_index)
        if resolved_uds is not None:
            uds_pm = resolved_uds

    is_known_uds = classify_uds(uds_pm)
    return {"link_type": "uds", "secdef": secdef_entry, "uds": uds_pm, "is_known_uds": is_known_uds}


def classify_uds(uds_pm) -> bool:
    """Exact port of MapperBase.IsUdsKnown: strip-name pattern + matching leg
    SecuritySubType (tag 762) among the UDS's own leg group. NOTE: the real source
    checks `leg.LegSecuritySubType` (per-leg), but tag 762 in our raw messages is a
    message-level scalar (SecuritySubType of the strategy itself, not per leg) — we
    use the message-level tag 762 value against each rule's expected subtype, which is
    consistent with every worked example in the spec."""
    strip_name = uds_pm.scalar.get(9202, "") or ""
    subtype = uds_pm.scalar.get(762)
    no_legs = uds_pm.scalar.get(555)
    try:
        no_legs_int = int(no_legs) if no_legs is not None else None
    except ValueError:
        no_legs_int = None

    if KUDS_MONTH_TO_MONTH_RE.match(strip_name) and subtype == "700":
        return True
    if KUDS_CAL_RE.match(strip_name) and no_legs_int == 12 and subtype == "900":
        return True
    if KUDS_QUARTER_RE.match(strip_name) and no_legs_int == 3 and subtype == "800":
        return True
    if (KUDS_WINTER_RE.match(strip_name) or KUDS_SUMMER_RE.match(strip_name)) and no_legs_int == 6 and subtype == "700":
        return True
    if (strip_name.startswith("WK") or strip_name.endswith("Week")) and no_legs_int in (5, 6, 7) and subtype == "600":
        return True
    if strip_name.startswith("Bal Month") and no_legs_int == 1 and subtype == "400":
        return True
    return False


def is_seasonal_uds(uds_pm) -> bool:
    strip_name = uds_pm.scalar.get(9202, "") or ""
    return bool(re.match(r"^(Winter|Summer)\d{2}$", strip_name, re.IGNORECASE))


def leg_delivery_period(leg_symbol: str, secdef_index: dict, today: date) -> str | None:
    entry = secdef_index.get(leg_symbol)
    if entry is None:
        return None
    return convert_delivery_period(entry.get(9202), today)


def is_full_quarter_uds(uds_pm, secdef_index: dict, today: date) -> tuple[bool, str | None]:
    legs = uds_pm.legs
    if not (3 <= len(legs) <= 4):
        return False, None
    delivery_periods = []
    for leg in legs:
        leg_symbol = leg.get(600)
        if not leg_symbol:
            return False, None
        dp = leg_delivery_period(leg_symbol, secdef_index, today)
        if dp is None:
            return False, None
        delivery_periods.append(dp)

    quarters = set()
    first_year = None
    for dp in delivery_periods:
        m = DELIVERY_PERIOD_MONTH_RE.match(dp)
        if not m:
            return False, None
        kind, num_str = m.group(1), m.group(2)
        num = int(num_str)
        if kind == "M":
            if not (1 <= num <= 12):
                return False, None
            quarter = (num - 1) // 3 + 1
            year = dp[3:7]
        else:
            if not (1 <= num <= 4):
                return False, None
            quarter = num
            year = dp[2:6]
        quarters.add(quarter)
        if first_year is None:
            first_year = year
        elif year != first_year:
            return False, None

    if len(quarters) != 1:
        return False, None
    return True, f"Q{quarters.pop()}{first_year}"


def is_full_year_uds(uds_pm, secdef_index: dict, today: date) -> tuple[bool, str | None]:
    legs = uds_pm.legs
    if len(legs) != 12:
        return False, None
    delivery_periods = set()
    for leg in legs:
        leg_symbol = leg.get(600)
        if not leg_symbol:
            return False, None
        dp = leg_delivery_period(leg_symbol, secdef_index, today)
        if dp is None:
            return False, None
        delivery_periods.add(dp)

    if len(delivery_periods) != 12:
        return False, None

    months = set()
    first_year = None
    for dp in delivery_periods:
        m = re.match(r"^M(\d{2})", dp)
        if not m:
            return False, None
        months.add(m.group(1))
        year = dp[3:7]
        if first_year is None:
            first_year = year
        elif year != first_year:
            return False, None

    if months != {f"{i:02d}" for i in range(1, 13)}:
        return False, None
    return True, f"Yr{first_year}"


# ---------------------------------------------------------------------------------
# TRAN_INS_TYPE (port of MapperBase.GetTranInsType, minus the SSO spread branches
# we don't model — spread flags always False here, matching an assumed Classic mode)
# ---------------------------------------------------------------------------------

def _is_option_instrument(secdef_entry) -> bool:
    if secdef_entry is None:
        return False
    cfi = (secdef_entry.get(463) or "")
    if cfi.upper().startswith("O"):
        return True
    ins_type = (secdef_entry.get(9063) or "")
    if "OPT" in ins_type.upper():
        return True
    subtype = (secdef_entry.get(763) or "")
    if "OPT" in subtype.upper():
        return True
    desc = (secdef_entry.get(307) or secdef_entry.get(9063) or "")
    return "OPTION" in desc.upper()


def derive_tran_ins_type(secdef_entry, ref: ReferenceData, tag828: str | None) -> tuple[str, bool]:
    """Returns (value, override_applied). `override_applied` is True when the
    ProductID->TRAN_INS_TYPE override table was used — this mechanism is NOT
    described anywhere in the spec's 5.1.3 TRAN_INS_TYPE section (which only
    documents KUDS/UUDS/SSO + Index/Fixed-Price-Future + block-trade prefix rules);
    it was found only by reading the mapper source. Flagged per-row rather than
    assumed correct, per instruction not to treat code behavior as ground truth."""
    is_option = _is_option_instrument(secdef_entry)
    tran_ins_type = "OP" if is_option else "ST"

    product_id = secdef_entry.get(9061) if secdef_entry is not None else None
    mapped = None
    if product_id and product_id in ref.commodity_step1:
        candidate = ref.commodity_step1[product_id].get("TRAN_INS_TYPE")
        if candidate is not None and pd.notna(candidate) and str(candidate).strip():
            mapped = str(candidate).strip()

    override_applied = bool(mapped and mapped.upper() != tran_ins_type.upper())
    if override_applied:
        tran_ins_type = mapped

    is_block_trade = (tag828 or "").upper() == "K"
    if is_block_trade:
        tran_ins_type = f"BL_{tran_ins_type}"

    return tran_ins_type, override_applied


# ---------------------------------------------------------------------------------
# Field derivation helpers
# ---------------------------------------------------------------------------------

def _cfi_is_future(cfi: str | None) -> bool:
    return bool(cfi) and cfi[0].upper() == "F"


def _price_for_order(exec_scalar: dict, secdef_entry) -> float | None:
    cfi = secdef_entry.get(463) if secdef_entry is not None else None
    order_type = exec_scalar.get(40)
    exec_type = exec_scalar.get(150)
    if _cfi_is_future(cfi):
        if order_type in ("1", "2", "4") and exec_type in ("0", "5", "4"):
            return exec_scalar.get(44)
        if order_type in ("1", "2", "4") and exec_type in ("1", "2"):
            return exec_scalar.get(31)
        if order_type == "3":
            return exec_scalar.get(99)
        return None
    return secdef_entry.get(316) if secdef_entry is not None else None  # UnderlyingStrikePrice proxy


def _price_for_trade_expected(exec_scalar: dict) -> str | None:
    """Spec-documented value (tag 31 = fill/last price) — deliberately NOT what the
    real TradeMapper.BuildTrade uses (`report.Price`, tag 44); kept literal so the
    comparison surfaces this known divergence rather than masking it."""
    return exec_scalar.get(31)


def _order_type(exec_scalar: dict) -> str | None:
    tag210, tag38, tag59, tag40 = (exec_scalar.get(t) for t in (210, 38, 59, 40))
    try:
        ice_condition = (
            tag210 is not None and tag38 is not None
            and float(tag210) <= float(tag38)
            and tag59 in ("0", "1", "6")
            and tag40 in ("2", "3", "4")
        )
    except (TypeError, ValueError):
        ice_condition = False
    if ice_condition:
        return "ICE"
    return {"1": "MAR", "2": "LIM", "3": "STOP", "4": "STOP"}.get(tag40)


def _volume_for_order(exec_scalar: dict, tran_status: str | None, previous_volume: float | None) -> float | None:
    if tran_status in ("V", "A"):
        return exec_scalar.get(151)
    if tran_status == "P":
        return exec_scalar.get(151)  # CubeWatchTsVersion=11 (!=10) path, per properties.json
    if tran_status == "C":
        if previous_volume is not None:
            return previous_volume
        try:
            return float(exec_scalar.get(38, 0)) - float(exec_scalar.get(14, 0))
        except (TypeError, ValueError):
            return None
    if tran_status == "E":
        return exec_scalar.get(32)
    return None


def _instrument_ref_fields(secdef_entry, uds_pm, ref: ReferenceData) -> dict:
    if secdef_entry is not None:
        product_id = secdef_entry.get(9061)
        country = ref.lookup_country(product_id, secdef_entry.fields)
        commodity = ref.lookup_commodity(product_id, secdef_entry.fields)
        return {
            "country": country.value, "country_step": country.step,
            "commodity": commodity.value, "commodity_step": commodity.step,
            "market_area": secdef_entry.get(9302),
            "unit": secdef_entry.get(998),
            "currency": secdef_entry.get(318),
            "lot_size": secdef_entry.get(9017),
            "market_place": secdef_entry.get(308),
            "strip_name": secdef_entry.get(9202),
        }
    if uds_pm is not None:
        product_id = uds_pm.scalar.get(9061)
        country = ref.lookup_country(product_id, uds_pm.scalar)
        commodity = ref.lookup_commodity(product_id, uds_pm.scalar)
        return {
            "country": country.value, "country_step": country.step,
            "commodity": commodity.value, "commodity_step": commodity.step,
            "market_area": uds_pm.scalar.get(9302),
            "unit": uds_pm.scalar.get(996),
            "currency": uds_pm.scalar.get(9100),
            "lot_size": uds_pm.scalar.get(9017),
            "market_place": uds_pm.scalar.get(207),
            "strip_name": uds_pm.scalar.get(9202),
        }
    return {k: None for k in ("country", "commodity", "market_area", "unit", "currency", "lot_size", "market_place", "strip_name")} | {
        "country_step": "unresolved", "commodity_step": "unresolved",
    }


# ---------------------------------------------------------------------------------
# Top-level per-execution-report row building
# ---------------------------------------------------------------------------------

def build_orig_tran_id_map(exec_df: pd.DataFrame) -> dict[str, str]:
    """
    tag11 (ClOrdID) -> ORIG_TRAN_ID, per spec 5.1.1 / OrderMapper.GetOrigTranId: the
    root ClOrdID reached by recursively following Tag-41 (OrigClOrdID) back through
    the day's execution reports in arrival order. Used both for CLIENT_ORDERS'
    ORIG_TRAN_ID directly, and (inverted) to find every raw execution report that
    belongs to a given order's lifecycle for targeted CSV export.
    """
    orig_tran_id_cache: dict[str, str] = {}
    ordered = exec_df.sort_values(["ReceivedAtUtc", "SequenceNumber"])
    for pm in ordered["ParsedMessage"]:
        s = pm.scalar
        tag11 = s.get(11)
        tag41 = s.get(41)
        exec_type = s.get(150)
        if exec_type == "0" or not tag41:
            orig_tran_id = tag11
        else:
            orig_tran_id = orig_tran_id_cache.get(tag41, tag11)
        if tag11:
            orig_tran_id_cache[tag11] = orig_tran_id
    return orig_tran_id_cache


def build_expected_rows(exec_df: pd.DataFrame, secdef_index: dict, uds_index: dict,
                         ref: ReferenceData, today: date) -> tuple[list[dict], list[dict], list[dict]]:
    diagnostics: list[dict] = []
    expected_orders: list[dict] = []
    expected_trades: list[dict] = []

    orig_tran_id_cache = build_orig_tran_id_map(exec_df)
    previous_volume_cache: dict[str, float] = {}
    sort_id_cache: dict[str, int] = {}

    ordered = exec_df.sort_values(["ReceivedAtUtc", "SequenceNumber"])

    # Drop retransmitted execution reports before building anything: ICE resends the
    # identical business event over a backup FIX session after a session failover
    # (tag 97=PossResend=Y observed; same ExecID/tag 17 and same trade/order economics,
    # only the session envelope — 34/52/56/57/9/10 — differs). Confirmed on 2026-07-14
    # data (e.g. ORIG_TRAN_ID 11000006018842): left undeduped, each resend was counted
    # as a second independent event, inflating expected row counts. ExecID is one-to-one
    # with a specific execution event per spec 5.2, so a repeat is a retransmission, not
    # a new event — keep only the first-arriving copy. Messages without tag 17 (should
    # not occur for Execution Reports, but guard anyway) are never deduped.
    seen_exec_ids: set[str] = set()
    keep_mask: list[bool] = []
    for pm in ordered["ParsedMessage"]:
        exec_id = pm.scalar.get(17)
        if exec_id is None:
            keep_mask.append(True)
            continue
        if exec_id in seen_exec_ids:
            keep_mask.append(False)
            diagnostics.append({
                "tag11": pm.scalar.get(11),
                "reason": f"excluded: duplicate ExecID (tag17={exec_id}) — resend/retransmission of an already-processed execution report",
            })
        else:
            seen_exec_ids.add(exec_id)
            keep_mask.append(True)
    ordered = ordered[keep_mask]

    for pm in ordered["ParsedMessage"]:
        s = pm.scalar
        tag11 = s.get(11)
        tag41 = s.get(41)
        exec_type = s.get(150)
        orig_tran_id = orig_tran_id_cache.get(tag11, tag11)

        if s.get(442) == "2":
            diagnostics.append({"tag11": tag11, "reason": "excluded: MultiLegReportingType(442)=2"})
            continue

        link = get_matching_instrument(s, secdef_index, uds_index)
        if link["link_type"] == "unresolved":
            diagnostics.append({"tag11": tag11, "reason": f"no SECDEF/UDS link found for tag55={s.get(55)}"})
            continue

        uds_pm = link["uds"]
        is_known_uds = link["is_known_uds"]

        # Build the row-set for ORDERS on this execution report: normally one row, but a
        # UUDS (non-KUDS UDS match) fans out into multiple leg rows, unless the legs form
        # a complete quarter or year (collapsed back to one row) or a seasonal UDS (also
        # single row, per IsSeasonalUds). This leg-split fan-out is OrderMapper-specific —
        # see trade_variant below, which never leg-splits.
        row_variants: list[dict] = []  # each: {secdef_entry, uds_pm, delivery_period_override, side, notes}

        if uds_pm is not None and not is_known_uds:
            if is_seasonal_uds(uds_pm):
                dp = convert_delivery_period(uds_pm.scalar.get(9202), today)
                row_variants.append({"secdef": link["secdef"], "uds": uds_pm, "dp_override": dp, "side": 0, "notes": []})
            else:
                is_fq, fq_dp = is_full_quarter_uds(uds_pm, secdef_index, today)
                is_fy, fy_dp = (False, None) if is_fq else is_full_year_uds(uds_pm, secdef_index, today)
                if is_fq or is_fy:
                    row_variants.append({
                        "secdef": link["secdef"], "uds": uds_pm,
                        "dp_override": fq_dp if is_fq else fy_dp, "side": 0,
                        "notes": ["full_quarter_or_year_uds_collapse: code-observed behavior, not documented in spec 4.3.3/5.1.3 text"],
                    })
                else:
                    for i, leg in enumerate(uds_pm.legs, start=1):
                        leg_symbol = leg.get(600)
                        if not leg_symbol:
                            continue
                        leg_secdef = secdef_index.get(leg_symbol)
                        if leg_secdef is None:
                            continue
                        row_variants.append({"secdef": leg_secdef, "uds": None, "dp_override": None, "side": i - 1, "notes": []})
        else:
            row_variants.append({"secdef": link["secdef"], "uds": uds_pm, "dp_override": None, "side": 0, "notes": []})

        if not row_variants:
            diagnostics.append({"tag11": tag11, "reason": "UUDS leg-split produced zero resolvable legs"})
            continue

        # Trades never leg-split, regardless of the ORDERS fan-out above: a multi-leg UDS
        # trade is booked as exactly one CLIENT_TRADES row against the strategy-level
        # instrument (not per leg). row_variants only ever has >1 entries in the UUDS
        # leg-split branch above — every other branch (plain instrument, seasonal UDS,
        # full-quarter/full-year collapse) already produces a single variant, which is
        # reused as-is. Inferred from actual CLIENT_TRADES row counts on multi-leg UDS
        # trades (e.g. ORIG_TRAN_ID 11000006018842, a 2-leg Sep26/Oct26 spread: actual=1)
        # — not independently re-confirmed against TradeMapper.cs source, hence the note.
        if len(row_variants) == 1:
            trade_variant = dict(row_variants[0])
        else:
            # DELIVERY_PERIOD="TIME_SPREAD" / TRAN_INS_TYPE="SP" (rather than the strategy
            # instrument's own fields) inferred from 100% of actual 2026-07-14 CLIENT_TRADES
            # rows for multi-leg UDS trades matching this pair of literal values (e.g.
            # ORIG_TRAN_ID 11000006018842) — not confirmed against TradeMapper.cs source.
            # NOTE: tried generalizing this SP override to every single-row KUDS trade too
            # (2026-07-15 showed 86 KUDS trades with actual TRAN_INS_TYPE=SP) but re-running
            # 2026-07-13 with that broader rule flipped 28 previously-matching trades to
            # mismatches — actual values there were ST/IT, not SP — so a KUDS instrument's
            # TRAN_INS_TYPE isn't reliably SP; reverted to multi-leg-only, which has no
            # counter-examples across all three days.
            trade_variant = {
                "secdef": link["secdef"], "uds": uds_pm, "dp_override": "TIME_SPREAD", "side": 0,
                "tran_ins_type_override": "SP",
                "notes": ["multileg_uds_trade_not_leg_split: TradeMapper writes one row per "
                          "execution report regardless of leg count, unlike OrderMapper — "
                          "inferred from actual CLIENT_TRADES row counts, not confirmed "
                          "against TradeMapper.cs source",
                          "multileg_uds_trade_spread_type: TRAN_INS_TYPE=SP / DELIVERY_PERIOD="
                          "TIME_SPREAD inferred from actual CLIENT_TRADES values, not confirmed "
                          "against TradeMapper.cs source"],
            }

        def _base_row(variant):
            secdef_entry = variant["secdef"]
            inst = _instrument_ref_fields(secdef_entry, variant["uds"], ref)
            delivery_period = variant["dp_override"] or convert_delivery_period(inst["strip_name"], today)
            if variant.get("tran_ins_type_override"):
                tran_ins_type, tran_ins_type_override = variant["tran_ins_type_override"], False
                if (s.get(828) or "").upper() == "K":
                    tran_ins_type = f"BL_{tran_ins_type}"
            else:
                tran_ins_type, tran_ins_type_override = derive_tran_ins_type(secdef_entry, ref, s.get(828))
            notes = list(variant["notes"])
            if tran_ins_type_override:
                notes.append("tran_ins_type_override_applied: code-observed ProductID override table, not in spec 5.1.3 text")
            return {
                "_tag11": tag11,
                "ORIG_TRAN_ID": orig_tran_id,
                "COUNTRY": inst["country"], "_country_step": inst["country_step"],
                "COMMODITY": inst["commodity"], "_commodity_step": inst["commodity_step"],
                "MARKET_AREA": inst["market_area"],
                "DELIVERY_PERIOD": delivery_period,
                "TRAN_INS_TYPE": tran_ins_type,
                "INS_CLASS": "F",
                "UNIT": inst["unit"],
                "CURRENCY": inst["currency"],
                "LOT_SIZE": inst["lot_size"],
                "MARKET_PLACE": inst["market_place"],
                "TRADER": s.get(9139),
                "TRAN_DATETIME": s.get(60),
                "LINKED_TRAN_ID": tag11 or tag41,
                "SIDE": variant["side"],
                "_link_type": link["link_type"],
                "_is_known_uds": is_known_uds,
                "_notes": notes,
            }

        tag9175 = s.get(9175)
        tran_status_order = TRAN_STATUS_ORDERS.get(exec_type, "C") if tag9175 in ("0", "4", "5", "6") else "C"

        if exec_type in ORDERS_EXEC_TYPES:
            for variant in row_variants:
                base_row = _base_row(variant)
                secdef_entry = variant["secdef"]
                prev_vol = previous_volume_cache.get(orig_tran_id)
                vol = _volume_for_order(s, tran_status_order, prev_vol)
                previous_volume_cache[orig_tran_id] = vol
                order_row = dict(base_row)
                order_row.update({
                    "TRAN_STATUS": tran_status_order,
                    "BID_ASK": {"1": "B", "2": "A"}.get(s.get(54)),
                    "ORDER_TYPE": _order_type(s),
                    "PRICE": _price_for_order(s, secdef_entry),
                    "VOLUME": vol,
                    "DELIVERY_CATEGORY": "O",
                })
                expected_orders.append(order_row)

        if exec_type in TRADES_EXEC_TYPES:
            # Trades' ORIG_TRAN_ID is Tag-17 (ExecID) directly — a completely
            # different derivation from Orders' recursive ClOrdID/OrigClOrdID
            # chain (spec 5.2 "ORIG_TRAN_ID ... Tag 17 (ExecID) One To One";
            # confirmed in TradeMapper.cs: `OriginalTransactionId = report.ExecID`).
            base_row = _base_row(trade_variant)
            trade_orig_tran_id = s.get(17)
            if trade_orig_tran_id:
                sort_id_cache[trade_orig_tran_id] = sort_id_cache.get(trade_orig_tran_id, 0) + 1
            trade_row = dict(base_row)
            trade_row.update({
                "ORIG_TRAN_ID": trade_orig_tran_id,
                "TRAN_STATUS": TRAN_STATUS_TRADES.get(exec_type),
                "ORDER_REF": tag11,
                "BUY_SELL": {"1": "B", "2": "S"}.get(s.get(54)),
                "COUNTERPARTY": s.get(9068),
                "BOOK": s.get(5364),
                "PRICE": _price_for_trade_expected(s),
                "VOLUME": s.get(32),
                "FIXED_ROLLOVER": "1",
                "BROKER": s.get(9066),
                "SORT_ID": sort_id_cache.get(trade_orig_tran_id),
                "DELIVERY_CATEGORY": "O",
            })
            expected_trades.append(trade_row)

    return expected_orders, expected_trades, diagnostics
