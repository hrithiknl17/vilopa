from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

from vilopa.core.catalog import ForeignKey, Schema

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "examples" / "bharat-bazaar"


@pytest.fixture(scope="session")
def demo_datamap_path() -> Path:
    return DEMO_DIR / "datamap.yaml"


@pytest.fixture(scope="session")
def seed_module() -> ModuleType:
    """Import examples/bharat-bazaar/seed.py, which is a script rather than a package."""
    name = "bharat_bazaar_seed"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DEMO_DIR / "seed.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("VILOPA_APP_PG_DSN")
    if not value:
        pytest.skip("VILOPA_APP_PG_DSN not set")
    return value


@pytest.fixture
def conn(dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SELECT current_database()").fetchone()
        if not row or row[0] != "bharat_bazaar":
            pytest.skip("refusing to seed a database not named bharat_bazaar")
        yield connection


def _fk(table: str, column: str, ref_table: str, ref_column: str = "id") -> ForeignKey:
    return ForeignKey(
        f"{table}_{column}_fkey", "public", table, (column,), "public", ref_table, (ref_column,)
    )


DEMO_FOREIGN_KEYS = (
    _fk("customers", "referred_by_customer_id", "customers"),
    _fk("addresses", "customer_id", "customers"),
    _fk("orders", "customer_id", "customers"),
    _fk("orders", "address_id", "addresses"),
    _fk("order_items", "order_id", "orders"),
    _fk("order_items", "product_id", "products"),
    _fk("invoices", "order_id", "orders"),
    _fk("payments", "order_id", "orders"),
    _fk("reviews", "customer_id", "customers"),
    _fk("reviews", "product_id", "products"),
    _fk("support_tickets", "customer_id", "customers"),
    _fk("wishlists", "customer_id", "customers"),
    _fk("wishlists", "product_id", "products"),
)

DEMO_NOT_NULL = {
    "customers": ("id", "email", "full_name", "created_at"),
    "addresses": ("id", "customer_id", "line1", "city", "state", "pin_code", "is_default"),
    "products": ("id", "sku", "name", "price_paise"),
    "orders": ("id", "status", "total_paise", "placed_at"),
    "order_items": ("id", "order_id", "product_id", "quantity", "unit_price_paise"),
    "invoices": (
        "id",
        "order_id",
        "invoice_number",
        "billing_name",
        "billing_address",
        "amount_paise",
        "issued_at",
    ),
    "payments": ("id", "order_id", "provider", "amount_paise", "status"),
    "reviews": ("id", "customer_id", "product_id", "rating", "created_at"),
    "support_tickets": ("id", "contact_email", "subject", "body", "created_at"),
    "wishlists": ("customer_id", "product_id", "added_at"),
    "events": ("id", "kind", "properties", "occurred_at"),
}


@pytest.fixture(scope="session")
def demo_schema() -> Schema:
    """The structural facts of examples/bharat-bazaar/schema.sql, written out by hand.

    tests/integration/test_plan_db.py checks that the live database reports the same.
    """
    primary_keys: dict[tuple[str, str], tuple[str, ...]] = {
        ("public", t): ("id",) for t in DEMO_NOT_NULL if t != "wishlists"
    }
    primary_keys["public", "wishlists"] = ("customer_id", "product_id")
    return Schema.build(
        DEMO_FOREIGN_KEYS,
        [("public", t, c) for t, cols in DEMO_NOT_NULL.items() for c in cols],
        primary_keys,
    )
