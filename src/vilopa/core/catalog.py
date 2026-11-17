"""What a connector's live schema looks like, independent of how it was read."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnInfo:
    """One column of one table, as the database reports it."""

    schema: str
    table: str
    name: str
    data_type: str

    def table_ref(self, connector: str) -> str:
        return f"{connector}.{self.schema}.{self.table}"

    def ref(self, connector: str) -> str:
        return f"{connector}.{self.schema}.{self.table}.{self.name}"


@dataclass(frozen=True)
class ForeignKey:
    """A foreign-key constraint: `columns` of (schema, table) reference `ref_columns`.

    Columns are paired by position, so composite keys keep their order. `on_delete` is the
    Postgres action name (`NO ACTION`, `RESTRICT`, `CASCADE`, `SET NULL`, `SET DEFAULT`).
    The planner never relies on it: it is recorded so a plan can say what it overrides.
    """

    name: str
    schema: str
    table: str
    columns: tuple[str, ...]
    ref_schema: str
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: str = "NO ACTION"


@dataclass(frozen=True)
class Schema:
    """The structural facts the planner needs from one connector."""

    foreign_keys: tuple[ForeignKey, ...] = ()
    not_null: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)
    """(schema, table, column) triples declared NOT NULL."""
    primary_keys: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    """(schema, table) -> primary key columns, in key order."""

    @classmethod
    def build(
        cls,
        foreign_keys: Iterable[ForeignKey] = (),
        not_null: Iterable[tuple[str, str, str]] = (),
        primary_keys: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> Schema:
        return cls(
            tuple(sorted(foreign_keys, key=lambda fk: (fk.schema, fk.table, fk.name))),
            frozenset(not_null),
            dict(primary_keys or {}),
        )

    def is_nullable(self, schema: str, table: str, column: str) -> bool:
        return (schema, table, column) not in self.not_null
