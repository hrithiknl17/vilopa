"""mita command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from mita import __version__
from mita.core import datamap

app = typer.Typer(
    no_args_is_help=True,
    help="Execute and verify data-erasure and access requests.",
)
datamap_app = typer.Typer(no_args_is_help=True, help="Validate and inspect data maps.")
app.add_typer(datamap_app, name="datamap")


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"mita {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """mita: delete a person's data from every system you mapped, and prove it."""


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
