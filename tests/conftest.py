# tests/conftest.py
import os
from pathlib import Path

import pytest
from tortoise import Tortoise

# Ensure PROMPTS_ROOT is set for saga engine tests (file-based prompts)
_project_root = Path(__file__).resolve().parent.parent
_prompts_dir = _project_root / "tests" / "fixtures" / "prompts"
if _prompts_dir.is_dir():
    os.environ.setdefault("PROMPTS_ROOT", str(_prompts_dir))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _oss_plugin_registry():
    """OSS tests use NoOp plugin defaults unless a test registers hooks explicitly."""
    from common.plugins import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
async def initialize_tests():
    """In-memory SQLite with OSS models only."""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["common.models"]},
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()
