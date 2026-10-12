"""The demo dataset is the test oracle for every erasure test, so it gets tested first."""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any

import pytest

Dataset = dict[str, list[dict[str, Any]]]
TOKEN = re.compile(r"[0-9a-f]{12}")


@pytest.fixture(scope="module")
def data(seed_module: ModuleType) -> Dataset:
    result: Dataset = seed_module.generate(seed=7, customers=120)
    return result


def tokens_by_customer(data: Dataset) -> dict[int, str]:
    return {c["id"]: c["full_name"].rsplit(" ", 1)[1] for c in data["customers"]}


def test_generation_is_deterministic(seed_module: ModuleType) -> None:
    assert seed_module.generate(seed=3, customers=20) == seed_module.generate(seed=3, customers=20)
    assert seed_module.generate(seed=3, customers=20) != seed_module.generate(seed=4, customers=20)


def test_every_table_has_rows(data: Dataset, seed_module: ModuleType) -> None:
    assert set(data) == set(seed_module.TABLES)
    assert all(data[table] for table in seed_module.TABLES)


def test_canaries_are_unique(data: Dataset) -> None:
    customers = data["customers"]
    for field in ("email", "phone"):
        values = [c[field] for c in customers]
        assert len(values) == len(set(values)), field
    tokens = tokens_by_customer(data)
    assert len(set(tokens.values())) == len(customers)


def test_canary_token_is_in_every_personal_field(data: Dataset) -> None:
    tokens = tokens_by_customer(data)
    order_owner = {o["id"]: o["customer_id"] for o in data["orders"]}
    checks: list[tuple[str, str, Any]] = [
        ("customers", "email", lambda r: r["id"]),
        ("customers", "full_name", lambda r: r["id"]),
        ("addresses", "line2", lambda r: r["customer_id"]),
        ("orders", "delivery_notes", lambda r: r["customer_id"]),
        ("invoices", "billing_name", lambda r: order_owner[r["order_id"]]),
        ("invoices", "billing_address", lambda r: order_owner[r["order_id"]]),
        ("payments", "upi_id", lambda r: order_owner[r["order_id"]]),
        ("reviews", "body", lambda r: r["customer_id"]),
        ("support_tickets", "contact_email", lambda r: _ticket_owner(data, r)),
        ("support_tickets", "body", lambda r: _ticket_owner(data, r)),
    ]
    for table, field, owner in checks:
        for row in data[table]:
            if row[field] is not None:
                assert tokens[owner(row)] in row[field], (table, field, row["id"])


def test_no_row_carries_two_customers_tokens(data: Dataset) -> None:
    """If a row mixed two people, a bystander check could never pass. Guard the oracle."""
    known = set(tokens_by_customer(data).values())
    for table, rows in data.items():
        for row in rows:
            found = {t for t in TOKEN.findall(repr(row)) if t in known}
            assert len(found) <= 1, (table, row)


def test_foreign_keys_are_consistent(data: Dataset) -> None:
    ids = {table: {r["id"] for r in rows if "id" in r} for table, rows in data.items()}
    address_owner = {a["id"]: a["customer_id"] for a in data["addresses"]}

    for c in data["customers"]:
        ref = c["referred_by_customer_id"]
        assert ref is None or ref < c["id"], "referrers must be inserted first"
    for o in data["orders"]:
        assert address_owner[o["address_id"]] == o["customer_id"], "order uses own address"
    for table, column, parent in [
        ("addresses", "customer_id", "customers"),
        ("order_items", "order_id", "orders"),
        ("order_items", "product_id", "products"),
        ("invoices", "order_id", "orders"),
        ("payments", "order_id", "orders"),
        ("reviews", "customer_id", "customers"),
        ("wishlists", "customer_id", "customers"),
        ("events", "customer_id", "customers"),
    ]:
        assert {r[column] for r in data[table]} <= ids[parent], (table, column)


def test_seed_covers_edge_cases_the_planner_must_handle(data: Dataset) -> None:
    assert any(c["referred_by_customer_id"] for c in data["customers"]), "self-referencing FK"
    assert any(t["customer_id"] is None for t in data["support_tickets"]), "guest tickets"
    assert any(p["upi_id"] for p in data["payments"]), "UPI ids to pseudonymise"
    assert any(e["properties"].get("email") for e in data["events"]), "PII inside jsonb"
    phones = [a["phone"] for a in data["addresses"]]
    assert any(p.startswith("+91 ") for p in phones), "spaced +91 format"
    assert any(p.startswith("0") for p in phones), "leading-zero format"


def test_phone_variants_share_the_same_ten_digits(seed_module: ModuleType) -> None:
    variants = seed_module.phone_variants("9876543210")
    assert {re.sub(r"\D", "", v)[-10:] for v in variants} == {"9876543210"}


def _ticket_owner(data: Dataset, ticket: dict[str, Any]) -> int:
    if ticket["customer_id"] is not None:
        return int(ticket["customer_id"])
    owner = next(c for c in data["customers"] if c["email"] == ticket["contact_email"])
    return int(owner["id"])
