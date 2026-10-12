"""Read a live Postgres schema and sample column values for detection.

`connect` opens a read-only session with a statement timeout, so a scan can be pointed at a
production replica without risk of writes or runaway queries. Identifiers only ever reach
the database through `psycopg.sql.Identifier`. Sampled values go straight to detection and
are dropped; they are never returned.

Sampling reads the first rows the table returns (no ORDER BY), which is cheap on large
tables but not random. Columns that sampling misses are still classified by name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql

from mita.core import detect
from mita.core.catalog import ColumnInfo

Connection = psycopg.Connection[tuple[Any, ...]]

SAMPLED_TYPES = detect.TEXT_TYPES | {"bigint", "numeric"}
"""Types whose values can hold an identifier detection recognises (phones fit in bigint)."""
MAX_VALUE_CHARS = 2000
"""Longer values are truncated in the query, so one huge document cannot exhaust memory."""

_COLUMNS_SQL = r"""
SELECT c.table_schema, c.table_name, c.column_name,
       CASE WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name ELSE c.data_type END
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE t.table_type = 'BASE TABLE'
  AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
  AND c.table_schema NOT LIKE 'pg\_%%'
  AND (%(schemas)s::text[] IS NULL OR c.table_schema = ANY (%(schemas)s::text[]))
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""


def connect(dsn: str, *, statement_timeout_ms: int = 30_000) -> Connection:
    conn = psycopg.connect(
        dsn,
        application_name="mita",
        options=f"-c statement_timeout={int(statement_timeout_ms)}",
    )
    conn.read_only = True
    return conn


def columns(conn: Connection, schemas: Sequence[str] | None = None) -> list[ColumnInfo]:
    """Every column of every ordinary table, outside the system schemas."""
    rows = conn.execute(_COLUMNS_SQL, {"schemas": list(schemas) if schemas else None})
    return [ColumnInfo(str(s), str(t), str(c), str(d)) for s, t, c, d in rows.fetchall()]


def scan(
    conn: Connection, *, schemas: Sequence[str] | None = None, sample_rows: int = 200
) -> list[detect.ColumnReport]:
    """Classify every column. `sample_rows=0` classifies by name and type only."""
    by_table: dict[tuple[str, str], list[ColumnInfo]] = {}
    for column in columns(conn, schemas):
        by_table.setdefault((column.schema, column.table), []).append(column)

    reports: list[detect.ColumnReport] = []
    for (schema, table), table_columns in by_table.items():
        names = [c.name for c in table_columns if c.data_type in SAMPLED_TYPES]
        samples = _sample(conn, schema, table, names, sample_rows) if names and sample_rows else {}
        for column in table_columns:
            values = samples.get(column.name, [])
            reports.append(
                detect.ColumnReport(column, len(values), detect.classify(column, values))
            )
    return reports


def _sample(
    conn: Connection, schema: str, table: str, names: Sequence[str], limit: int
) -> dict[str, list[str]]:
    query = sql.SQL("SELECT {} FROM {} LIMIT {}").format(
        sql.SQL(", ").join(
            sql.SQL("left({}::text, {})").format(sql.Identifier(name), sql.Literal(MAX_VALUE_CHARS))
            for name in names
        ),
        sql.Identifier(schema, table),
        sql.Literal(limit),
    )
    out: dict[str, list[str]] = {name: [] for name in names}
    try:
        # A savepoint, so one unreadable table does not abort the whole scan.
        with conn.transaction():
            for row in conn.execute(query):
                for name, value in zip(names, row, strict=True):
                    if value is not None:
                        out[name].append(value)
    except psycopg.errors.InsufficientPrivilege:
        return {}
    return out
