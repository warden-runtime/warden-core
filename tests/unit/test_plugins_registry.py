"""Unit tests for common.plugins registry (no DB, no audit imports)."""

from __future__ import annotations

import pytest
from common.messaging.postgres import PostgresQueueConsumer, PostgresQueueProducer
from common.plugins import (
    ExecutionScope,
    NoOpEngineLifecycleHooks,
    get_registry,
    register_engine_hooks,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_registry()
    yield
    reset_registry()


def test_get_registry_returns_same_singleton():
    a = get_registry()
    b = get_registry()
    assert a is b


def test_defaults_are_noop_and_postgres_messaging():
    reg = get_registry()
    assert isinstance(reg.engine, NoOpEngineLifecycleHooks)
    assert isinstance(reg.messaging.create_producer(), PostgresQueueProducer)
    consumer = reg.messaging.create_consumer("t", "g", _noop_handler)
    assert isinstance(consumer, PostgresQueueConsumer)


async def _noop_handler(_msg: dict) -> None:
    return None


@pytest.mark.asyncio
async def test_noop_engine_hook_is_awaitable():
    reg = get_registry()
    await reg.engine.on_saga_created(saga=None)


def test_register_engine_hooks_replaces_domain():
    custom = NoOpEngineLifecycleHooks()
    register_engine_hooks(custom)
    assert get_registry().engine is custom


def test_reset_registry_restores_defaults():
    custom = NoOpEngineLifecycleHooks()
    register_engine_hooks(custom)
    reset_registry()
    assert get_registry().engine is not custom
    assert isinstance(get_registry().engine, NoOpEngineLifecycleHooks)


def test_execution_scope_from_injection():
    from common.plugins.context import execution_scope_from_injection

    scope = ExecutionScope(
        namespace="default",
        trace_id="a" * 32,
        step_span_id="b" * 16,
        idempotency_key="k",
        command_type="DO_STEP",
        worker_name="w",
    )
    assert execution_scope_from_injection({"execution_scope": scope}) is scope
    assert (
        execution_scope_from_injection(
            {
                "execution_scope": {
                    "namespace": "default",
                    "trace_id": "a" * 32,
                    "step_span_id": "b" * 16,
                    "idempotency_key": "k",
                    "command_type": "DO_STEP",
                    "worker_name": "w",
                }
            }
        )
        == scope
    )


def test_plugins_modules_do_not_reference_common_audit():
    from pathlib import Path

    import common.plugins

    plugins_dir = Path(common.plugins.__file__).resolve().parent
    for path in plugins_dir.glob("*.py"):
        assert "common.audit" not in path.read_text(encoding="utf-8"), (
            f"{path.name} must not import common.audit"
        )


def test_kernel_import_gate_script_passes():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    script = root / "scripts" / "check_open_core_boundary.sh"
    result = subprocess.run(  # noqa: S603
        [str(script)], cwd=root, check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
