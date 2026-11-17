"""Build an erasure plan: which rows belong to the principal, and in what order to change them.

A plan has two phases, and the split is what makes it safe:

1. **Resolve**, parent first. Starting at the principal's root row, every mapped table finds
   its target rows through foreign keys, declared relations and `match` columns. Every
   target set is snapshotted before anything changes, so nullifying `orders.customer_id`
   cannot hide the invoices that are found through those orders.
2. **Mutate**, child first. Detach steps run first: they null out references to rows that
   will be deleted, from rows that are not resolved through that reference (a customer the
   principal referred, through a self-referencing foreign key). Then each table is deleted
   or updated, children before their parents. Retained tables only get a record step.

A table resolves only through parents strictly nearer the root (breadth-first depth), so the
resolve graph is acyclic by construction, and so is the mutate order derived from it. Every
other enforced foreign key into a table whose rows are deleted becomes a detach step, or a
problem when its columns are NOT NULL. The planner never relies on `ON DELETE CASCADE` or
`SET NULL` the map did not declare: a cascade deletes rows nobody reviewed.

Planning is pure: it reads the data map and schema facts and touches no database.
Row counts come from `vilopa.engine.estimate`; `check_blast` judges them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vilopa.core.catalog import Schema
from vilopa.core.datamap import (
    ColumnStrategy,
    DataMap,
    Normalizer,
    Problem,
    Table,
    TableErase,
)


@dataclass(frozen=True, order=True)
class Edge:
    """`child.columns` points at `parent.ref_columns`. Table names are full refs."""

    child: str
    columns: tuple[str, ...]
    parent: str
    ref_columns: tuple[str, ...]
    name: str
    """Constraint name, or `relation` for a link declared in the data map."""
    enforced: bool
    """True for a database foreign key; False for a declared relation."""
    on_delete: str | None = None

    def describe(self) -> str:
        cols = ", ".join(self.columns)
        refs = ", ".join(self.ref_columns)
        kind = f"foreign key {self.name}" if self.enforced else "declared relation"
        return f"{self.child}({cols}) -> {self.parent}({refs}) [{kind}]"

    def to_json(self) -> dict[str, Any]:
        return {
            "child": self.child,
            "columns": list(self.columns),
            "parent": self.parent,
            "ref_columns": list(self.ref_columns),
            "name": self.name,
            "enforced": self.enforced,
        }


@dataclass(frozen=True)
class KeyMatch:
    """Rows whose `column`, normalised, equals a principal identifier `key`."""

    key: str
    column: str
    normalize: Normalizer

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "column": self.column, "normalize": str(self.normalize)}


@dataclass(frozen=True)
class Resolve:
    """How one table's target rows are found. A row is a target if any condition holds.

    Key matches compare against the identifiers given on the command line and, for tables
    other than the root, against the same identifiers read from the root's target rows.
    """

    table: str
    depth: int
    keys: tuple[KeyMatch, ...] = ()
    via: tuple[Edge, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "depth": self.depth,
            "keys": [k.to_json() for k in self.keys],
            "via": [e.to_json() for e in self.via],
        }


class StepKind(StrEnum):
    DETACH = "detach"
    DELETE = "delete"
    UPDATE = "update"
    RETAIN = "retain"


@dataclass(frozen=True)
class Step:
    kind: StepKind
    table: str
    columns: tuple[tuple[str, ColumnStrategy], ...] = ()
    """For `update`: each column with a strategy, retained columns included."""
    edge: Edge | None = None
    """For `detach`: the reference being nulled out."""
    reason: str | None = None
    """For `retain`: why the rows are kept."""

    def describe(self) -> str:
        if self.kind is StepKind.DETACH:
            edge = self.edge
            if edge is None:
                raise ValueError("a detach step needs an edge")
            cols = ", ".join(edge.columns)
            return f"set {cols} = NULL where it points at a target row of {edge.parent}"
        if self.kind is StepKind.DELETE:
            return "delete target rows"
        if self.kind is StepKind.UPDATE:
            return ", ".join(f"{c}: {s}" for c, s in self.columns)
        return f"keep: {self.reason}"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": str(self.kind), "table": self.table}
        if self.columns:
            out["columns"] = {c: str(s) for c, s in self.columns}
        if self.edge is not None:
            out["edge"] = self.edge.to_json()
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class Plan:
    resolve: tuple[Resolve, ...]
    """Parent first: every table's `via` parents appear before it. The root is first."""
    steps: tuple[Step, ...]
    """In execution order."""
    problems: tuple[Problem, ...] = ()
    """Anything here blocks execution."""

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "resolve": [r.to_json() for r in self.resolve],
            "steps": [s.to_json() for s in self.steps],
            "problems": [{"path": p.path, "message": p.message} for p in self.problems],
        }


def edges(dm: DataMap, schemas: Mapping[str, Schema]) -> list[Edge]:
    """Every foreign key of every connector, plus the data map's declared relations."""
    out: list[Edge] = []
    for connector, schema in sorted(schemas.items()):
        for fk in schema.foreign_keys:
            out.append(
                Edge(
                    child=f"{connector}.{fk.schema}.{fk.table}",
                    columns=fk.columns,
                    parent=f"{connector}.{fk.ref_schema}.{fk.ref_table}",
                    ref_columns=fk.ref_columns,
                    name=fk.name,
                    enforced=True,
                    on_delete=fk.on_delete,
                )
            )
    for relation in dm.relations:
        child, column = relation.from_.rsplit(".", 1)
        parent, ref_column = relation.to.rsplit(".", 1)
        out.append(Edge(child, (column,), parent, (ref_column,), "relation", enforced=False))
    return out


def build(dm: DataMap, schemas: Mapping[str, Schema]) -> Plan:
    """Plan an erasure for the data map `dm` given each connector's schema facts."""
    problems: list[Problem] = []
    all_edges = edges(dm, schemas)
    tables = dm.tables
    root = dm.principal.root

    for ref in tables:
        connector = ref.split(".", 1)[0]
        if connector not in schemas:
            problems.append(Problem(ref, f"no schema was read for connector {connector!r}"))

    depth = _depths(dm, all_edges)
    resolve = _resolve(dm, all_edges, depth)
    via = {r.table: set(r.via) for r in resolve}

    for ref in tables:
        if ref not in depth:
            problems.append(
                Problem(
                    ref,
                    "cannot be reached from the principal: no foreign key, relation or match "
                    f"links it to {root}",
                )
            )

    deleted = {ref for ref, t in tables.items() if t.erase is TableErase.DELETE_ROW}
    before: dict[str, set[str]] = {ref: set() for ref in depth}
    """before[x]: tables whose mutate step must run before x's."""
    detaches: list[Step] = []

    for edge in all_edges:
        if not edge.enforced or edge.parent not in deleted or edge.parent not in depth:
            continue
        child_table = tables.get(edge.child)
        if child_table is None:
            problems.append(
                Problem(
                    edge.child,
                    f"not in the data map, but {edge.describe()} points at a table whose rows "
                    "erasure deletes. Map this table so the planner can handle it",
                )
            )
            continue
        if edge.child not in depth:
            continue  # already reported as unreachable
        if edge in via[edge.child]:
            if child_table.erase is TableErase.DELETE_ROW or _nullifies(child_table, edge.columns):
                before[edge.parent].add(edge.child)
            else:
                cols = ", ".join(edge.columns)
                problems.append(
                    Problem(
                        edge.child,
                        f"keeps rows that point at {edge.parent} through ({cols}), but erasure "
                        f"deletes those rows. Set strategy: nullify on {cols}, or delete the row",
                    )
                )
            continue
        not_null = [c for c in edge.columns if not _nullable(schemas, edge.child, c)]
        if not_null:
            problems.append(
                Problem(
                    edge.child,
                    f"({', '.join(not_null)}) is NOT NULL, so rows not resolved through "
                    f"{edge.describe()} cannot be detached before their parent is deleted. "
                    "Make the column nullable, or change the map so this table resolves "
                    "through that link",
                )
            )
            continue
        detaches.append(Step(StepKind.DETACH, edge.child, edge=edge))

    problems.extend(_column_problems(dm, all_edges, schemas))

    mutate_order = _topological(
        [ref for ref in depth if tables[ref].erase is not TableErase.RETAIN], before
    )
    steps: list[Step] = sorted(detaches, key=lambda s: (s.table, s.edge.name if s.edge else ""))
    for ref in mutate_order:
        table = tables[ref]
        if table.erase is TableErase.DELETE_ROW:
            steps.append(Step(StepKind.DELETE, ref))
        else:
            changes = tuple(
                (name, column.strategy)
                for name, column in table.columns.items()
                if column.strategy is not None
            )
            steps.append(Step(StepKind.UPDATE, ref, columns=changes))
    for ref in sorted(depth):
        retained = tables[ref]
        if retained.erase is TableErase.RETAIN:
            reason = retained.retain.reason if retained.retain else "retained"
            steps.append(Step(StepKind.RETAIN, ref, reason=reason))

    return Plan(tuple(resolve), tuple(steps), tuple(problems))


def _depths(dm: DataMap, all_edges: Sequence[Edge]) -> dict[str, int]:
    """Breadth-first distance from the root over child -> parent links between mapped tables.

    A table with `match` columns is found from the root's identifiers, so it is at most 1.
    """
    tables = dm.tables
    root = dm.principal.root
    children: dict[str, set[str]] = {}
    for edge in all_edges:
        if edge.child in tables and edge.parent in tables and edge.child != edge.parent:
            children.setdefault(edge.parent, set()).add(edge.child)

    depth = {root: 0}
    frontier = [root]
    level = 0
    while frontier:
        found = {child for parent in frontier for child in children.get(parent, ())}
        if level == 0:
            found |= {ref for ref, t in tables.items() if t.match}
        frontier = sorted(ref for ref in found if ref not in depth)
        level += 1
        for ref in frontier:
            depth[ref] = level
    return depth


def _resolve(dm: DataMap, all_edges: Sequence[Edge], depth: Mapping[str, int]) -> list[Resolve]:
    keys = dm.principal.keys
    out: list[Resolve] = []
    for ref in sorted(depth, key=lambda r: (depth[r], r)):
        if ref == dm.principal.root:
            matches = tuple(
                KeyMatch(name, spec.column, spec.normalize) for name, spec in keys.items()
            )
            out.append(Resolve(ref, 0, keys=matches))
            continue
        table = dm.tables[ref]
        matches = tuple(KeyMatch(m.key, m.column, keys[m.key].normalize) for m in table.match)
        via = tuple(
            sorted(
                {
                    e
                    for e in all_edges
                    if e.child == ref and e.parent in depth and depth[e.parent] < depth[ref]
                }
            )
        )
        out.append(Resolve(ref, depth[ref], keys=matches, via=via))
    return out


def _nullifies(table: Table, columns: Iterable[str]) -> bool:
    if table.erase is not TableErase.UPDATE_COLUMNS:
        return False
    for name in columns:
        column = table.columns.get(name)
        if column is None or column.strategy is not ColumnStrategy.NULLIFY:
            return False
    return True


def _nullable(schemas: Mapping[str, Schema], table_ref: str, column: str) -> bool:
    connector, schema, table = table_ref.split(".")
    facts = schemas.get(connector)
    return facts is None or facts.is_nullable(schema, table, column)


def _column_problems(
    dm: DataMap, all_edges: Sequence[Edge], schemas: Mapping[str, Schema]
) -> list[Problem]:
    """Updates the database would reject, or that would break other rows' references."""
    problems: list[Problem] = []
    referenced: dict[tuple[str, str], list[Edge]] = {}
    for edge in all_edges:
        if edge.enforced:
            for ref_column in edge.ref_columns:
                referenced.setdefault((edge.parent, ref_column), []).append(edge)

    for ref, table in dm.tables.items():
        if table.erase is not TableErase.UPDATE_COLUMNS:
            continue
        for name, column in table.columns.items():
            if column.strategy is ColumnStrategy.NULLIFY and not _nullable(schemas, ref, name):
                problems.append(
                    Problem(
                        f"{ref}.{name}",
                        "strategy: nullify, but the column is NOT NULL in the database",
                    )
                )
            if column.strategy in (ColumnStrategy.NULLIFY, ColumnStrategy.PSEUDONYMIZE):
                for edge in referenced.get((ref, name), ()):
                    problems.append(
                        Problem(
                            f"{ref}.{name}",
                            f"strategy: {column.strategy} changes a key that {edge.describe()} "
                            "depends on",
                        )
                    )
    return problems


def _topological(nodes: Iterable[str], before: Mapping[str, set[str]]) -> list[str]:
    """Kahn's algorithm, alphabetical among ready nodes so the order is stable."""
    pending = set(nodes)
    incoming = {n: {m for m in before.get(n, set()) if m in pending} for n in pending}
    ready = sorted(n for n, deps in incoming.items() if not deps)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for other in sorted(pending):
            if node in incoming[other]:
                incoming[other].discard(node)
                if not incoming[other]:
                    ready.append(other)
        ready.sort()
    if len(order) != len(pending):
        # Unreachable by construction: ordering edges follow strictly decreasing depth.
        raise AssertionError(f"cycle in mutate order: {sorted(pending - set(order))}")
    return order


# --- principal identifiers -------------------------------------------------------------------

_NON_DIGIT = re.compile(r"\D")


class PrincipalError(ValueError):
    pass


def normalize(value: str, how: Normalizer) -> str:
    """Normalise an identifier exactly as the Postgres connector does in SQL."""
    if how is Normalizer.EMAIL:
        return value.strip(" ").lower()
    if how is Normalizer.IN_MOBILE:
        return _NON_DIGIT.sub("", value)[-10:]
    return value


def parse_principal(dm: DataMap, specs: Sequence[str]) -> dict[str, list[str]]:
    """Turn `key:value` arguments into normalised values per principal key."""
    if not specs:
        raise PrincipalError("give at least one --principal key:value")
    out: dict[str, list[str]] = {}
    for spec in specs:
        key, sep, value = spec.partition(":")
        if not sep or not value.strip():
            raise PrincipalError(f"{spec!r}: expected key:value, e.g. email:a@example.com")
        if key not in dm.principal.keys:
            known = ", ".join(dm.principal.keys)
            raise PrincipalError(f"{spec!r}: unknown key {key!r} (the map declares {known})")
        how = dm.principal.keys[key].normalize
        normal = normalize(value, how)
        if how is Normalizer.IN_MOBILE and not re.fullmatch(r"[6-9]\d{9}", normal):
            raise PrincipalError(f"{spec!r}: not an Indian mobile number")
        if how is Normalizer.EMAIL and "@" not in normal:
            raise PrincipalError(f"{spec!r}: not an email address")
        values = out.setdefault(key, [])
        if normal not in values:
            values.append(normal)
    return out


# --- row estimates and the blast-radius guard ------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_rows: int = 10_000
    """No single step may touch more rows than this."""
    max_fraction: float = 0.05
    """No step may touch more than this share of its table..."""
    fraction_floor: int = 100
    """...once it touches more than this many rows. Small counts are not held to the share."""


@dataclass(frozen=True)
class Estimate:
    targets: dict[str, int]
    """Target rows per table."""
    totals: dict[str, int]
    """Rows in each table."""
    detach: dict[int, int] = field(default_factory=dict)
    """Step index -> rows a detach step would change."""

    def rows(self, plan: Plan, index: int) -> int:
        step = plan.steps[index]
        if step.kind is StepKind.DETACH:
            return self.detach.get(index, 0)
        return self.targets.get(step.table, 0)


def check_blast(plan: Plan, estimate: Estimate, limits: Limits | None = None) -> list[Problem]:
    """Problems that stop a plan from running because it would touch too much."""
    limits = limits or Limits()
    problems: list[Problem] = []
    if plan.resolve:
        root = plan.resolve[0].table
        root_rows = estimate.targets.get(root, 0)
        if root_rows > 1:
            problems.append(
                Problem(
                    root,
                    f"the identifiers match {root_rows} rows; one erasure is one person. "
                    "Check the identifiers, or erase each person separately",
                )
            )
    for index, step in enumerate(plan.steps):
        if step.kind is StepKind.RETAIN:
            continue
        rows = estimate.rows(plan, index)
        total = estimate.totals.get(step.table, 0)
        where = f"steps[{index}] {step.table}"
        if rows > limits.max_rows:
            problems.append(
                Problem(
                    where, f"{step.kind} touches {rows} rows, above the limit {limits.max_rows}"
                )
            )
        elif rows > limits.fraction_floor and total and rows / total > limits.max_fraction:
            problems.append(
                Problem(
                    where,
                    f"{step.kind} touches {rows} of {total} rows ({rows / total:.0%}), above the "
                    f"limit of {limits.max_fraction:.0%}. A data map link may be wrong",
                )
            )
    return problems
