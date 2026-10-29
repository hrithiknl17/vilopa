from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "examples" / "bharat-bazaar"


@pytest.fixture(scope="session")
def demo_datamap_path() -> Path:
    return DEMO_DIR / "datamap.yaml"


@pytest.fixture(scope="session")
def seed_module() -> ModuleType:
    """Import examples/bharat-bazaar/seed.py, which is a script rather than a package."""
    name = "bharat_bazaar_seed"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, DEMO_DIR / "seed.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("VILOPA_APP_PG_DSN")
    if not value:
        pytest.skip("VILOPA_APP_PG_DSN not set")
    return value


@pytest.fixture
def conn(dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SELECT current_database()").fetchone()
        if not row or row[0] != "bharat_bazaar":
            pytest.skip("refusing to seed a database not named bharat_bazaar")
        yield connection
