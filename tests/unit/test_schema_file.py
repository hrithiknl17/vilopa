"""The committed JSON Schema must match the pydantic model it is generated from."""

from __future__ import annotations

import json
from pathlib import Path

from mita.core import datamap

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schemas" / "datamap.schema.json"


def test_committed_schema_is_current() -> None:
    committed = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert committed == datamap.json_schema(), (
        "schemas/datamap.schema.json is stale. Regenerate it with:\n"
        "  uv run mita datamap schema > schemas/datamap.schema.json"
    )
