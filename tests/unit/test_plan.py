from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from vilopa.core import datamap
from vilopa.core.catalog import ForeignKey, Schema
from vilopa.core.datamap import Normalizer
from vilopa.core.plan import (
    Estimate,
    Limits,
    PrincipalError,
    build,
    check_blast,
    normalize,
    parse_principal,
)

GOLDEN = Path(__file__).with_name("golden")


@pytest.fixture(scope="module")
def demo_map(demo_datamap_path: Path) -> datamap.DataMap:
    return datamap.load(demo_datamap_path)


def summary(steps: Any) -> list[tuple[str, str]]:
    return [(str(s.kind), s.table.removeprefix("app_pg.public.")) for s in steps]


def test_demo_plan_matches_golden(demo_map: datamap.DataMap, demo_schema: Schema) -> None:
    """Any change to planner output shows up here as a reviewed diff.

    Regenerate with VILOPA_UPDATE_GOLDEN=1 after checking the new plan is right.
    """
    plan = build(demo_map, {"app_pg": demo_schema})
    got = json.dumps(plan.to_json(), indent=2, sort_keys=True) + "\n"
    path = GOLDEN / "bharat-bazaar.plan.json"
    if os.environ.get("VILOPA_UPDATE_GOLDEN"):
        path.write_text(got, encoding="utf-8")
    assert got == path.read_text(encoding="utf-8")


def test_demo_plan_order(demo_map: datamap.DataMap, demo_schema: Schema) -> None:
    plan = build(demo_map, {"app_pg": demo_schema})
    assert plan.ok, plan.problems
    assert summary(plan.steps) == [
        ("detach", "customers"),  # people the principal referred keep their account
        ("detach", "orders"),  # orders of others pointing at the principal's addresses
        ("delete", "addresses"),
        ("delete", "events"),
        ("update", "orders"),
        ("update", "payments"),
        ("delete", "reviews"),
        ("delete", "support_tickets"),
        ("delete", "wishlists"),
        ("delete", "customers"),
        ("retain", "invoices"),
    ]
    detach = plan.steps[0].edge
    assert detach is not None
    assert detach.columns == ("referred_by_customer_id",)


def test_demo_resolve_reaches_every_table_parent_first(
    demo_map: datamap.DataMap, demo_schema: Schema
) -> None:
    plan = build(demo_map, {"app_pg": demo_schema})
    seen: set[str] = set()
    for r in plan.resolve:
        assert all(e.parent in seen for e in r.via), r.table
        seen.add(r.table)
    assert seen == set(demo_map.tables)
    depth = {r.table.removeprefix("app_pg.public."): r.depth for r in plan.resolve}
    assert depth["customers"] == 0
    assert depth["events"] == 1  # through the declared relation, which has no FK
    assert depth["invoices"] == 2  # through orders
    tickets = next(r for r in plan.resolve if r.table.endswith("support_tickets"))
    assert [k.key for k in tickets.keys] == ["email"]  # guest tickets
    assert [e.name for e in tickets.via] == ["support_tickets_customer_id_fkey"]


def make_map(tables: dict[str, Any], **top: Any) -> datamap.DataMap:
    return datamap.from_obj(
        {
            "version": 0,
            "connectors": [{"name": "db", "kind": "postgres", "dsn_env": "DB_DSN"}],
            "principal": {"root": "db.public.users", "keys": {"email": {"column": "email"}}},
            "tables": {
                "db.public.users": {
                    "erase": "delete_row",
                    "columns": {"email": {"pii": "email"}},
                },
                **tables,
            },
            **top,
        }
    )


def fk(table: str, column: str, ref: str, ref_column: str = "id") -> ForeignKey:
    return ForeignKey(
        f"{table}_{column}_fkey", "public", table, (column,), "public", ref, (ref_column,)
    )


def test_kept_row_pointing_at_deleted_parent_is_a_problem() -> None:
    dm = make_map(
        {
            "db.public.orders": {
                "erase": "retain",
                "retain": {"reason": "tax", "period": "P8Y"},
            }
        }
    )
    plan = build(dm, {"db": Schema.build([fk("orders", "user_id", "users")])})
    assert [p.path for p in plan.problems] == ["db.public.orders"]
    assert "keeps rows that point at db.public.users" in plan.problems[0].message


def test_unmapped_table_pointing_at_deleted_table_is_a_problem() -> None:
    plan = build(make_map({}), {"db": Schema.build([fk("sessions", "user_id", "users")])})
    assert [p.path for p in plan.problems] == ["db.public.sessions"]
    assert "not in the data map" in plan.problems[0].message


def test_unmapped_table_pointing_at_kept_table_is_fine() -> None:
    dm = make_map(
        {
            "db.public.orders": {
                "erase": "update_columns",
                "columns": {"user_id": {"pii": "none", "strategy": "nullify"}},
            }
        }
    )
    schema = Schema.build(
        [fk("orders", "user_id", "users"), fk("order_items", "order_id", "orders")]
    )
    assert build(dm, {"db": schema}).ok


def test_unreachable_table_is_a_problem() -> None:
    plan = build(make_map({"db.public.logs": {"erase": "delete_row"}}), {"db": Schema.build()})
    assert [p.path for p in plan.problems] == ["db.public.logs"]
    assert "cannot be reached" in plan.problems[0].message


def test_match_column_reaches_a_table_without_links() -> None:
    dm = make_map(
        {
            "db.public.leads": {
                "erase": "delete_row",
                "match": [{"key": "email", "column": "contact"}],
                "columns": {"contact": {"pii": "email"}},
            }
        }
    )
    plan = build(dm, {"db": Schema.build()})
    assert plan.ok
    assert summary(plan.steps) == [("delete", "db.public.leads"), ("delete", "db.public.users")]


def test_self_reference_not_null_cannot_be_detached() -> None:
    schema = Schema.build(
        [fk("users", "invited_by", "users")], not_null=[("public", "users", "invited_by")]
    )
    plan = build(make_map({}), {"db": schema})
    assert len(plan.problems) == 1
    assert "is NOT NULL" in plan.problems[0].message


def test_two_table_cycle_is_broken_with_a_detach() -> None:
    # users.team_id -> teams and teams.owner_id -> users: teams resolve through owner_id,
    # so users.team_id is the side that gets detached.
    dm = make_map({"db.public.teams": {"erase": "delete_row"}})
    schema = Schema.build([fk("users", "team_id", "teams"), fk("teams", "owner_id", "users")])
    plan = build(dm, {"db": schema})
    assert plan.ok, plan.problems
    assert summary(plan.steps) == [
        ("detach", "db.public.users"),
        ("delete", "db.public.teams"),
        ("delete", "db.public.users"),
    ]


def test_nullify_on_not_null_column_is_a_problem() -> None:
    dm = make_map(
        {
            "db.public.orders": {
                "erase": "update_columns",
                "columns": {"user_id": {"pii": "none", "strategy": "nullify"}},
            }
        }
    )
    schema = Schema.build(
        [fk("orders", "user_id", "users")], not_null=[("public", "orders", "user_id")]
    )
    messages = [p.message for p in build(dm, {"db": schema}).problems]
    assert "strategy: nullify, but the column is NOT NULL in the database" in messages


def test_changing_a_referenced_key_is_a_problem() -> None:
    dm = make_map(
        {
            "db.public.users": {
                "erase": "update_columns",
                "columns": {"email": {"pii": "email", "strategy": "pseudonymize"}},
            }
        }
    )
    schema = Schema.build([fk("audit", "user_email", "users", "email")])
    plan = build(dm, {"db": schema})
    assert any("changes a key" in p.message for p in plan.problems)


def test_declared_relation_orders_nothing_but_resolves() -> None:
    dm = make_map(
        {"db.public.events": {"erase": "delete_row"}},
        relations=[{"from": "db.public.events.user_id", "to": "db.public.users.id"}],
    )
    plan = build(dm, {"db": Schema.build()})
    assert plan.ok
    events = next(r for r in plan.resolve if r.table == "db.public.events")
    assert [(e.name, e.enforced) for e in events.via] == [("relation", False)]


def test_missing_connector_schema_is_a_problem() -> None:
    assert not build(make_map({}), {}).ok


# --- principal identifiers ---


def test_normalize_matches_the_documented_forms() -> None:
    assert normalize("  Priya@Example.TEST ", Normalizer.EMAIL) == "priya@example.test"
    for raw in ("+91 98765 43210", "09876543210", "919876543210", "9876543210"):
        assert normalize(raw, Normalizer.IN_MOBILE) == "9876543210"
    assert normalize(" X ", Normalizer.EXACT) == " X "


def test_parse_principal(demo_map: datamap.DataMap) -> None:
    got = parse_principal(demo_map, ["email:A@B.test", "phone:+91 98765 43210", "email:a@b.test"])
    assert got == {"email": ["a@b.test"], "phone": ["9876543210"]}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("nokey", "expected key:value"),
        ("email:", "expected key:value"),
        ("pan:ABCDE1234F", "unknown key 'pan'"),
        ("phone:12345", "not an Indian mobile number"),
        ("email:nobody", "not an email address"),
    ],
)
def test_parse_principal_rejects(demo_map: datamap.DataMap, spec: str, message: str) -> None:
    with pytest.raises(PrincipalError, match=message):
        parse_principal(demo_map, [spec])


# --- blast radius ---


def test_blast_radius(demo_map: datamap.DataMap, demo_schema: Schema) -> None:
    plan = build(demo_map, {"app_pg": demo_schema})
    root = plan.resolve[0].table
    events = "app_pg.public.events"
    totals = {r.table: 100_000 for r in plan.resolve}

    assert check_blast(plan, Estimate({root: 1, events: 40}, totals)) == []

    too_many = Estimate({root: 1, events: 48_000}, totals)
    (problem,) = check_blast(plan, too_many)
    assert problem.path.endswith(events)
    assert "48000 rows, above the limit 10000" in problem.message
    assert check_blast(plan, too_many, Limits(max_rows=50_000, max_fraction=0.5)) == []

    share = Estimate({root: 1, events: 600}, {**totals, events: 1000})
    (problem,) = check_blast(plan, share)
    assert "600 of 1000 rows (60%)" in problem.message

    small = Estimate({root: 1, events: 90}, {**totals, events: 100})
    assert check_blast(plan, small) == []  # under the floor, so the share is not checked

    (problem,) = check_blast(plan, Estimate({root: 2}, totals))
    assert "match 2 rows" in problem.message
