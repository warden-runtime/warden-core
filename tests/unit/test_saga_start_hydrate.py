"""Start-path hydrate: authoring AST → instance frozen_steps."""

from __future__ import annotations

import json

import pytest
from common.models import SagaDefinition, SagaInstance, SagaStepInstance, StepDefinition
from engine.api.saga_start import start_saga
from engine.registry.service import RegistryService

WORKER_YAML = """
kind: worker
name: hydrate-worker
namespace: default
version: "1.0.0"
provider: mock
model_name: demo
system_prompt: You are helpful.
tool_sources: []
adapter: langchain
"""

STEP_YAML = """
kind: step
name: hydrate-step
namespace: default
version: "1.0.0"
title: Hydrated
inputs: {}
step_kind: reason
worker: hydrate-worker
worker_version: "1.0.0"
prompt: noop.j2
tools:
  allow: []
timeout_seconds: 300
"""

SAGA_YAML = """
kind: saga
name: hydrate-saga
namespace: default
version: "1.0.0"
description: Authoring AST only
steps:
  - id: step1
    use: hydrate-step
    version: "1.0.0"
    with: {}
"""


@pytest.mark.asyncio
async def test_start_saga_hydrates_use_refs_into_frozen_steps() -> None:
    service = RegistryService()
    await service.register_manifest(WORKER_YAML)
    await service.register_manifest(STEP_YAML)
    await service.register_manifest(SAGA_YAML)

    definition = await SagaDefinition.get(name="hydrate-saga", namespace="default", version="1.0.0")
    assert definition.body["steps"][0]["use"] == "hydrate-step"
    assert "worker" not in definition.body["steps"][0]

    result = await start_saga(
        namespace="default",
        name="hydrate-saga",
        version="1.0.0",
        input={},
    )
    saga = await SagaInstance.get(trace_id=result.trace_id)
    assert saga.frozen_steps is not None
    assert len(saga.frozen_steps) == 1
    frozen = saga.frozen_steps[0]
    assert frozen["kind"] == "reason"
    assert frozen["id"] == "step1"
    assert frozen["worker"] == "hydrate-worker"
    assert frozen["prompt"] == "noop.j2"
    assert frozen.get("prompt_definition")
    assert "No-op prompt" in frozen["prompt_definition"]
    assert frozen["step_definition_name"] == "hydrate-step"
    assert frozen["step_definition_version"] == "1.0.0"

    step = await SagaStepInstance.get(saga_trace_id=result.trace_id, step_id="step1")
    assert step.step_definition_name == "hydrate-step"
    assert step.worker == "hydrate-worker"
    assert step.prompt_definition == frozen["prompt_definition"]


@pytest.mark.asyncio
async def test_start_saga_freezes_policy_definition_onto_steps(monkeypatch, tmp_path) -> None:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "gate.yaml").write_text(
        'name: gate\nversion: "3"\ncel: "true"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("POLICIES_ROOT", str(policies))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        service = RegistryService()
        await service.register_manifest(WORKER_YAML)
        step_yaml = """
kind: step
name: hydrate-policy-step
namespace: default
version: "1.0.0"
title: Gated
inputs: {}
step_kind: reason
worker: hydrate-worker
worker_version: "1.0.0"
prompt: noop.j2
policy: gate.yaml
tools:
  allow: []
timeout_seconds: 300
"""
        await service.register_manifest(step_yaml)
        saga_yaml = """
kind: saga
name: hydrate-policy-saga
namespace: default
version: "1.0.0"
description: Policy freeze
steps:
  - id: step1
    use: hydrate-policy-step
    version: "1.0.0"
    with: {}
"""
        await service.register_manifest(saga_yaml)
        result = await start_saga(
            namespace="default",
            name="hydrate-policy-saga",
            version="1.0.0",
            input={},
        )
        saga = await SagaInstance.get(trace_id=result.trace_id)
        frozen = saga.frozen_steps[0]
        assert frozen["policy"] == "gate.yaml"
        assert frozen["policy_definition"] == {
            "name": "gate",
            "version": "3",
            "cel": "true",
        }
        step = await SagaStepInstance.get(saga_trace_id=result.trace_id, step_id="step1")
        assert step.policy_name == "gate.yaml"
        assert step.policy_definition == {
            "name": "gate",
            "version": "3",
            "cel": "true",
        }
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_start_saga_freezes_output_schema_definition_onto_steps(
    monkeypatch, tmp_path
) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    schema_payload = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    (schemas / "hydrate-out.json").write_text(
        json.dumps(schema_payload),
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEMAS_ROOT", str(schemas))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        service = RegistryService()
        await service.register_manifest(WORKER_YAML)
        step_yaml = """
kind: step
name: hydrate-schema-step
namespace: default
version: "1.0.0"
title: Schema
inputs: {}
step_kind: reason
worker: hydrate-worker
worker_version: "1.0.0"
prompt: noop.j2
output_schema: hydrate-out.json
tools:
  allow: []
timeout_seconds: 300
"""
        await service.register_manifest(step_yaml)
        saga_yaml = """
kind: saga
name: hydrate-schema-saga
namespace: default
version: "1.0.0"
description: Schema freeze
steps:
  - id: step1
    use: hydrate-schema-step
    version: "1.0.0"
    with: {}
"""
        await service.register_manifest(saga_yaml)
        result = await start_saga(
            namespace="default",
            name="hydrate-schema-saga",
            version="1.0.0",
            input={},
        )
        saga = await SagaInstance.get(trace_id=result.trace_id)
        frozen = saga.frozen_steps[0]
        assert frozen["output_schema"] == "hydrate-out.json"
        assert frozen["output_schema_definition"] == schema_payload
        step = await SagaStepInstance.get(saga_trace_id=result.trace_id, step_id="step1")
        assert step.output_schema == schema_payload
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_child_spawn_hydrates_child_definition_into_own_frozen_steps() -> None:
    """Child start resolves the child saga's use: refs into the child instance freeze."""
    from common.config import get_settings
    from engine.api.saga_start import _create_saga_and_steps
    from tortoise.transactions import in_transaction

    settings = get_settings()
    service = RegistryService()
    await service.register_manifest(WORKER_YAML)
    await service.register_manifest(STEP_YAML)
    child_yaml = """
kind: saga
name: child-hydrate-saga
namespace: default
version: "1.0.0"
description: Child authoring
steps:
  - id: child-step
    use: hydrate-step
    version: "1.0.0"
    with: {}
"""
    await service.register_manifest(child_yaml)
    child_def = await SagaDefinition.get(
        name="child-hydrate-saga", namespace="default", version="1.0.0"
    )
    assert child_def.body["steps"][0]["use"] == "hydrate-step"

    async with in_transaction() as conn:
        child_trace = await _create_saga_and_steps(
            conn=conn,
            definition=child_def,
            namespace="default",
            name="child-hydrate-saga",
            version="1.0.0",
            input={},
            idempotency_key=None,
            schemas_root=None,
            compensations_root=None,
            prompts_root=settings.prompts_root,
            skills_root=settings.skills_root,
            parent_trace_id="a" * 32,
        )

    child = await SagaInstance.get(trace_id=child_trace)
    assert child.parent_trace_id == "a" * 32
    assert child.frozen_steps[0]["kind"] == "reason"
    assert child.frozen_steps[0]["step_definition_name"] == "hydrate-step"
    assert child.frozen_steps[0].get("prompt_definition")
    assert await StepDefinition.filter(name="hydrate-step").count() == 1
