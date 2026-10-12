from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

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
