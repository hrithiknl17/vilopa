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

from vilopa.core import detect
from vilopa.core.catalog import ColumnInfo, ForeignKey, Schema
from vilopa.core.datamap import Normalizer
from vilopa.engine.estimate import Condition, KeyCondition

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
        application_name="vilopa",
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


_FOREIGN_KEYS_SQL = """
SELECT c.conname, ns.nspname, cl.relname,
       ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(num, pos)
             JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num
             ORDER BY k.pos)::text[],
       rns.nspname, rcl.relname,
       ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY AS k(num, pos)
             JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.num
             ORDER BY k.pos)::text[],
       c.confdeltype
FROM pg_constraint c
JOIN pg_class cl ON cl.oid = c.conrelid
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
JOIN pg_class rcl ON rcl.oid = c.confrelid
JOIN pg_namespace rns ON rns.oid = rcl.relnamespace
WHERE c.contype = 'f'
  AND c.conparentid = 0
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY ns.nspname, cl.relname, c.conname
"""

_NOT_NULL_SQL = """
SELECT ns.nspname, cl.relname, a.attname
FROM pg_attribute a
JOIN pg_class cl ON cl.oid = a.attrelid
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
WHERE a.attnotnull AND a.attnum > 0 AND NOT a.attisdropped
  AND cl.relkind IN ('r', 'p')
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
"""

_PRIMARY_KEYS_SQL = """
SELECT ns.nspname, cl.relname,
       ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(num, pos)
             JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num
             ORDER BY k.pos)::text[]
FROM pg_constraint c
JOIN pg_class cl ON cl.oid = c.conrelid
JOIN pg_namespace ns ON ns.oid = cl.relnamespace
WHERE c.contype = 'p' AND ns.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
"""

_ON_DELETE = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def schema_info(conn: Connection) -> Schema:
    """Foreign keys, NOT NULL columns and primary keys of every non-system schema.

    All schemas are read, not only the mapped ones: a foreign key from an unmapped schema
    into a mapped table still blocks a delete.
    """
    foreign_keys = [
        ForeignKey(
            name=str(name),
            schema=str(schema),
            table=str(table),
            columns=tuple(columns),
            ref_schema=str(ref_schema),
            ref_table=str(ref_table),
            ref_columns=tuple(ref_columns),
            on_delete=_ON_DELETE.get(str(action), str(action)),
        )
        for name, schema, table, columns, ref_schema, ref_table, ref_columns, action in (
            conn.execute(_FOREIGN_KEYS_SQL).fetchall()
        )
    ]
    not_null = [(str(s), str(t), str(c)) for s, t, c in conn.execute(_NOT_NULL_SQL).fetchall()]
    primary_keys = {
        (str(s), str(t)): tuple(cols) for s, t, cols in conn.execute(_PRIMARY_KEYS_SQL).fetchall()
    }
    return Schema.build(foreign_keys, not_null, primary_keys)


def normalize_sql(column: sql.Composable, how: Normalizer) -> sql.Composable:
    """The SQL twin of `vilopa.core.plan.normalize`. Keep the two in step."""
    text = sql.SQL("{}::text").format(column)
    if how is Normalizer.EMAIL:
        return sql.SQL("lower(btrim({}))").format(text)
    if how is Normalizer.IN_MOBILE:
        return sql.SQL("right(regexp_replace({}, '[^0-9]', '', 'g'), 10)").format(text)
    return text


class Source:
    """Read-only row access for the estimator (`vilopa.engine.estimate.Source`)."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def select(
        self, table_ref: str, columns: Sequence[str], conditions: Sequence[Condition]
    ) -> list[tuple[str | None, ...]]:
        if not conditions:
            return []
        _, schema, table = table_ref.split(".")
        params: list[Any] = []
        clauses: list[sql.Composable] = []
        for condition in conditions:
            if isinstance(condition, KeyCondition):
                clauses.append(
                    sql.SQL("{} = ANY ({}::text[])").format(
                        normalize_sql(sql.Identifier(condition.column), condition.normalize),
                        sql.Placeholder(),
                    )
                )
                params.append(list(condition.values))
            else:
                width = len(condition.columns)
                left = sql.SQL(", ").join(
                    sql.SQL("{}::text").format(sql.Identifier(c)) for c in condition.columns
                )
                arrays = sql.SQL(", ").join(
                    sql.SQL("{}::text[]").format(sql.Placeholder()) for _ in range(width)
                )
                clauses.append(sql.SQL("({}) IN (SELECT * FROM unnest({}))").format(left, arrays))
                params.extend([v[i] for v in condition.values] for i in range(width))
        selected = (
            sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(c)) for c in columns)
            if columns
            else sql.SQL("1")
        )
        query = sql.SQL("SELECT {} FROM {} WHERE {}").format(
            selected, sql.Identifier(schema, table), sql.SQL(" OR ").join(clauses)
        )
        rows = self.conn.execute(query, params).fetchall()
        if not columns:
            return [() for _ in rows]
        return [tuple(None if v is None else str(v) for v in row) for row in rows]

    def total(self, table_ref: str) -> int:
        _, schema, table = table_ref.split(".")
        row = self.conn.execute(
            "SELECT c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = %s",
            (schema, table),
        ).fetchone()
        if row and row[0] is not None and float(row[0]) > 0:
            return int(row[0])
        exact = self.conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
        ).fetchone()
        return int(exact[0]) if exact else 0


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
