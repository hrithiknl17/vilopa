"""Scan and lint the seeded demo shop. Runs only when VILOPA_APP_PG_DSN is set."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from typer.testing import CliRunner

from vilopa.cli import app
from vilopa.connectors import postgres
from vilopa.core import datamap
from vilopa.core.datamap import PiiCategory
from vilopa.core.lint import lint

pytestmark = pytest.mark.db

Conn = psycopg.Connection[tuple[object, ...]]
CONNECTOR = "app_pg"


@pytest.fixture
def seeded(conn: Conn, seed_module: ModuleType) -> Iterator[Conn]:
    """The demo shop, committed. Schema changes a test makes are rolled back after it."""
    seed_module.load(conn, seed_module.generate(seed=42, customers=50))
    conn.commit()
    yield conn
    conn.rollback()


@pytest.fixture
def demo_map(demo_datamap_path: Path) -> datamap.DataMap:
    return datamap.load(demo_datamap_path)


def test_every_mapped_personal_data_column_is_found(
    seeded: Conn, demo_map: datamap.DataMap
) -> None:
    findings = {r.column.ref(CONNECTOR): r.finding for r in postgres.scan(seeded)}
    missed = {}
    for table_ref, table in demo_map.tables.items():
        for name, column in table.columns.items():
            if column.pii is PiiCategory.NONE:
                continue
            ref = f"{table_ref}.{name}"
            finding = findings.get(ref)
            if finding is None or finding.category is not column.pii:
                missed[ref] = (column.pii, finding)
    assert missed == {}


def test_demo_map_lints_clean(seeded: Conn, demo_map: datamap.DataMap) -> None:
    result = lint(demo_map, CONNECTOR, postgres.scan(seeded))
    assert result.issues == []


def test_demo_map_lints_clean_without_sampling(seeded: Conn, demo_map: datamap.DataMap) -> None:
    """CI often lints a freshly migrated, empty database: names alone must be enough."""
    result = lint(demo_map, CONNECTOR, postgres.scan(seeded, sample_rows=0))
    assert result.issues == []


def test_new_unmapped_column_fails_lint(seeded: Conn, demo_map: datamap.DataMap) -> None:
    seeded.execute("ALTER TABLE customers ADD COLUMN alt_mobile text")
    result = lint(demo_map, CONNECTOR, postgres.scan(seeded))
    assert [i.ref for i in result.errors] == ["app_pg.public.customers.alt_mobile"]


def test_badly_named_column_is_caught_by_its_values(
    seeded: Conn, demo_map: datamap.DataMap
) -> None:
    seeded.execute("ALTER TABLE orders ADD COLUMN extra text")
    seeded.execute(
        "UPDATE orders o SET extra = c.email FROM customers c WHERE c.id = o.customer_id"
    )
    result = lint(demo_map, CONNECTOR, postgres.scan(seeded))
    assert [i.ref for i in result.errors] == ["app_pg.public.orders.extra"]
    assert "looks like email" in result.errors[0].message


def test_dropped_column_fails_lint(seeded: Conn, demo_map: datamap.DataMap) -> None:
    seeded.execute("ALTER TABLE customers DROP COLUMN pan")
    result = lint(demo_map, CONNECTOR, postgres.scan(seeded))
    assert [(i.ref, i.message) for i in result.errors] == [
        ("app_pg.public.customers.pan", "in the data map but not in the database")
    ]


def test_reports_never_carry_values(seeded: Conn) -> None:
    for report in postgres.scan(seeded):
        if report.finding is not None:
            for reason in report.finding.reasons:
                assert "@" not in reason
                assert "+91" not in reason


def test_connect_is_read_only(dsn: str) -> None:
    with (
        postgres.connect(dsn) as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("CREATE TABLE vilopa_must_not_exist ()")


runner = CliRunner()


@pytest.mark.usefixtures("seeded")
def test_cli_lint_passes_on_the_demo(demo_datamap_path: Path) -> None:
    result = runner.invoke(app, ["lint", str(demo_datamap_path)])
    assert result.exit_code == 0, result.output
    assert "app_pg: 0 error(s), 0 warning(s)" in result.output


@pytest.mark.usefixtures("seeded")
def test_cli_scan_json() -> None:
    result = runner.invoke(
        app, ["scan", "--dsn-env", "VILOPA_APP_PG_DSN", "--connector", CONNECTOR, "--json"]
    )
    assert result.exit_code == 0, result.output
    by_ref = {row["ref"]: row for row in json.loads(result.output)}
    email = by_ref["app_pg.public.customers.email"]
    assert (email["category"], email["confidence"]) == ("email", "high")
