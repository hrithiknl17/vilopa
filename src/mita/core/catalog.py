"""What a connector's live schema looks like, independent of how it was read."""

from __future__ import annotations

from dataclasses import dataclass


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
