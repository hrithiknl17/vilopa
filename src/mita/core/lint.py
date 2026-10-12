"""Compare a data map with what a scan found in the live database.

Two kinds of drift fail a lint:

1. Stale map: a table or column the map names no longer exists (renamed, dropped). An
   erasure plan built from a stale map would miss data or fail halfway.
2. Uncovered data: a column that looks like personal data at or above the threshold
   confidence is not listed in the map. This is what catches a migration that adds
   `alt_mobile` without anyone updating the map.

A column listed as `pii: none` is a reviewed decision and never fails the lint, but strong
evidence against it is reported as a warning.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from mita.core.datamap import DataMap, PiiCategory, Table
from mita.core.detect import ColumnReport, Confidence, Finding


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    ref: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.ref}: {self.message}"


@dataclass(frozen=True)
class LintResult:
    issues: list[Issue] = field(default_factory=list)
    below_threshold: int = 0
    """Findings under the confidence threshold, not reported. `mita scan` lists them."""

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]


def lint(
    dm: DataMap,
    connector: str,
    reports: Sequence[ColumnReport],
    *,
    fail_on: Confidence = Confidence.MEDIUM,
) -> LintResult:
    """Lint the part of `dm` that lives on `connector` against that connector's scan."""
    live: dict[str, dict[str, ColumnReport]] = {}
    for report in reports:
        live.setdefault(report.column.table_ref(connector), {})[report.column.name] = report

    issues: list[Issue] = []
    prefix = f"{connector}."

    for table_ref, table in dm.tables.items():
        if not table_ref.startswith(prefix):
            continue
        live_columns = live.get(table_ref)
        if live_columns is None:
            issues.append(
                Issue(Severity.ERROR, table_ref, "in the data map but not in the database")
            )
            continue
        for name in _named_columns(table):
            if name not in live_columns:
                issues.append(
                    Issue(
                        Severity.ERROR,
                        f"{table_ref}.{name}",
                        "in the data map but not in the database",
                    )
                )

    for relation in dm.relations:
        for column_ref in (relation.from_, relation.to):
            table_ref, name = column_ref.rsplit(".", 1)
            if table_ref in live and name not in live[table_ref]:
                issues.append(
                    Issue(Severity.ERROR, column_ref, "relation column is not in the database")
                )

    below = 0
    for report in reports:
        finding = report.finding
        if finding is None or finding.category is PiiCategory.NONE:
            continue
        if finding.confidence < fail_on:
            below += 1
            continue
        ref = report.column.ref(connector)
        mapped_table = dm.tables.get(report.column.table_ref(connector))
        mapped = mapped_table.columns.get(report.column.name) if mapped_table else None
        if mapped is None:
            issues.append(
                Issue(Severity.ERROR, ref, f"looks like {_describe(finding)}; not in the data map")
            )
        elif mapped.pii is PiiCategory.NONE:
            issues.append(
                Issue(
                    Severity.WARNING,
                    ref,
                    f"mapped as pii: none but looks like {_describe(finding)}",
                )
            )

    return LintResult(issues, below)


def _named_columns(table: Table) -> Iterator[str]:
    """Every column a table entry refers to, each once, in document order."""
    seen: set[str] = set()
    names = [*table.columns, *(m.column for m in table.match)]
    if table.retain and table.retain.from_column:
        names.append(table.retain.from_column)
    for column in table.columns.values():
        if column.retain and column.retain.from_column:
            names.append(column.retain.from_column)
    for name in names:
        if name not in seen:
            seen.add(name)
            yield name


def _describe(finding: Finding) -> str:
    return f"{finding.category} ({finding.confidence}: {'; '.join(finding.reasons)})"
