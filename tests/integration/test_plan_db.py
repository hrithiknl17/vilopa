"""Plan against the seeded demo shop. Runs only when VILOPA_APP_PG_DSN is set."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from vilopa.cli import app
from vilopa.connectors import postgres
from vilopa.core.catalog import Schema

pytestmark = pytest.mark.db

Conn = psycopg.Connection[tuple[object, ...]]
runner = CliRunner()


@pytest.fixture
def seeded(conn: Conn, seed_module: ModuleType) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = seed_module.generate(seed=42, customers=50)
    seed_module.load(conn, data)
    conn.commit()
    return data


def test_schema_info_matches_the_hand_written_facts(
    seeded: dict[str, list[dict[str, Any]]], conn: Conn, demo_schema: Schema
) -> None:
    live = postgres.schema_info(conn)
    public = [fk for fk in live.foreign_keys if fk.schema == "public"]
    assert {(fk.name, fk.table, fk.columns, fk.ref_table, fk.ref_columns) for fk in public} == {
        (fk.name, fk.table, fk.columns, fk.ref_table, fk.ref_columns)
        for fk in demo_schema.foreign_keys
    }
    assert {t for t in live.not_null if t[0] == "public"} == demo_schema.not_null
    assert {k: v for k, v in live.primary_keys.items() if k[0] == "public"} == (
        demo_schema.primary_keys
    )


def test_plan_counts_one_customer(
    seeded: dict[str, list[dict[str, Any]]], demo_datamap_path: Path
) -> None:
    customer = seeded["customers"][0]
    result = runner.invoke(
        app,
        ["plan", str(demo_datamap_path), "--principal", f"email:{customer['email']}", "--json"],
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    targets = {k.removeprefix("app_pg.public."): v for k, v in out["estimate"]["targets"].items()}
    cid = customer["id"]
    assert targets["customers"] == 1
    assert targets["addresses"] == sum(1 for a in seeded["addresses"] if a["customer_id"] == cid)
    assert targets["events"] == sum(1 for e in seeded["events"] if e["customer_id"] == cid)
    assert out["blast"] == []


def test_plan_finds_by_phone_in_any_format(
    seeded: dict[str, list[dict[str, Any]]], demo_datamap_path: Path
) -> None:
    customer = seeded["customers"][3]
    mobile = customer["phone"].removeprefix("+91")
    result = runner.invoke(
        app,
        ["plan", str(demo_datamap_path), "--principal", f"phone:0{mobile}", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["estimate"]["targets"]["app_pg.public.customers"] == 1


def test_blast_guard_refuses_too_many_rows(
    seeded: dict[str, list[dict[str, Any]]], demo_datamap_path: Path
) -> None:
    customer = seeded["customers"][0]
    events = sum(1 for e in seeded["events"] if e["customer_id"] == customer["id"])
    args = ["plan", str(demo_datamap_path), "--principal", f"user_id:{customer['id']}"]
    result = runner.invoke(app, [*args, "--max-rows", str(events - 1)])
    assert result.exit_code == 1
    assert f"app_pg.public.events: delete touches {events} rows" in result.output
    assert "plan refused" in result.output


def test_identifiers_of_two_people_are_refused(
    seeded: dict[str, list[dict[str, Any]]], demo_datamap_path: Path
) -> None:
    a, b = seeded["customers"][:2]
    principal = ["--principal", f"email:{a['email']}", "--principal", f"phone:{b['phone']}"]
    result = runner.invoke(app, ["plan", str(demo_datamap_path), *principal])
    assert result.exit_code == 1
    assert "the identifiers match 2 rows" in result.output
