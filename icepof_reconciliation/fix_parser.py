"""
Parse raw ICEPOF FIX 4.2 message bodies (SOH-delimited tag=value strings) as stored in
the `api_messages` Azure File Share (columns: ReceivedAtUtc, SequenceNumber, Body).

IMPORTANT: Security Definition (35=d) messages are BATCHED — a single message can carry
up to 100 full security entries, each repeating the entire instrument-field set (tag 311
UnderlyingSymbol as the anchor, ~60 other fields per entry, occurrence count always equal
to the number of 311 occurrences in that message). Confirmed against real 2026-07-13 data:
844/1166 SECDEF messages carry exactly 100 securities each. A naive "first value per tag"
scalar parse silently drops 99% of the batch. `parse_security_definition_message` handles
this properly via a generic repeating-entry extractor; `parse_fix_message` (scalar-only,
plus the two smaller repeating groups below) remains correct for Execution Reports and
User Defined Strategy messages, which are confirmed NOT batched (always exactly one
Symbol/tag-55 per message).

Two smaller repeating groups are handled for non-batched messages (UDS), per samples
observed in real data:
  - Leg group (User Defined Strategy legs): anchor tag 600 (LegSymbol), member tags
    609, 623, 624, 9566, 9567, 9623, 9624.
  - SRS pricing-table group (nested within a security/strategy entry): anchor tag 9071,
    member tags 9072, 9073.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

import pandas as pd

FIX_SOH = "\x01"

LEG_GROUP_ANCHOR = 600
LEG_GROUP_FIELDS = {609, 623, 624, 9566, 9567, 9623, 9624}

SRS_GROUP_ANCHOR = 9071
SRS_GROUP_FIELDS = {9072, 9073}


def split_fix(body: str) -> list[tuple[int, str]]:
    """Return the ordered list of (tag, value) pairs from a raw FIX body string."""
    pairs: list[tuple[int, str]] = []
    for chunk in body.split(FIX_SOH):
        if not chunk or "=" not in chunk:
            continue
        tag_str, _, value = chunk.partition("=")
        tag_str = tag_str.strip()
        if not tag_str.isdigit():
            continue
        pairs.append((int(tag_str), value))
    return pairs


@dataclass
class ParsedFixMessage:
    scalar: dict[int, str]
    all_values: dict[int, list[str]]
    legs: list[dict[int, str]] = field(default_factory=list)
    srs_legs: list[dict[int, str]] = field(default_factory=list)

    @property
    def msg_type(self) -> str | None:
        return self.scalar.get(35)

    def get(self, tag: int, default=None):
        return self.scalar.get(tag, default)


def parse_fix_message(body: str) -> ParsedFixMessage:
    pairs = split_fix(body)

    scalar: dict[int, str] = {}
    all_values: dict[int, list[str]] = {}
    legs: list[dict[int, str]] = []
    srs_legs: list[dict[int, str]] = []
    current_leg: dict[int, str] | None = None
    current_srs: dict[int, str] | None = None

    for tag, value in pairs:
        all_values.setdefault(tag, []).append(value)
        if tag not in scalar:
            scalar[tag] = value

        if tag == LEG_GROUP_ANCHOR:
            current_leg = {tag: value}
            legs.append(current_leg)
        elif tag in LEG_GROUP_FIELDS and current_leg is not None:
            current_leg[tag] = value
        elif tag == SRS_GROUP_ANCHOR:
            current_srs = {tag: value}
            srs_legs.append(current_srs)
        elif tag in SRS_GROUP_FIELDS and current_srs is not None:
            current_srs[tag] = value

    return ParsedFixMessage(scalar=scalar, all_values=all_values, legs=legs, srs_legs=srs_legs)


# ---- Batched Security Definition parsing (35=d) -----------------------------------------

@dataclass
class SecurityEntry:
    fields: dict[int, str]
    srs_legs: list[dict[int, str]] = field(default_factory=list)

    def get(self, tag: int, default=None):
        return self.fields.get(tag, default)


@dataclass
class ParsedSecDefMessage:
    header: dict[int, str]
    entries: list[SecurityEntry]

    @property
    def msg_type(self) -> str | None:
        return self.header.get(35)

    def is_reject(self) -> bool:
        return 58 in self.header and not self.entries


def parse_security_definition_message(body: str) -> ParsedSecDefMessage:
    """
    Generic repeating-entry parse anchored on tag 311 (UnderlyingSymbol), with the
    nested SRS pricing-table sub-group (anchor 9071, fields 9072/9073) collected per
    entry rather than flattened globally. Tags appearing before the first 311 are
    message-level header fields (first-occurrence wins).
    """
    pairs = split_fix(body)

    header: dict[int, str] = {}
    entries: list[SecurityEntry] = []
    current: SecurityEntry | None = None
    current_srs: dict[int, str] | None = None
    seen_anchor = False

    for tag, value in pairs:
        if tag == 311:
            current = SecurityEntry(fields={tag: value})
            entries.append(current)
            current_srs = None
            seen_anchor = True
            continue

        if not seen_anchor:
            if tag not in header:
                header[tag] = value
            continue

        assert current is not None
        if tag == SRS_GROUP_ANCHOR:
            current_srs = {tag: value}
            current.srs_legs.append(current_srs)
        elif tag in SRS_GROUP_FIELDS and current_srs is not None:
            current_srs[tag] = value
        else:
            current.fields[tag] = value

    return ParsedSecDefMessage(header=header, entries=entries)


# ---- Loading a day's raw parquet files -------------------------------------------------

RAW_MSG_TYPES = {
    "execution_report": "Execution_Reports",
    "security_definition": "Security_Definitions",
    "user_defined_strategy": "User_Defined_Strategies",
}


def load_raw_messages(raw_dir: str, date_str: str, msg_type_key: str) -> pd.DataFrame:
    """
    Load and parse all raw parquet files for one message type on one day.

    raw_dir: folder containing the flat *.parquet files (as downloaded from
             shared/icepof/api_messages/).
    date_str: e.g. "2026-07-13".
    msg_type_key: one of RAW_MSG_TYPES keys.

    Returns a DataFrame with one row per FIX message: ReceivedAtUtc, SequenceNumber,
    SourceFile, ParsedMessage (ParsedFixMessage object), plus every scalar tag flattened
    into its own `tag_<N>` column for convenience.
    """
    folder_name = RAW_MSG_TYPES[msg_type_key]
    pattern = os.path.join(raw_dir, f"{date_str}_{folder_name}_*.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No raw files found for pattern {pattern}")

    rows = []
    for fp in files:
        df = pd.read_parquet(fp)
        source_file = os.path.basename(fp)
        for _, r in df.iterrows():
            parsed = parse_fix_message(str(r["Body"]))
            rows.append(
                {
                    "ReceivedAtUtc": r["ReceivedAtUtc"],
                    "SequenceNumber": r["SequenceNumber"],
                    "SourceFile": source_file,
                    "ParsedMessage": parsed,
                }
            )

    out = pd.DataFrame(rows)

    # Flatten scalar tags into tag_<N> columns for easy vectorized access/debugging.
    all_tags: set[int] = set()
    for pm in out["ParsedMessage"]:
        all_tags.update(pm.scalar.keys())
    for tag in sorted(all_tags):
        out[f"tag_{tag}"] = out["ParsedMessage"].map(lambda pm, t=tag: pm.scalar.get(t))

    return out


def load_raw_secdef_messages(raw_dir: str, date_str: str) -> pd.DataFrame:
    """
    Like load_raw_messages, but for Security_Definitions specifically: each raw parquet
    row is parsed with parse_security_definition_message (batched, up to 100 securities
    per message), and returned one row per FIX *message* with a `ParsedSecDef` column
    (a ParsedSecDefMessage whose `.entries` list holds every security in that message).
    """
    pattern = os.path.join(raw_dir, f"{date_str}_{RAW_MSG_TYPES['security_definition']}_*.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No raw files found for pattern {pattern}")

    rows = []
    for fp in files:
        df = pd.read_parquet(fp)
        source_file = os.path.basename(fp)
        for _, r in df.iterrows():
            parsed = parse_security_definition_message(str(r["Body"]))
            rows.append(
                {
                    "ReceivedAtUtc": r["ReceivedAtUtc"],
                    "SequenceNumber": r["SequenceNumber"],
                    "SourceFile": source_file,
                    "ParsedSecDef": parsed,
                }
            )
    return pd.DataFrame(rows)
