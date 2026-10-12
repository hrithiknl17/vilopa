from __future__ import annotations

import pytest

from mita.core.catalog import ColumnInfo
from mita.core.datamap import PiiCategory as P
from mita.core.detect import Confidence as C
from mita.core.detect import classify, verhoeff_valid


def col(name: str, data_type: str = "text") -> ColumnInfo:
    return ColumnInfo("public", "t", name, data_type)


def with_check_digit(base: str) -> str:
    """Complete an 11-digit string into a Verhoeff-valid 12-digit Aadhaar-shaped number."""
    return next(base + d for d in "0123456789" if verhoeff_valid(base + d))


@pytest.mark.parametrize(
    ("name", "data_type", "category", "confidence"),
    [
        ("email", "text", P.EMAIL, C.HIGH),
        ("contactEmail", "text", P.EMAIL, C.HIGH),
        ("email_id", "character varying", P.EMAIL, C.HIGH),
        ("phone", "text", P.PHONE, C.HIGH),
        ("alt_mobile", "text", P.PHONE, C.HIGH),
        ("whatsapp_no", "text", P.PHONE, C.HIGH),
        ("full_name", "text", P.NAME, C.HIGH),
        ("billing_name", "text", P.NAME, C.HIGH),
        ("FirstName", "text", P.NAME, C.HIGH),
        ("name", "text", P.NAME, C.LOW),
        ("date_of_birth", "date", P.DOB, C.HIGH),
        ("pan", "text", P.PAN, C.HIGH),
        ("aadhaar_number", "text", P.AADHAAR, C.HIGH),
        ("gstin", "text", P.GSTIN, C.HIGH),
        ("upi_id", "text", P.UPI_ID, C.HIGH),
        ("pin_code", "text", P.PIN_CODE, C.HIGH),
        ("postcode", "text", P.PIN_CODE, C.HIGH),
        ("client_ip", "text", P.IP_ADDRESS, C.HIGH),
        ("ip_address", "text", P.IP_ADDRESS, C.HIGH),
        ("anything", "inet", P.IP_ADDRESS, C.HIGH),
        ("line1", "text", P.ADDRESS, C.MEDIUM),
        ("billing_address", "text", P.ADDRESS, C.MEDIUM),
        ("city", "text", P.ADDRESS, C.LOW),
        ("delivery_notes", "text", P.FREE_TEXT, C.MEDIUM),
        ("body", "text", P.FREE_TEXT, C.MEDIUM),
        ("provider_ref", "text", P.PAYMENT_REF, C.MEDIUM),
        ("card_number", "text", P.PAYMENT_REF, C.MEDIUM),
    ],
)
def test_name_rules(name: str, data_type: str, category: P, confidence: C) -> None:
    finding = classify(col(name, data_type))
    assert finding is not None
    assert (finding.category, finding.confidence) == (category, confidence)


@pytest.mark.parametrize(
    ("name", "data_type"),
    [
        ("id", "bigint"),
        ("customer_id", "bigint"),
        ("address_id", "bigint"),
        ("email_verified", "boolean"),
        ("email_verified", "text"),
        ("phone_type", "text"),
        ("email_count", "integer"),
        ("last_login_at", "timestamp with time zone"),
        ("phone_updated", "timestamp with time zone"),
        ("status", "text"),
        ("sku", "text"),
        ("shipment", "text"),
        ("company", "text"),
        ("is_default", "boolean"),
    ],
)
def test_names_that_are_not_personal_data(name: str, data_type: str) -> None:
    assert classify(col(name, data_type)) is None


PHONES = ["+919876543210", "+91 98765 43210", "09876543210", "9876543210", "+91-98765-43210"]


def test_indian_mobile_formats_are_one_category() -> None:
    finding = classify(col("contact"), PHONES)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.PHONE, C.HIGH)


def test_values_identify_a_column_with_an_unhelpful_name() -> None:
    emails = [f"user{i}@example.in" for i in range(10)]
    finding = classify(col("login"), emails)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.EMAIL, C.HIGH)
    assert finding.reasons == ("10/10 sampled values look like email",)


def test_reasons_never_contain_values() -> None:
    finding = classify(col("contact"), PHONES)
    assert finding is not None
    for reason in finding.reasons:
        for value in PHONES:
            assert value not in reason


def test_upi_ids_are_not_emails() -> None:
    finding = classify(col("handle"), [f"ravi.{i}@okaxis" for i in range(6)])
    assert finding is not None
    assert finding.category is P.UPI_ID


@pytest.mark.parametrize(
    ("values", "category"),
    [
        (["ABCPK1234Z", "XYZPS0001A", "LMNPR9876Q", "QWEPT1111B", "ASDPV2222C"], P.PAN),
        (["27AAPFU0939F1ZV"] * 5, P.GSTIN),
        (["10.0.0.1", "2001:db8::1", "192.168.1.5/32", "8.8.8.8", "1.1.1.1"], P.IP_ADDRESS),
    ],
)
def test_fixed_shape_identifiers(values: list[str], category: P) -> None:
    finding = classify(col("value"), values)
    assert finding is not None
    assert finding.category is category


def test_aadhaar_needs_a_valid_check_digit() -> None:
    valid = [with_check_digit(f"2345678901{i}") for i in range(6)]
    finding = classify(col("doc_number"), valid)
    assert finding is not None
    assert finding.category is P.AADHAAR

    invalid = [v[:-1] + str((int(v[-1]) + 1) % 10) for v in valid]
    assert classify(col("doc_number"), invalid) is None


def test_verhoeff() -> None:
    assert verhoeff_valid("2363")
    assert not verhoeff_valid("2364")


def test_free_text_with_phone_numbers_inside() -> None:
    notes = ["Leave at the gate"] * 7 + [f"Call {p} before delivery" for p in PHONES[:3]]
    finding = classify(col("extra"), notes)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.FREE_TEXT, C.HIGH)


def test_rare_identifiers_in_free_text_are_medium() -> None:
    notes = ["Leave at the gate"] * 14 + ["Mail me at a@b.example"]
    finding = classify(col("extra"), notes)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.FREE_TEXT, C.MEDIUM)


def test_identifiers_inside_an_address_keep_it_an_address() -> None:
    lines = ["12, MG Road"] * 4 + ["Flat 3, call 9876543210 at gate"]
    finding = classify(col("address_line"), lines)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.ADDRESS, C.HIGH)


def test_json_with_an_email_inside() -> None:
    props = ['{"path": "/"}'] * 4 + ['{"path": "/login", "email": "a@b.example"}']
    finding = classify(col("properties", "jsonb"), props)
    assert finding is not None
    assert finding.category is P.FREE_TEXT


def test_name_is_downgraded_when_values_disagree() -> None:
    finding = classify(col("email"), ["Welcome to Bharat Bazaar"] * 10)
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.EMAIL, C.LOW)
    assert "0/10 sampled values look like email" in finding.reasons


def test_few_samples_cap_value_evidence_at_medium() -> None:
    finding = classify(col("contact"), ["a@b.example", "c@d.example"])
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.EMAIL, C.MEDIUM)


def test_six_digit_text_is_only_a_weak_pin_code_hint() -> None:
    finding = classify(col("code"), ["400001", "110011", "560034", "700019", "600028"])
    assert finding is not None
    assert (finding.category, finding.confidence) == (P.PIN_CODE, C.LOW)


def test_amounts_in_paise_are_not_pin_codes() -> None:
    amounts = ["459900", "129900", "999900", "250000", "100100"]
    assert classify(col("price_paise", "bigint"), amounts) is None


def test_nulls_and_blanks_are_ignored() -> None:
    finding = classify(col("contact"), ["", "  ", *PHONES])
    assert finding is not None
    assert finding.reasons == ("5/5 sampled values look like phone",)
