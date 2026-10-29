"""Guess whether a column holds personal data, from its name, its type and sampled values.

Detection returns categories, confidences and counts, never values. Sampled values live in
memory for one call and are not logged, printed or stored, so a scan report is safe to
paste into a pull request or a CI log.

The rules are deliberately boring: regular expressions over column names, and validators
for identifiers that have a fixed shape (email, Indian mobile, PAN, GSTIN, Aadhaar, UPI ID,
IP address, PIN code). A name that suggests a structured identifier is downgraded when
enough values were sampled and none of them looks like one.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum

from vilopa.core.catalog import ColumnInfo
from vilopa.core.datamap import PiiCategory

MIN_SAMPLES = 5
"""Fewer non-empty samples than this cap value-only evidence at medium confidence."""
FULL_MATCH_RATIO = 0.8
"""Share of sampled values that must wholly be one identifier for a value-based finding."""
EMBEDDED_RATIO = 0.05
EMBEDDED_HIGH_RATIO = 0.2
"""Shares of sampled values with an email or phone inside for free text (medium, high)."""

TEXT_TYPES = frozenset(
    {"text", "character varying", "character", "citext", "name", "json", "jsonb", "ARRAY"}
)
TEMPORAL_TYPES = frozenset(
    {
        "date",
        "timestamp with time zone",
        "timestamp without time zone",
        "time with time zone",
        "time without time zone",
        "interval",
    }
)
KEY_TYPES = frozenset({"smallint", "integer", "bigint", "uuid"})
NEVER_PII_TYPES = frozenset({"boolean"})
IP_TYPES = frozenset({"inet", "cidr"})


class Confidence(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    def __str__(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Finding:
    category: PiiCategory
    confidence: Confidence
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ColumnReport:
    """What a scan concluded about one live column. `sampled` counts non-null values read."""

    column: ColumnInfo
    sampled: int
    finding: Finding | None


def classify(column: ColumnInfo, samples: Sequence[str] = ()) -> Finding | None:
    """Best guess for one column, or None when nothing suggests personal data.

    `samples` are the column's non-null values rendered as text. They are counted, not kept.
    """
    if column.data_type in NEVER_PII_TYPES:
        return None
    if column.data_type in IP_TYPES:
        return Finding(
            PiiCategory.IP_ADDRESS, Confidence.HIGH, (f"column type is {column.data_type}",)
        )

    values = [v.strip() for v in samples if v and v.strip()]
    n = len(values)
    name_hit = _from_name(column)
    reasons = [f"name suggests {name_hit[0]}"] if name_hit else []

    # 1. Values that are wholly one identifier.
    best = _best_full_match(values, textual=column.data_type in TEXT_TYPES)
    if best is not None:
        category, count = best
        reasons.append(f"{count}/{n} sampled values look like {category}")
        if name_hit is not None and name_hit[0] is category:
            confidence = Confidence.HIGH
        elif category is PiiCategory.PIN_CODE:
            confidence = Confidence.LOW  # any six-digit code looks like a PIN
        else:
            confidence = Confidence.HIGH if n >= MIN_SAMPLES else Confidence.MEDIUM
        return Finding(category, confidence, tuple(reasons))

    # 2. Text with identifiers inside it. A name without a fixed shape (an address, a
    # name, notes) keeps its category; anything else becomes free text.
    embedded = sum(1 for v in values if _EMAIL.search(v) or _PHONE.search(v))
    if embedded and embedded / n >= EMBEDDED_RATIO:
        reasons.append(f"{embedded}/{n} sampled values contain an email address or phone number")
        if name_hit is not None and name_hit[0] not in _DETECTORS:
            return Finding(name_hit[0], Confidence.HIGH, tuple(reasons))
        high = embedded / n >= EMBEDDED_HIGH_RATIO
        confidence = Confidence.HIGH if high else Confidence.MEDIUM
        return Finding(PiiCategory.FREE_TEXT, confidence, tuple(reasons))

    # 3. The name alone, checked against the values where the category has a fixed shape.
    if name_hit is None:
        return None
    category, confidence = name_hit
    detector = _DETECTORS.get(category)
    if detector is not None and n >= MIN_SAMPLES:
        count = sum(1 for v in values if detector(v))
        reasons.append(f"{count}/{n} sampled values look like {category}")
        if count == 0:
            confidence = Confidence.LOW
    return Finding(category, confidence, tuple(reasons))


# --- names -------------------------------------------------------------------------------


def _words(*alternatives: str) -> re.Pattern[str]:
    """Match any alternative as whole underscore-separated words of a normalised name."""
    return re.compile(rf"(?:^|_)(?:{'|'.join(alternatives)})(?:_|$)")


# First match wins, so more specific rules come first (email_address is an email, not an
# address; ip_address is an IP).
_NAME_RULES: tuple[tuple[PiiCategory, Confidence, re.Pattern[str]], ...] = (
    (PiiCategory.EMAIL, Confidence.HIGH, _words(r"e_?mail")),
    (PiiCategory.IP_ADDRESS, Confidence.HIGH, _words("ip", "ipv4", "ipv6", r"ip_?address")),
    (PiiCategory.IP_ADDRESS, Confidence.HIGH, _words("remote_addr")),
    (
        PiiCategory.PHONE,
        Confidence.HIGH,
        _words("phone", "mobile", "msisdn", "whatsapp", "tel", "telephone", r"contact_(?:no|num)"),
    ),
    (PiiCategory.AADHAAR, Confidence.HIGH, _words("aadhaar", "aadhar", "adhaar", "uidai")),
    (PiiCategory.GSTIN, Confidence.HIGH, _words("gstin", r"gst_?(?:no|num|number)")),
    (PiiCategory.PAN, Confidence.HIGH, _words("pan")),
    (PiiCategory.UPI_ID, Confidence.HIGH, _words("upi", "vpa")),
    (PiiCategory.DOB, Confidence.HIGH, _words("dob", "date_of_birth", r"birth_?date", "birthday")),
    (
        PiiCategory.PIN_CODE,
        Confidence.HIGH,
        _words(r"pin_?code", r"post_?code", r"postal_?code", r"zip(?:_?code)?"),
    ),
    (
        PiiCategory.NAME,
        Confidence.HIGH,
        _words(
            r"(?:first|last|middle|full|given|family|sur|nick|display|legal|preferred|billing"
            r"|shipping|customer|user|contact|person|holder|recipient|father|mother|guardian)"
            r"_?name",
            "fname",
            "lname",
        ),
    ),
    (
        PiiCategory.PAYMENT_REF,
        Confidence.MEDIUM,
        _words(
            r"card_?(?:number|no|num)",
            r"account_?(?:number|no|num)",
            "acct_no",
            "iban",
            "ifsc",
            "utr",
            r"(?:provider|payment|gateway|pg)_ref(?:erence)?",
            r"(?:txn|transaction)_(?:id|ref|reference)",
        ),
    ),
    (
        PiiCategory.ADDRESS,
        Confidence.MEDIUM,
        _words("address", "addr", "street", "landmark", "locality", r"line_?\d", r"house_?no"),
    ),
    (PiiCategory.ADDRESS, Confidence.LOW, _words("city", "district", "town", "village", "taluka")),
    (
        PiiCategory.FREE_TEXT,
        Confidence.MEDIUM,
        _words(
            r"notes?",
            r"comments?",
            r"remarks?",
            r"messages?",
            "body",
            "feedback",
            "bio",
            r"instructions?",
        ),
    ),
    (PiiCategory.FREE_TEXT, Confidence.LOW, _words("description", "details")),
    (PiiCategory.NAME, Confidence.LOW, _words("name")),
)

# Columns about an identifier rather than holding it: email_verified, phone_type, sms_sent_at.
_ABOUT_SUFFIX = re.compile(
    r"_(?:count|at|verified|enabled|confirmed|type|status|flag|opt_?in|sent|bounced|valid)$"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    return _NON_WORD.sub("_", _CAMEL.sub("_", name).lower()).strip("_")


def _from_name(column: ColumnInfo) -> tuple[PiiCategory, Confidence] | None:
    name = _normalize(column.name)
    if _ABOUT_SUFFIX.search(name):
        return None
    if name.endswith("_id") and column.data_type in KEY_TYPES:
        return None  # a key into another table (address_id), not the data itself
    for category, confidence, pattern in _NAME_RULES:
        if pattern.search(name):
            if column.data_type in TEMPORAL_TYPES and category is not PiiCategory.DOB:
                return None
            return category, confidence
    return None


# --- values ------------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_UPI = re.compile(r"[A-Za-z0-9._-]{2,256}@[A-Za-z]{2,64}")
# Indian mobile: optional +91 / 91 / 0 prefix, then 10 digits starting 6-9, optionally
# split 5-5 by a space or hyphen.
_PHONE = re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
_PAN = re.compile(r"[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]")
_GSTIN = re.compile(r"\d{2}[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
_AADHAAR = re.compile(r"[2-9]\d{3} ?\d{4} ?\d{4}")
_PIN = re.compile(r"[1-9]\d{2} ?\d{3}")

# Verhoeff tables, for the Aadhaar check digit.
_V_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_V_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_valid(digits: str) -> bool:
    check = 0
    for i, digit in enumerate(reversed(digits)):
        check = _V_D[check][_V_P[i % 8][int(digit)]]
    return check == 0


def _is_email(v: str) -> bool:
    return _EMAIL.fullmatch(v) is not None


def _is_upi(v: str) -> bool:
    return _UPI.fullmatch(v) is not None


def _is_phone(v: str) -> bool:
    return _PHONE.fullmatch(v) is not None


def _is_pan(v: str) -> bool:
    return _PAN.fullmatch(v) is not None


def _is_gstin(v: str) -> bool:
    return _GSTIN.fullmatch(v) is not None


def _is_aadhaar(v: str) -> bool:
    return _AADHAAR.fullmatch(v) is not None and verhoeff_valid(v.replace(" ", ""))


def _is_ip(v: str) -> bool:
    try:
        ipaddress.ip_interface(v)
    except ValueError:
        return False
    return True


def _is_pin(v: str) -> bool:
    return _PIN.fullmatch(v) is not None


# Order breaks ties between detectors with the same hit count.
_DETECTORS: dict[PiiCategory, Callable[[str], bool]] = {
    PiiCategory.EMAIL: _is_email,
    PiiCategory.UPI_ID: _is_upi,
    PiiCategory.PHONE: _is_phone,
    PiiCategory.GSTIN: _is_gstin,
    PiiCategory.PAN: _is_pan,
    PiiCategory.AADHAAR: _is_aadhaar,
    PiiCategory.IP_ADDRESS: _is_ip,
    PiiCategory.PIN_CODE: _is_pin,
}


def _best_full_match(values: Sequence[str], *, textual: bool) -> tuple[PiiCategory, int] | None:
    if not values:
        return None
    best: tuple[PiiCategory, int] | None = None
    for category, detector in _DETECTORS.items():
        if category is PiiCategory.PIN_CODE and not textual:
            continue  # six-digit amounts in paise are not PIN codes
        count = sum(1 for v in values if detector(v))
        if count / len(values) >= FULL_MATCH_RATIO and (best is None or count > best[1]):
            best = (category, count)
    return best
