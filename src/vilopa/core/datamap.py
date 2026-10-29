"""The data map: where a person's data lives and what erasure does to it.

The map is a YAML file kept in the user's repository and reviewed in pull requests.
Loading happens in two stages:

1. Structure (pydantic): shapes, enums and required fields; unknown keys are rejected.
2. Semantics (`check`): cross-references a JSON Schema cannot express. Every problem is
   reported at once, so a user can fix their map in a single pass.

Names are addressed as `connector.schema.table` and `connector.schema.table.column`.
Schema, table and column names may hold any character except `.` and whitespace; they
are only ever sent to a database through `psycopg.sql.Identifier`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

_PART = r"[^.\s]+"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
TABLE_REF_PATTERN = rf"^[a-z][a-z0-9_]{{0,62}}\.{_PART}\.{_PART}$"
COLUMN_REF_PATTERN = rf"^[a-z][a-z0-9_]{{0,62}}\.{_PART}\.{_PART}\.{_PART}$"

Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
TableRef = Annotated[str, StringConstraints(pattern=TABLE_REF_PATTERN)]
ColumnRef = Annotated[str, StringConstraints(pattern=COLUMN_REF_PATTERN)]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# ISO 8601 date period with at least one part (P6Y, P18M, P1Y6M, P30D). No lookahead:
# pydantic's regex engine does not support it.
IsoPeriod = Annotated[str, StringConstraints(pattern=r"^P(\d+Y(\d+M)?(\d+D)?|\d+M(\d+D)?|\d+D)$")]
EnvVar = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]


class PiiCategory(StrEnum):
    """What kind of personal data a column holds. `none` marks a reviewed, non-PII column."""

    EMAIL = "email"
    PHONE = "phone"
    NAME = "name"
    ADDRESS = "address"
    PIN_CODE = "pin_code"
    PAN = "pan"
    AADHAAR = "aadhaar"
    DOB = "dob"
    IP_ADDRESS = "ip_address"
    GSTIN = "gstin"
    UPI_ID = "upi_id"
    PAYMENT_REF = "payment_ref"
    FREE_TEXT = "free_text"
    OTHER = "other"
    NONE = "none"


class Normalizer(StrEnum):
    """How an identifier is normalised before it is matched or hashed."""

    EXACT = "exact"
    EMAIL = "email"
    IN_MOBILE = "in_mobile"


class TableErase(StrEnum):
    """What erasure does to a matching row."""

    DELETE_ROW = "delete_row"
    UPDATE_COLUMNS = "update_columns"
    RETAIN = "retain"


class ColumnStrategy(StrEnum):
    """What erasure does to one column of a row that is kept (`update_columns`)."""

    NULLIFY = "nullify"
    PSEUDONYMIZE = "pseudonymize"
    RETAIN = "retain"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Retention(_Model):
    """Why data is kept despite an erasure request, and for how long."""

    reason: NonEmpty = Field(description="Plain-language reason. Shown in evidence records.")
    period: IsoPeriod = Field(description="ISO 8601 period, e.g. P6Y or P18M.")
    from_column: str | None = Field(
        default=None, description="Timestamp column the period counts from."
    )
    rule_ref: str | None = Field(
        default=None, description="The law or policy clause this relies on."
    )


class Column(_Model):
    pii: PiiCategory
    strategy: ColumnStrategy | None = Field(
        default=None, description="Required for PII columns in `update_columns` tables."
    )
    retain: Retention | None = Field(default=None, description="Required when strategy is retain.")
    note: str | None = None


class Match(_Model):
    """A column that identifies the principal directly, without a foreign-key path."""

    key: Name = Field(description="A key declared under `principal.keys`.")
    column: str


class Table(_Model):
    erase: TableErase
    retain: Retention | None = Field(
        default=None, description="Required when erase is retain; the whole row is kept."
    )
    match: tuple[Match, ...] = ()
    columns: dict[str, Column] = Field(default_factory=dict)


class PrincipalKey(_Model):
    column: str = Field(description="Column of the root table holding this identifier.")
    normalize: Normalizer = Normalizer.EXACT


class Principal(_Model):
    root: TableRef = Field(description="The table whose row is the person.")
    keys: dict[Name, PrincipalKey] = Field(min_length=1)


class Connector(_Model):
    name: Name
    kind: Literal["postgres"]
    dsn_env: EnvVar = Field(description="Environment variable holding the connection string.")


class Relation(_Model):
    """A link the database does not enforce as a foreign key."""

    from_: ColumnRef = Field(alias="from")
    to: ColumnRef


class DataMap(_Model):
    version: Literal[0]
    connectors: tuple[Connector, ...] = Field(min_length=1)
    principal: Principal
    tables: dict[TableRef, Table] = Field(min_length=1)
    relations: tuple[Relation, ...] = ()


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class DataMapError(Exception):
    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s) in data map")


def load(path: Path) -> DataMap:
    """Read, parse and validate a data map. Raises DataMapError listing every problem."""
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise DataMapError([Problem("$", f"invalid YAML: {exc}")]) from exc
    return from_obj(raw)


def from_obj(raw: object) -> DataMap:
    """Validate an already-parsed object. Raises DataMapError listing every problem."""
    try:
        datamap = DataMap.model_validate(raw)
    except ValidationError as exc:
        raise DataMapError(
            [Problem(_format_loc(err["loc"]), err["msg"]) for err in exc.errors()]
        ) from exc
    problems = check(datamap)
    if problems:
        raise DataMapError(problems)
    return datamap


def check(dm: DataMap) -> list[Problem]:
    """Cross-reference checks. Returns every problem found, in document order."""
    problems: list[Problem] = []

    def add(path: str, message: str) -> None:
        problems.append(Problem(path, message))

    connector_names: set[str] = set()
    for i, connector in enumerate(dm.connectors):
        if connector.name in connector_names:
            add(f"connectors[{i}].name", f"duplicate connector {connector.name!r}")
        connector_names.add(connector.name)

    root = dm.tables.get(dm.principal.root)
    if root is None:
        add("principal.root", f"table {dm.principal.root!r} is not declared under tables")
    else:
        if root.erase is TableErase.RETAIN:
            add("principal.root", "the root table cannot use erase: retain")
        for key, spec in dm.principal.keys.items():
            if spec.column not in root.columns:
                add(
                    f"principal.keys.{key}.column",
                    f"column {spec.column!r} is not listed under the root table's columns",
                )

    for ref, table in dm.tables.items():
        base = f"tables[{ref!r}]"
        connector_name = ref.split(".", 1)[0]
        if connector_name not in connector_names:
            add(base, f"connector {connector_name!r} is not declared")
        problems.extend(_check_table(base, table))
        for i, match in enumerate(table.match):
            if match.key not in dm.principal.keys:
                add(
                    f"{base}.match[{i}].key", f"key {match.key!r} is not declared in principal.keys"
                )
            if match.column not in table.columns:
                add(
                    f"{base}.match[{i}].column",
                    f"column {match.column!r} is not listed under this table's columns",
                )

    for i, relation in enumerate(dm.relations):
        if relation.from_ == relation.to:
            add(f"relations[{i}]", "a relation cannot point at itself")
        for side, column_ref in (("from", relation.from_), ("to", relation.to)):
            table_ref = column_ref.rsplit(".", 1)[0]
            if table_ref not in dm.tables:
                add(f"relations[{i}].{side}", f"table {table_ref!r} is not declared under tables")

    return problems


def _check_table(base: str, table: Table) -> list[Problem]:
    problems: list[Problem] = []

    if table.erase is TableErase.RETAIN and table.retain is None:
        problems.append(Problem(base, "erase: retain needs a retain block with a reason"))
    if table.erase is not TableErase.RETAIN and table.retain is not None:
        problems.append(Problem(f"{base}.retain", "a table retain block needs erase: retain"))

    has_strategy = False
    for name, column in table.columns.items():
        path = f"{base}.columns.{name}"
        if column.strategy is not None:
            has_strategy = True
            if table.erase is not TableErase.UPDATE_COLUMNS:
                problems.append(
                    Problem(f"{path}.strategy", "strategy only applies to update_columns tables")
                )
        elif table.erase is TableErase.UPDATE_COLUMNS and column.pii is not PiiCategory.NONE:
            problems.append(Problem(path, f"{column.pii} column needs a strategy"))

        if column.strategy is ColumnStrategy.RETAIN and column.retain is None:
            problems.append(Problem(path, "strategy: retain needs a retain block with a reason"))
        if column.strategy is not ColumnStrategy.RETAIN and column.retain is not None:
            problems.append(Problem(f"{path}.retain", "a retain block needs strategy: retain"))

    if table.erase is TableErase.UPDATE_COLUMNS and not has_strategy:
        problems.append(Problem(base, "update_columns table has no column with a strategy"))

    return problems


def json_schema() -> dict[str, Any]:
    """The JSON Schema for data map files, as committed in schemas/datamap.schema.json."""
    schema = DataMap.model_json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "vilopa data map",
        **{k: v for k, v in schema.items() if k != "title"},
    }


def _format_loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif "." in part:
            out += f"[{part!r}]"
        else:
            out += f".{part}" if out else part
    return out or "$"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys, which PyYAML otherwise silently overwrites.

    A duplicated table key would make a whole table vanish from the map without warning.
    """


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
