"""vilopa command-line interface."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer

from vilopa import __version__
from vilopa.connectors import postgres
from vilopa.core import datamap
from vilopa.core.detect import ColumnReport, Confidence
from vilopa.core.lint import LintResult, lint

app = typer.Typer(
    no_args_is_help=True,
    help="Execute and verify data-erasure and access requests.",
)
datamap_app = typer.Typer(no_args_is_help=True, help="Validate and inspect data maps.")
app.add_typer(datamap_app, name="datamap")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"vilopa {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """vilopa: delete a person's data from every system you mapped, and prove it."""


@datamap_app.command("validate")
def validate(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Check a data map for structural and cross-reference problems."""
    try:
        dm = datamap.load(path)
    except datamap.DataMapError as exc:
        for problem in exc.problems:
            typer.echo(f"{path}: {problem}", err=True)
        typer.echo(f"{path}: {len(exc.problems)} problem(s)", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"{path}: ok ({len(dm.tables)} tables, {len(dm.connectors)} connector(s))")


@datamap_app.command("schema")
def schema() -> None:
    """Print the data map JSON Schema."""
    typer.echo(json.dumps(datamap.json_schema(), indent=2))


class Threshold(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


SampleRows = Annotated[
    int, typer.Option(min=0, help="Rows sampled per table. 0 classifies by name and type only.")
]
AsJson = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


@app.command("scan")
def scan(
    dsn_env: Annotated[
        str, typer.Option(help="Environment variable holding the Postgres connection string.")
    ] = "DATABASE_URL",
    connector: Annotated[
        str, typer.Option(help="Connector name to prefix refs with, as in your data map.")
    ] = "db",
    schemas: Annotated[
        list[str] | None, typer.Option("--schema", help="Only scan this schema. Repeatable.")
    ] = None,
    sample_rows: SampleRows = 200,
    show_all: Annotated[
        bool, typer.Option("--all", help="Also list columns with no finding.")
    ] = False,
    as_json: AsJson = False,
) -> None:
    """Find columns that look like personal data in a live Postgres database.

    Reads the schema and samples values in a read-only transaction. Values are never
    printed: the report shows categories, confidences and counts only.
    """
    reports = _scan(dsn_env, schemas, sample_rows)
    shown = [r for r in reports if show_all or r.finding is not None]
    if as_json:
        typer.echo(json.dumps([_report_json(r, connector) for r in shown], indent=2))
        return
    rows = [
        (
            r.column.ref(connector),
            str(r.finding.category) if r.finding else "-",
            str(r.finding.confidence) if r.finding else "-",
            "; ".join(r.finding.reasons) if r.finding else "",
        )
        for r in shown
    ]
    _echo_table(("COLUMN", "CATEGORY", "CONFIDENCE", "WHY"), rows)
    found = [r.finding for r in reports if r.finding is not None]
    by_level = ", ".join(
        f"{sum(1 for f in found if f.confidence is level)} {level}"
        for level in reversed(Confidence)
    )
    typer.echo(
        f"\n{len(reports)} columns scanned, {len(found)} look like personal data ({by_level})"
    )


@app.command("lint")
def lint_command(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    connectors: Annotated[
        list[str] | None, typer.Option("--connector", help="Only lint this connector. Repeatable.")
    ] = None,
    fail_on: Annotated[
        Threshold, typer.Option(help="Lowest confidence that fails the lint when unmapped.")
    ] = Threshold.MEDIUM,
    sample_rows: SampleRows = 200,
    as_json: AsJson = False,
) -> None:
    """Fail when the database has personal data the data map does not cover, or the map
    names tables and columns the database no longer has."""
    try:
        dm = datamap.load(path)
    except datamap.DataMapError as exc:
        for problem in exc.problems:
            typer.echo(f"{path}: {problem}", err=True)
        raise typer.Exit(1) from exc

    declared = {c.name: c for c in dm.connectors}
    for name in connectors or ():
        if name not in declared:
            typer.echo(f"{path}: no connector named {name!r}", err=True)
            raise typer.Exit(2)
    selected = [declared[name] for name in connectors] if connectors else list(dm.connectors)

    threshold = Confidence[fail_on.name]
    results: dict[str, LintResult] = {}
    for connector in selected:
        reports = _scan(connector.dsn_env, None, sample_rows)
        results[connector.name] = lint(dm, connector.name, reports, fail_on=threshold)

    failed = any(result.errors for result in results.values())
    if as_json:
        out: dict[str, Any] = {
            "ok": not failed,
            "connectors": {
                name: {
                    "issues": [
                        {"severity": str(i.severity), "ref": i.ref, "message": i.message}
                        for i in result.issues
                    ],
                    "below_threshold": result.below_threshold,
                }
                for name, result in results.items()
            },
        }
        typer.echo(json.dumps(out, indent=2))
    else:
        for name, result in results.items():
            for issue in result.issues:
                typer.echo(f"{path}: {issue}", err=True)
            warnings = len(result.issues) - len(result.errors)
            summary = f"{path}: {name}: {len(result.errors)} error(s), {warnings} warning(s)"
            if result.below_threshold:
                summary += (
                    f"; {result.below_threshold} finding(s) below {fail_on} confidence"
                    " not checked (see vilopa scan)"
                )
            typer.echo(summary)
    if failed:
        raise typer.Exit(1)


def _scan(dsn_env: str, schemas: Sequence[str] | None, sample_rows: int) -> list[ColumnReport]:
    dsn = os.environ.get(dsn_env)
    if not dsn:
        typer.echo(f"environment variable {dsn_env} is not set", err=True)
        raise typer.Exit(2)
    try:
        with postgres.connect(dsn) as conn:
            return postgres.scan(conn, schemas=schemas, sample_rows=sample_rows)
    except psycopg.Error as exc:
        typer.echo(f"cannot scan the database in ${dsn_env}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _report_json(report: ColumnReport, connector: str) -> dict[str, Any]:
    finding = report.finding
    return {
        "ref": report.column.ref(connector),
        "data_type": report.column.data_type,
        "sampled": report.sampled,
        "category": str(finding.category) if finding else None,
        "confidence": str(finding.confidence) if finding else None,
        "reasons": list(finding.reasons) if finding else [],
    }


def _echo_table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header) - 1)]
    for row in (header, *rows):
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        typer.echo("  ".join([*cells, row[-1]]).rstrip())
