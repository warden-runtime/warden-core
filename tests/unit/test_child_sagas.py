"""Unit tests for child-saga helpers and blueprint spawn/join wiring."""

from __future__ import annotations

import pytest
from common.child_sagas import (
    build_child_resolve_context,
    child_start_idempotency_key,
    resolve_items_from_context,
    summarize_join_children,
    validate_spawn_items,
)
from common.schemas.saga import HydratedSagaBlueprint
from pydantic import ValidationError


def test_child_start_idempotency_key_stable():
    a = child_start_idempotency_key("parent", "spawn", "item-1")
    b = child_start_idempotency_key("parent", "spawn", "item-1")
    c = child_start_idempotency_key("parent", "spawn", "item-2")
    assert a == b
    assert a != c
    assert len(a) == 64


def test_validate_spawn_items_ok():
    items = [{"id": "a", "x": 1}, {"id": "b"}]
    out = validate_spawn_items(items, max_children=8)
    assert out == [("a", items[0]), ("b", items[1])]


def test_validate_spawn_items_empty():
    with pytest.raises(ValueError, match="SPAWN_EMPTY_ITEMS"):
        validate_spawn_items([])


def test_validate_spawn_items_too_many():
    items = [{"id": str(i)} for i in range(5)]
    with pytest.raises(ValueError, match="TOO_MANY_CHILDREN"):
        validate_spawn_items(items, max_children=3)


def test_validate_spawn_items_missing_id():
    with pytest.raises(ValueError, match="SPAWN_ITEM_MISSING_ID"):
        validate_spawn_items([{"name": "nope"}])


def test_resolve_items_and_context():
    ctx = {"steps": {"plan": {"output": {"data": {"items": [{"id": "x"}]}}}}}
    items = resolve_items_from_context("$.steps.plan.output.data.items", ctx)
    assert items == [{"id": "x"}]
    enriched = build_child_resolve_context(ctx, items[0], item_var="hypothesis")
    assert enriched["item"] == items[0]
    assert enriched["hypothesis"] == items[0]
    assert enriched["steps"] == ctx["steps"]


def test_summarize_join_children():
    summary = summarize_join_children(
        [
            {"status": "COMPLETED"},
            {"status": "FAILED"},
            {"status": "COMPENSATED"},
        ]
    )
    assert summary == {"total": 3, "succeeded": 1, "failed": 2}


def _parent_blueprint(**spawn_extra):
    spawn = {
        "saga_name": "child",
        "saga_version": "1.0.0",
        "items_from": "$.steps.plan.output.data.items",
        "result_from": "$.steps.done.output.data",
        "input": {"payload": {"from": "$.item"}},
        **spawn_extra,
    }
    return {
        "kind": "saga",
        "name": "parent",
        "namespace": "default",
        "version": "1.0.0",
        "description": "test",
        "steps": [
            {
                "id": "plan",
                "name": "Plan",
                "kind": "reason",
                "worker": "w",
                "worker_version": "1.0.0",
                "prompt": "p.j2",
                "agent-adapter": "simple",
            },
            {
                "id": "dispatch",
                "name": "Dispatch",
                "kind": "spawn_sagas",
                "spawn": spawn,
            },
            {
                "id": "await",
                "name": "Await",
                "kind": "join_sagas",
                "join": {"spawn_step_id": "dispatch"},
            },
        ],
    }


def test_blueprint_spawn_join_ok():
    bp = HydratedSagaBlueprint(**_parent_blueprint())
    assert bp.steps[1].kind == "spawn_sagas"
    assert bp.steps[2].join.spawn_step_id == "dispatch"


def test_blueprint_requires_result_from():
    data = _parent_blueprint()
    del data["steps"][1]["spawn"]["result_from"]
    with pytest.raises(ValidationError):
        HydratedSagaBlueprint(**data)


def test_blueprint_join_unknown_spawn():
    data = _parent_blueprint()
    data["steps"][2]["join"]["spawn_step_id"] = "missing"
    with pytest.raises(ValidationError, match="unknown spawn_step_id"):
        HydratedSagaBlueprint(**data)


def test_blueprint_duplicate_join_target():
    data = _parent_blueprint()
    data["steps"].append(
        {
            "id": "await2",
            "name": "Await2",
            "kind": "join_sagas",
            "join": {"spawn_step_id": "dispatch"},
        }
    )
    with pytest.raises(ValidationError, match="more than one join"):
        HydratedSagaBlueprint(**data)


@pytest.mark.asyncio
async def test_execute_spawn_sagas_maps_inactive_child_to_step_failed(mocker):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from engine.api.saga_errors import InactiveSagaDefinitionError
    from engine.child_sagas import execute_spawn_sagas

    data = _parent_blueprint(items_from="$.input.items")
    saga = SimpleNamespace(
        trace_id="a" * 32,
        namespace="default",
        context={"input": {"items": [{"id": "one"}]}},
        frozen_steps=data["steps"],
    )
    step = SimpleNamespace(step_id="dispatch", span_id="b" * 16)
    mocker.patch(
        "engine.child_sagas._require_saga_definition",
        side_effect=InactiveSagaDefinitionError(
            namespace="default", name="child-saga", version="1.0.0"
        ),
    )
    fail = mocker.patch("engine.logic._apply_step_failure_lifecycle", new_callable=AsyncMock)
    finalize = mocker.patch(
        "engine.logic._finalize_step_output_and_advance", new_callable=AsyncMock
    )

    await execute_spawn_sagas(saga=saga, step=step, db_conn=object())

    fail.assert_awaited_once()
    finalize.assert_not_awaited()
    synthetic = fail.await_args.args[2]
    assert synthetic.error_details["code"] == "SPAWN_CHILD_DEFINITION_INACTIVE"


@pytest.mark.asyncio
async def test_execute_spawn_sagas_maps_missing_child_to_step_failed(mocker):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from engine.api.saga_errors import DefinitionNotFoundError
    from engine.child_sagas import execute_spawn_sagas

    data = _parent_blueprint(items_from="$.input.items")
    saga = SimpleNamespace(
        trace_id="a" * 32,
        namespace="default",
        context={"input": {"items": [{"id": "one"}]}},
        frozen_steps=data["steps"],
    )
    step = SimpleNamespace(step_id="dispatch", span_id="b" * 16)
    mocker.patch(
        "engine.child_sagas._require_saga_definition",
        side_effect=DefinitionNotFoundError(
            namespace="default", name="child-saga", version="1.0.0"
        ),
    )
    fail = mocker.patch("engine.logic._apply_step_failure_lifecycle", new_callable=AsyncMock)

    await execute_spawn_sagas(saga=saga, step=step, db_conn=object())

    synthetic = fail.await_args.args[2]
    assert synthetic.error_details["code"] == "SPAWN_CHILD_DEFINITION_NOT_FOUND"


@pytest.mark.asyncio
async def test_execute_spawn_sagas_maps_hydrate_value_error_to_step_failed(mocker):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from engine.child_sagas import execute_spawn_sagas

    data = _parent_blueprint(items_from="$.input.items")
    saga = SimpleNamespace(
        trace_id="a" * 32,
        namespace="default",
        context={"input": {"items": [{"id": "one"}, {"id": "two"}]}},
        frozen_steps=data["steps"],
    )
    step = SimpleNamespace(step_id="dispatch", span_id="b" * 16)
    mocker.patch(
        "engine.child_sagas._require_saga_definition",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(id="def-1"),
    )
    mocker.patch(
        "engine.child_sagas._resolve_idempotent_start",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch(
        "engine.child_sagas._create_saga_and_steps",
        new_callable=AsyncMock,
        side_effect=ValueError("Step definition 'missing'@'1.0.0' is inactive."),
    )
    fail = mocker.patch("engine.logic._apply_step_failure_lifecycle", new_callable=AsyncMock)
    finalize = mocker.patch(
        "engine.logic._finalize_step_output_and_advance", new_callable=AsyncMock
    )

    await execute_spawn_sagas(saga=saga, step=step, db_conn=object())

    fail.assert_awaited_once()
    finalize.assert_not_awaited()
    synthetic = fail.await_args.args[2]
    assert synthetic.error_details["code"] == "SPAWN_CHILD_HYDRATE_FAILED"
    assert "inactive" in synthetic.error_details["message"]
