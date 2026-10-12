from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mita.cli import app

runner = CliRunner()


def test_validate_accepts_the_demo_map(demo_datamap_path: Path) -> None:
    result = runner.invoke(app, ["datamap", "validate", str(demo_datamap_path)])
    assert result.exit_code == 0, result.output
    assert "ok (9 tables, 1 connector(s))" in result.output


def test_validate_reports_problems_and_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["datamap", "validate", str(path)])
    assert result.exit_code == 1
    assert "problem(s)" in result.output


def test_schema_prints_json() -> None:
    result = runner.invoke(app, ["datamap", "schema"])
    assert result.exit_code == 0
    assert json.loads(result.output)["title"] == "mita data map"


def test_scan_needs_the_dsn_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MITA_TEST_UNSET_DSN", raising=False)
    result = runner.invoke(app, ["scan", "--dsn-env", "MITA_TEST_UNSET_DSN"])
    assert result.exit_code == 2
    assert "MITA_TEST_UNSET_DSN is not set" in result.output


def test_lint_rejects_an_invalid_map(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    result = runner.invoke(app, ["lint", str(path)])
    assert result.exit_code == 1


def test_lint_rejects_an_unknown_connector(demo_datamap_path: Path) -> None:
    result = runner.invoke(app, ["lint", str(demo_datamap_path), "--connector", "nope"])
    assert result.exit_code == 2
    assert "no connector named 'nope'" in result.output


def test_lint_needs_the_connector_dsn(
    demo_datamap_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MITA_APP_PG_DSN", raising=False)
    result = runner.invoke(app, ["lint", str(demo_datamap_path)])
    assert result.exit_code == 2
    assert "MITA_APP_PG_DSN is not set" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("mita ")
