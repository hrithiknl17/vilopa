"""Load the demo shop into a real Postgres. Runs only when VILOPA_APP_PG_DSN is set."""

from __future__ import annotations

from types import ModuleType

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.db


def test_seed_loads_and_counts_match(
    conn: psycopg.Connection[tuple[object, ...]], seed_module: ModuleType
) -> None:
    data = seed_module.generate(seed=11, customers=60)
    counts = seed_module.load(conn, data)
    for table, expected in counts.items():
        row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        assert row.fetchone() == (expected,), table


def test_identity_sequences_continue_after_seed(
    conn: psycopg.Connection[tuple[object, ...]], seed_module: ModuleType
) -> None:
    seed_module.load(conn, seed_module.generate(seed=11, customers=10))
    new_id = conn.execute(
        "INSERT INTO products (sku, name, price_paise) VALUES ('T-1', 'Test', 100) RETURNING id"
    ).fetchone()
    assert new_id is not None
    assert new_id[0] == len(seed_module.PRODUCTS) + 1
    conn.rollback()


def test_events_link_has_no_foreign_key(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """The demo relies on events.customer_id being an undeclared link."""
    row = conn.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.events'::regclass "
        "AND contype = 'f'"
    ).fetchone()
    assert row == (0,)
