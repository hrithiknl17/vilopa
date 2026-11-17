"""Count what a plan would touch, without changing anything.

Walks the plan's resolve phase parent first, the way the executor will: each table's
target rows are found from the identifiers and from the key values already read from its
parents' targets. Only the key columns children need are read, as text; nothing else
leaves the database. Works across connectors, because parent keys travel as values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from vilopa.core.datamap import Normalizer
from vilopa.core.plan import Edge, Estimate, Plan, StepKind, normalize


@dataclass(frozen=True)
class KeyCondition:
    """The column, normalised, is one of `values`."""

    column: str
    normalize: Normalizer
    values: tuple[str, ...]


@dataclass(frozen=True)
class RefCondition:
    """The columns, as text, form one of the `values` tuples."""

    columns: tuple[str, ...]
    values: tuple[tuple[str, ...], ...]


Condition = KeyCondition | RefCondition
Row = tuple[str | None, ...]
Keys = tuple[str, ...]


class Source(Protocol):
    """Read access to one connector."""

    def select(
        self, table_ref: str, columns: Sequence[str], conditions: Sequence[Condition]
    ) -> list[Row]:
        """Rows matching any condition, each selected column as text (None for NULL)."""

    def total(self, table_ref: str) -> int:
        """Rows in the table. May be the database's estimate on large tables."""


def estimate(
    plan: Plan,
    principal: Mapping[str, Sequence[str]],
    sources: Mapping[str, Source],
) -> Estimate:
    """Count targets per table and rows per detach step.

    `principal` maps key name to normalised values, as `plan.parse_principal` returns.
    """
    needed: dict[str, set[Keys]] = {}
    for r in plan.resolve:
        for edge in r.via:
            needed.setdefault(edge.parent, set()).add(edge.ref_columns)
    for step in plan.steps:
        if step.edge is not None:
            needed.setdefault(step.edge.parent, set()).add(step.edge.ref_columns)
    root = plan.resolve[0].table if plan.resolve else None
    root_keys = {k.key: k.column for k in plan.resolve[0].keys} if plan.resolve else {}
    match_keys = sorted({k.key for r in plan.resolve[1:] for k in r.keys})
    if root is not None:
        for key in match_keys:
            needed.setdefault(root, set()).add((root_keys[key],))

    found: dict[str, dict[Keys, set[Keys]]] = {}
    from_root: dict[str, set[str]] = {}
    targets: dict[str, int] = {}

    for r in plan.resolve:
        conditions: list[Condition] = []
        for km in r.keys:
            values = set(principal.get(km.key, ()))
            if r.table != root:
                values |= from_root.get(km.key, set())
            if values:
                conditions.append(KeyCondition(km.column, km.normalize, tuple(sorted(values))))
        for edge in r.via:
            refs = found.get(edge.parent, {}).get(edge.ref_columns, set())
            if refs:
                conditions.append(RefCondition(edge.columns, tuple(sorted(refs))))

        wanted = sorted(needed.get(r.table, set()))
        columns = list(dict.fromkeys(c for group in wanted for c in group))
        rows = _source(sources, r.table).select(r.table, columns, conditions) if conditions else []
        targets[r.table] = len(rows)

        position = {c: i for i, c in enumerate(columns)}
        table_found = found.setdefault(r.table, {})
        for group in wanted:
            seen = table_found.setdefault(group, set())
            for row in rows:
                value = tuple(row[position[c]] for c in group)
                if all(v is not None for v in value):
                    seen.add(tuple(str(v) for v in value))
        if r.table == root:
            for key in match_keys:
                how = next(k.normalize for k in r.keys if k.key == key)
                values_read = table_found.get((root_keys[key],), set())
                from_root[key] = {normalize(v[0], how) for v in values_read}

    detach: dict[int, int] = {}
    for i, step in enumerate(plan.steps):
        if step.kind is StepKind.DETACH and step.edge is not None:
            detach[i] = _count_referencing(step.edge, found, sources)

    tables = {r.table for r in plan.resolve}
    totals = {table: _source(sources, table).total(table) for table in sorted(tables)}
    return Estimate(targets, totals, detach)


def _count_referencing(
    edge: Edge, found: Mapping[str, Mapping[Keys, set[Keys]]], sources: Mapping[str, Source]
) -> int:
    refs = found.get(edge.parent, {}).get(edge.ref_columns, set())
    if not refs:
        return 0
    condition = RefCondition(edge.columns, tuple(sorted(refs)))
    return len(_source(sources, edge.child).select(edge.child, [], [condition]))


def _source(sources: Mapping[str, Source], table_ref: str) -> Source:
    return sources[table_ref.split(".", 1)[0]]
