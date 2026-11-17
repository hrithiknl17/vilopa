"""Property tests: on random schemas and data, a plan the planner accepts never breaks a
foreign key or a NOT NULL constraint at any step, and removes every target row it deletes.

Each example builds a random set of tables with random foreign keys (self references and
cycles included), a random data map over them, and random rows that satisfy every
constraint. The plan is then executed against an in-memory model that checks the
constraints after every step, the way Postgres would with immediate constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vilopa.core import datamap
from vilopa.core.catalog import ForeignKey, Schema
from vilopa.core.datamap import ColumnStrategy
from vilopa.core.plan import Plan, Step, StepKind, build

Rows = dict[str, dict[int, dict[str, Any]]]
"""table -> id -> row."""


@dataclass(frozen=True)
class Case:
    tables: list[str]
    fks: list[tuple[str, str, str, bool]]
    """(child, column, parent, nullable)."""
    erase: dict[str, str]
    nullify: dict[str, set[str]]
    """update_columns table -> FK columns it nullifies."""
    mapped: set[str]
    rows: Rows

    def ref(self, table: str) -> str:
        return f"db.public.{table}"


@st.composite
def cases(draw: st.DrawFn) -> Case:
    n = draw(st.integers(1, 5))
    tables = [f"t{i}" for i in range(n)]
    fks: list[tuple[str, str, str, bool]] = []
    for j in range(draw(st.integers(0, 7))):
        child = draw(st.sampled_from(tables))
        parent = draw(st.sampled_from(tables))
        nullable = draw(st.sampled_from([True, True, True, False]))  # accepted plans need it
        fks.append((child, f"f{j}", parent, nullable))

    mapped = {"t0"} | {t for t in tables[1:] if draw(st.booleans())}
    erase = {"t0": draw(st.sampled_from(["delete_row", "update_columns"]))}
    for t in sorted(mapped - {"t0"}):
        erase[t] = draw(st.sampled_from(["delete_row", "update_columns", "retain"]))
    nullify = {
        t: {col for child, col, _, _ in fks if child == t and draw(st.booleans())}
        for t in sorted(mapped)
        if erase[t] == "update_columns"
    }

    ids = {t: list(range(1, draw(st.integers(1, 5)) + 1)) for t in tables}
    rows: Rows = {t: {} for t in tables}
    for t in tables:
        for i in ids[t]:
            row: dict[str, Any] = {"id": i, "k": draw(st.sampled_from(["a", "b"])), "x": "pii"}
            for child, col, parent, nullable in fks:
                if child == t:
                    options: list[int | None] = list(ids[parent])
                    if nullable:
                        options.append(None)
                    row[col] = draw(st.sampled_from(options))
            rows[t][i] = row
    return Case(tables, fks, erase, nullify, mapped, rows)


def to_map(case: Case) -> datamap.DataMap:
    tables: dict[str, Any] = {}
    for t in sorted(case.mapped):
        erase = case.erase[t]
        columns: dict[str, Any] = {}
        entry: dict[str, Any] = {"erase": erase, "columns": columns}
        if t == "t0":
            columns["k"] = {"pii": "email"}
            if erase == "update_columns":
                columns["k"]["strategy"] = "nullify"
        if erase == "update_columns":
            columns["x"] = {"pii": "free_text", "strategy": "nullify"}
            for col in sorted(case.nullify[t]):
                columns[col] = {"pii": "none", "strategy": "nullify"}
        if erase == "retain":
            entry["retain"] = {"reason": "kept", "period": "P1Y"}
        tables[case.ref(t)] = entry
    return datamap.from_obj(
        {
            "version": 0,
            "connectors": [{"name": "db", "kind": "postgres", "dsn_env": "DB"}],
            "principal": {"root": case.ref("t0"), "keys": {"k": {"column": "k"}}},
            "tables": tables,
        }
    )


def to_schema(case: Case) -> Schema:
    fks = [
        ForeignKey(f"{child}_{col}_fkey", "public", child, (col,), "public", parent, ("id",))
        for child, col, parent, _ in case.fks
    ]
    not_null = [("public", t, "id") for t in case.tables]
    not_null += [("public", child, col) for child, col, _, nullable in case.fks if not nullable]
    return Schema.build(fks, not_null)


def short(ref: str) -> str:
    return ref.rsplit(".", 1)[1]


def resolve_targets(plan: Plan, rows: Rows) -> dict[str, set[int]]:
    """The executor's snapshot: every target set, found before anything changes."""
    targets: dict[str, set[int]] = {}
    for r in plan.resolve:
        table = short(r.table)
        found: set[int] = set()
        for i, row in rows[table].items():
            if any(row.get(k.column) == "a" for k in r.keys):
                found.add(i)
            for edge in r.via:
                if row.get(edge.columns[0]) in targets[short(edge.parent)]:
                    found.add(i)
        targets[table] = found
    return targets


def apply(step: Step, rows: Rows, targets: dict[str, set[int]]) -> None:
    table = short(step.table)
    if step.kind is StepKind.DETACH:
        assert step.edge is not None
        column, parent = step.edge.columns[0], short(step.edge.parent)
        for row in rows[table].values():
            if row.get(column) in targets[parent]:
                row[column] = None
    elif step.kind is StepKind.DELETE:
        for i in targets[table]:
            rows[table].pop(i, None)
    elif step.kind is StepKind.UPDATE:
        for i in targets[table]:
            for column, strategy in step.columns:
                if strategy is ColumnStrategy.NULLIFY:
                    rows[table][i][column] = None


def violations(case: Case, rows: Rows) -> list[str]:
    out = []
    for child, col, parent, nullable in case.fks:
        for row in rows[child].values():
            value = row[col]
            if value is None and not nullable:
                out.append(f"{child}.{col} is NULL in row {row['id']}")
            if value is not None and value not in rows[parent]:
                out.append(f"{child}.{col} = {value} points at a deleted {parent} row")
    return out


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cases())
def test_accepted_plans_never_break_a_constraint(case: Case) -> None:
    plan = build(to_map(case), {"db": to_schema(case)})
    if not plan.ok:
        return
    rows: Rows = {t: {i: dict(r) for i, r in table.items()} for t, table in case.rows.items()}
    assert violations(case, rows) == []
    targets = resolve_targets(plan, rows)
    for step in plan.steps:
        apply(step, rows, targets)
        assert violations(case, rows) == [], f"after {step}"
    for step in plan.steps:
        if step.kind is StepKind.DELETE:
            table = short(step.table)
            assert not targets[table] & set(rows[table])


@settings(max_examples=300, deadline=None)
@given(cases())
def test_plan_order_respects_every_resolving_edge(case: Case) -> None:
    plan = build(to_map(case), {"db": to_schema(case)})
    position = {s.table: i for i, s in enumerate(plan.steps) if s.kind is not StepKind.DETACH}
    seen: set[str] = set()
    for r in plan.resolve:
        for edge in r.via:
            assert edge.parent in seen  # resolve runs parent first
            child, parent = position.get(edge.child), position.get(edge.parent)
            if child is None or parent is None or not plan.ok:
                continue
            if plan.steps[parent].kind is StepKind.DELETE and edge.enforced:
                assert child < parent  # mutate runs child first
        seen.add(r.table)
    kinds = [s.kind for s in plan.steps]
    detaches = kinds.count(StepKind.DETACH)
    assert kinds[:detaches] == [StepKind.DETACH] * detaches  # detaches come first


@settings(max_examples=200, deadline=None)
@given(cases())
def test_planning_is_deterministic(case: Case) -> None:
    dm, schema = to_map(case), to_schema(case)
    assert build(dm, {"db": schema}) == build(dm, {"db": schema})
