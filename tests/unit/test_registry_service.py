"""Unit tests for engine.registry.service.RegistryService and manifest validation helpers."""

import json

import pytest
import yaml
from common.catalog_errors import CatalogDefinitionNotFoundError, InactiveCatalogDefinitionError
from common.config import get_settings
from common.manifest_validation import manifest_validation_error
from common.models import SagaDefinition, StepDefinition, WorkerDefinition
from engine.registry.service import RegistryService
from pydantic import ValidationError

WORKER_YAML = """
kind: worker
name: test-worker
namespace: default
version: "1.0.0"
description: A test worker
provider: openai
model_name: gpt-4o
system_prompt: You are helpful.
tool_sources: []
adapter: langchain
"""

STEP_YAML = """
kind: step
name: test-step
namespace: default
version: "1.0.0"
title: First
inputs: {}
step_kind: reason
worker: test-worker
worker_version: "1.0.0"
prompt: step1.j2
timeout_seconds: 600
"""

SAGA_YAML = """
kind: saga
name: test-saga
namespace: default
version: "1.0.0"
description: A test saga
steps:
  - id: step-1
    use: test-step
    version: "1.0.0"
    with: {}
"""


async def _create_worker(name: str, version: str = "1.0.0") -> WorkerDefinition:
    """Insert a worker row directly (deploy order is worker -> step -> saga)."""
    from tests.factories import worker_definition_body

    return await WorkerDefinition.create(
        namespace="default",
        name=name,
        version=version,
        body=worker_definition_body(name=name, version=version),
    )


@pytest.mark.asyncio
async def test_register_manifest_invalid_yaml_raises():
    """register_manifest raises ValueError when YAML is invalid."""
    service = RegistryService()
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest("not: valid: yaml: [[[")
    assert "invalid yaml" in str(exc_info.value).lower() or "yaml" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_register_manifest_non_dict_root_raises():
    """register_manifest raises ValueError when YAML root is not a dict."""
    service = RegistryService()
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest("- list\n- items")
    assert "mapping" in str(exc_info.value).lower() or "dict" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_register_manifest_unknown_kind_raises():
    """register_manifest accepts worker | step | saga and rejects anything else."""
    service = RegistryService()
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest("kind: pipeline\nname: p1")
    message = str(exc_info.value)
    assert "unknown" in message.lower()
    assert "pipeline" in message


@pytest.mark.asyncio
async def test_register_manifest_worker_creates_definition():
    """register_manifest with kind worker creates a WorkerDefinition row."""
    service = RegistryService()
    msg = await service.register_manifest(WORKER_YAML)

    assert "registered successfully" in msg.lower()
    assert "test-worker" in msg

    w = await WorkerDefinition.get_or_none(name="test-worker", namespace="default", version="1.0.0")
    assert w is not None
    assert w.body["provider"] == "openai"
    assert w.body["model_name"] == "gpt-4o"
    assert w.version == "1.0.0"
    assert w.body["system_prompt"] == "You are helpful."
    assert w.body["adapter"] == "langchain"
    assert w.body.get("temperature") == 0.0


@pytest.mark.asyncio
async def test_register_manifest_worker_rejects_same_version_overwrite():
    """Worker versions are append-only; redeploying the same pin fails."""
    service = RegistryService()
    await service.register_manifest(WORKER_YAML)
    with pytest.raises(ValueError, match="already exists and is immutable") as exc_info:
        await service.register_manifest(WORKER_YAML)
    assert "Bump the version" in str(exc_info.value)


@pytest.mark.asyncio
async def test_register_manifest_worker_versions_are_distinct_rows():
    """Deploying the same worker name with a new version creates a separate definition row."""
    service = RegistryService()
    await service.register_manifest(WORKER_YAML)
    v2_yaml = WORKER_YAML.replace('version: "1.0.0"', 'version: "2.0.0"')
    await service.register_manifest(v2_yaml)

    rows = await WorkerDefinition.filter(name="test-worker", namespace="default").order_by(
        "version"
    )
    assert len(rows) == 2
    assert [r.version for r in rows] == ["1.0.0", "2.0.0"]


@pytest.mark.asyncio
async def test_register_manifest_worker_rejects_legacy_sse_transport():
    """register_manifest rejects worker tool_sources with transport sse."""
    service = RegistryService()
    worker_yaml = """
kind: worker
name: legacy-sse-worker
namespace: default
version: "1.0.0"
provider: mock
model_name: demo
system_prompt: You are helpful.
tool_sources:
  - name: hosted
    transport: sse
    url: http://mcp.example/sse
"""
    with pytest.raises(ValueError, match="transport 'sse' was removed") as exc_info:
        await service.register_manifest(worker_yaml)
    assert "tool_sources.0.transport" in str(exc_info.value)


def test_manifest_validation_error_formats_pydantic_message():
    from common.schemas.worker import MCPServerConfig

    with pytest.raises(ValidationError) as exc_info:
        MCPServerConfig(name="legacy", transport="sse", url="http://mcp.example/sse")

    message = str(manifest_validation_error(exc_info.value))
    assert "transport: transport 'sse' was removed" in message


@pytest.mark.asyncio
async def test_register_manifest_step_creates_step_definition():
    """register_manifest with kind step persists a catalog StepDefinition row."""
    await _create_worker("test-worker")
    service = RegistryService()
    msg = await service.register_manifest(STEP_YAML)

    assert "registered successfully" in msg.lower()
    assert "test-step" in msg

    row = await StepDefinition.get_or_none(name="test-step", namespace="default", version="1.0.0")
    assert row is not None
    assert row.is_active is True
    assert row.body["step_kind"] == "reason"
    assert row.body["worker"] == "test-worker"
    assert row.body["prompt"] == "step1.j2"
    assert row.body["title"] == "First"


@pytest.mark.asyncio
async def test_register_manifest_step_requires_worker():
    """Step registration fails when the pinned worker version is not registered."""
    service = RegistryService()
    with pytest.raises(CatalogDefinitionNotFoundError) as exc_info:
        await service.register_manifest(STEP_YAML)
    message = str(exc_info.value)
    assert "not registered" in message.lower()
    assert "test-worker@1.0.0" in message


@pytest.mark.asyncio
async def test_register_manifest_step_commit_requires_exactly_one_tool():
    """StepBlueprint rejects commit steps that do not have exactly one tool in tools.allow."""
    await _create_worker("commit-step-worker")
    service = RegistryService()
    bad_step = """
kind: step
name: bad-commit-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: commit
worker: commit-step-worker
worker_version: "1.0.0"
tools:
  allow:
    - name: tool_a
    - name: tool_b
timeout_seconds: 600
"""
    with pytest.raises(ValueError, match="exactly one tool"):
        await service.register_manifest(bad_step)


@pytest.mark.asyncio
async def test_register_manifest_saga_requires_registered_steps():
    """register_manifest with kind saga raises when a use: ref has no catalog step."""
    await _create_worker("test-worker")
    service = RegistryService()
    with pytest.raises(CatalogDefinitionNotFoundError) as exc_info:
        await service.register_manifest(SAGA_YAML)
    message = str(exc_info.value)
    assert "not registered" in message.lower()
    assert "test-step@1.0.0" in message


@pytest.mark.asyncio
async def test_register_manifest_saga_requires_workers():
    """Saga registration re-checks that hydrated steps' workers are still registered."""
    worker = await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    await worker.delete()

    with pytest.raises(CatalogDefinitionNotFoundError) as exc_info:
        await service.register_manifest(SAGA_YAML)
    message = str(exc_info.value)
    assert "workers that are not registered" in message
    assert "test-worker@1.0.0" in message


@pytest.mark.asyncio
async def test_register_manifest_parent_rejects_inactive_child_saga():
    """Parent spawn_sagas deploy requires the child definition to be active."""
    await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    child_yaml = """
kind: saga
name: child-inactive-target
namespace: default
version: "1.0.0"
description: Child target
steps:
  - id: c1
    use: test-step
    version: "1.0.0"
    with: {}
"""
    await service.register_manifest(child_yaml)
    child = await SagaDefinition.get(
        name="child-inactive-target", namespace="default", version="1.0.0"
    )
    child.is_active = False
    await child.save()

    parent_yaml = """
kind: saga
name: parent-spawn-inactive
namespace: default
version: "1.0.0"
description: Parent spawn
steps:
  - id: dispatch
    name: Dispatch
    kind: spawn_sagas
    spawn:
      saga_name: child-inactive-target
      saga_version: "1.0.0"
      items_from: "$.input.items"
      item_var: item
      max_children: 4
      result_from: "$.steps.c1.output.data"
      input: {}
  - id: await_children
    name: Await
    kind: join_sagas
    join:
      spawn_step_id: dispatch
"""
    with pytest.raises(InactiveCatalogDefinitionError) as exc_info:
        await service.register_manifest(parent_yaml)
    assert "inactive" in str(exc_info.value).lower()
    assert "child-inactive-target@1.0.0" in str(exc_info.value)
    assert exc_info.value.code == "INACTIVE_CATALOG_DEFINITION"


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_same_version_overwrite():
    """Step versions are append-only; redeploying the same pin fails."""
    await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    with pytest.raises(ValueError, match="already exists and is immutable") as exc_info:
        await service.register_manifest(STEP_YAML)
    message = str(exc_info.value)
    assert "Step capability" in message
    assert "Bump the version" in message


@pytest.mark.asyncio
async def test_register_manifest_saga_stores_use_ref_authoring_body():
    """A thin saga persists use:/with authoring AST — not a hydrated reason/commit body."""
    await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    msg = await service.register_manifest(SAGA_YAML)

    assert "registered successfully" in msg.lower()
    assert "test-saga" in msg

    s = await SagaDefinition.get_or_none(name="test-saga", namespace="default", version="1.0.0")
    assert s is not None
    assert len(s.body["steps"]) == 1
    step0 = s.body["steps"][0]
    assert step0["id"] == "step-1"
    assert step0["use"] == "test-step"
    assert step0["version"] == "1.0.0"
    assert step0.get("kind") in (None, "use")
    assert "worker" not in step0
    assert "prompt" not in step0
    assert "step_definition_name" not in step0


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_unknown_with_key():
    """Saga with keys must be declared inputs on the referenced step definition."""
    await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    saga_yaml = """
kind: saga
name: saga-unknown-with
namespace: default
version: "1.0.0"
description: Binds a port the step does not declare
steps:
  - id: step-1
    use: test-step
    version: "1.0.0"
    with:
      nope:
        value: hi
"""
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest(saga_yaml)
    message = str(exc_info.value)
    assert "not declared inputs" in message
    assert "nope" in message


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_missing_required_input():
    """Saga registration fails when a required input port has no with binding."""
    await _create_worker("input-worker")
    service = RegistryService()
    step_yaml = """
kind: step
name: input-step
namespace: default
version: "1.0.0"
title: Needs owner
inputs:
  owner:
    required: true
step_kind: reason
worker: input-worker
worker_version: "1.0.0"
prompt: p.j2
timeout_seconds: 600
"""
    await service.register_manifest(step_yaml)
    saga_yaml = """
kind: saga
name: saga-missing-input
namespace: default
version: "1.0.0"
description: Leaves a required port unbound
steps:
  - id: step-1
    use: input-step
    version: "1.0.0"
    with: {}
"""
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest(saga_yaml)
    message = str(exc_info.value)
    assert "missing required inputs" in message
    assert "owner" in message


@pytest.mark.asyncio
async def test_register_manifest_saga_accepts_tightened_hitl():
    """A saga may turn hitl on for a catalog step that does not require it."""
    await _create_worker("test-worker")
    service = RegistryService()
    await service.register_manifest(STEP_YAML)
    saga_yaml = """
kind: saga
name: saga-tighten-hitl
namespace: default
version: "1.0.0"
description: Tightens the catalog guardrail
steps:
  - id: step-1
    use: test-step
    version: "1.0.0"
    with: {}
    hitl: true
"""
    msg = await service.register_manifest(saga_yaml)
    assert "registered successfully" in msg.lower()

    s = await SagaDefinition.get_or_none(
        name="saga-tighten-hitl", namespace="default", version="1.0.0"
    )
    assert s is not None
    assert s.body["steps"][0].get("hitl") is True
    assert s.body["steps"][0]["use"] == "test-step"


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_widening_hitl():
    """A saga may not clear hitl that the catalog step requires."""
    await _create_worker("hitl-worker")
    service = RegistryService()
    step_yaml = """
kind: step
name: hitl-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: hitl-worker
worker_version: "1.0.0"
prompt: p.j2
hitl: true
timeout_seconds: 600
"""
    await service.register_manifest(step_yaml)
    saga_yaml = """
kind: saga
name: saga-widen-hitl
namespace: default
version: "1.0.0"
description: Tries to widen the catalog guardrail
steps:
  - id: step-1
    use: hitl-step
    version: "1.0.0"
    with: {}
    hitl: false
"""
    with pytest.raises(ValueError, match="cannot clear catalog hitl"):
        await service.register_manifest(saga_yaml)


@pytest.mark.asyncio
async def test_register_manifest_saga_validates_output_schema_and_compensation(
    monkeypatch, tmp_path
):
    """Catalog output_schema (JSON) and compensation (YAML) survive expansion into the saga."""
    (tmp_path / "out.json").write_text(
        json.dumps({"type": "object", "properties": {"a": {"type": "string"}}}),
        encoding="utf-8",
    )
    comp_body = {
        "worker": "schema-test-worker",
        "worker_version": "1.0.0",
        "with": {},
        "tools": {"allow": [{"name": "noop_tool"}]},
    }
    compensations_root = tmp_path / "compensations"
    compensations_root.mkdir()
    (compensations_root / "undo.yaml").write_text(yaml.dump(comp_body), encoding="utf-8")
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    monkeypatch.setenv("COMPENSATIONS_ROOT", str(compensations_root))
    get_settings.cache_clear()
    try:
        await _create_worker("schema-test-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: schema-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: schema-test-worker
worker_version: "1.0.0"
prompt: p.j2
output_schema: out.json
compensation: undo.yaml
timeout_seconds: 600
"""
        await service.register_manifest(step_yaml)
        saga_yaml = """
kind: saga
name: saga-with-schema-ref
namespace: default
version: "1.0.0"
description: Has output schema ref
steps:
  - id: s1
    use: schema-step
    version: "1.0.0"
    with: {}
"""
        msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()

        s = await SagaDefinition.get_or_none(
            name="saga-with-schema-ref", namespace="default", version="1.0.0"
        )
        assert s is not None
        step0 = s.body["steps"][0]
        assert step0["use"] == "schema-step"
        assert "compensation_definition" not in step0
        step_row = await StepDefinition.get(
            name="schema-step", namespace="default", version="1.0.0"
        )
        assert step_row.body.get("output_schema") == "out.json"
        assert step_row.body.get("compensation") == "undo.yaml"
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        monkeypatch.delenv("COMPENSATIONS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_missing_prompt_file(monkeypatch, tmp_path):
    """Step registration fails when a reason step's prompt file is missing under PROMPTS_ROOT."""
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts_root))
    get_settings.cache_clear()
    try:
        await _create_worker("prompt-test-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-bad-prompt
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: prompt-test-worker
worker_version: "1.0.0"
prompt: missing.j2
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        err = str(exc_info.value).lower()
        assert "prompt" in err
        assert "not found" in err or "invalid" in err
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_prompt_without_prompts_root(monkeypatch):
    """Step registration fails when PROMPTS_ROOT is unset but a reason step has prompt."""
    monkeypatch.delenv("PROMPTS_ROOT", raising=False)
    get_settings.cache_clear()
    try:
        await _create_worker("no-prompts-root-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-no-prompts-root
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: no-prompts-root-worker
worker_version: "1.0.0"
prompt: p.j2
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        assert "prompts_root" in str(exc_info.value).lower()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_prompt_variable_not_in_inputs(monkeypatch, tmp_path):
    """Prompt Jinja variables are validated against the step's declared input ports."""
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()
    (prompts_root / "needs_name.j2").write_text("Hello {{ name }}", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts_root))
    get_settings.cache_clear()
    try:
        await _create_worker("prompt-var-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-bad-prompt-vars
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: prompt-var-worker
worker_version: "1.0.0"
prompt: needs_name.j2
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        assert "name" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_accepts_prompt_variable_declared_as_input(
    monkeypatch, tmp_path
):
    """Declaring the port the prompt needs makes the same step manifest valid."""
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()
    (prompts_root / "needs_name.j2").write_text("Hello {{ name }}", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts_root))
    get_settings.cache_clear()
    try:
        await _create_worker("prompt-var-ok-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-good-prompt-vars
namespace: default
version: "1.0.0"
inputs:
  name:
    required: true
step_kind: reason
worker: prompt-var-ok-worker
worker_version: "1.0.0"
prompt: needs_name.j2
timeout_seconds: 600
"""
        msg = await service.register_manifest(step_yaml)
        assert "registered successfully" in msg.lower()

        saga_yaml = """
kind: saga
name: saga-good-prompt-vars
namespace: default
version: "1.0.0"
description: Binds the declared port
steps:
  - id: s1
    use: step-good-prompt-vars
    version: "1.0.0"
    with:
      name:
        value: Ada
"""
        msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_missing_output_schema_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        await _create_worker("schema-test-worker-2")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-bad-schema
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: schema-test-worker-2
worker_version: "1.0.0"
prompt: p.j2
output_schema: schemas/does-not-exist.json
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        assert "output_schema" in str(exc_info.value) or "not found" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        monkeypatch.delenv("COMPENSATIONS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_unsupported_output_schema_keywords(
    monkeypatch, tmp_path
):
    (tmp_path / "bad.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "event": {
                        "type": "object",
                        "anyOf": [{"type": "object"}, {"type": "null"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        await _create_worker("schema-test-worker-unsupported")
        service = RegistryService()
        step_yaml = """
kind: step
name: step-unsupported-schema
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: schema-test-worker-unsupported
worker_version: "1.0.0"
prompt: p.j2
output_schema: bad.json
timeout_seconds: 600
"""
        with pytest.raises(ValueError, match="unsupported keyword"):
            await service.register_manifest(step_yaml)
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_resources_allow_persists_on_step_definition():
    """Catalog resources.allow entries are stored on the step definition, not the saga AST."""
    await _create_worker("resource-worker")
    service = RegistryService()
    step_yaml = """
kind: step
name: resource-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: resource-worker
worker_version: "1.0.0"
prompt: p.j2
resources:
  allow:
    - uri: "file:///policies/fraud-v3.md"
    - uri: "postgres://risk/profiles/{customer_id}"
timeout_seconds: 600
"""
    await service.register_manifest(step_yaml)
    saga_yaml = """
kind: saga
name: saga-with-resources
namespace: default
version: "1.0.0"
description: Has resources
steps:
  - id: s1
    use: resource-step
    version: "1.0.0"
    with: {}
"""
    msg = await service.register_manifest(saga_yaml)
    assert "registered successfully" in msg.lower()

    step_row = await StepDefinition.get(name="resource-step", namespace="default", version="1.0.0")
    uris = [r["uri"] for r in step_row.body["resources"]["allow"]]
    assert uris == ["file:///policies/fraud-v3.md", "postgres://risk/profiles/{customer_id}"]

    s = await SagaDefinition.get_or_none(
        name="saga-with-resources", namespace="default", version="1.0.0"
    )
    assert s is not None
    assert "resources" not in s.body["steps"][0]


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_resource_without_uri():
    """Step registration fails when a resources.allow entry omits the required uri."""
    await _create_worker("bad-resource-worker")
    service = RegistryService()
    step_yaml = """
kind: step
name: bad-resource-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: reason
worker: bad-resource-worker
worker_version: "1.0.0"
prompt: p.j2
resources:
  allow:
    - description: "missing uri field"
timeout_seconds: 600
"""
    with pytest.raises(ValueError):
        await service.register_manifest(step_yaml)


@pytest.mark.asyncio
async def test_register_manifest_step_validates_policy_at_deploy(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    (policies_root / "gate.yaml").write_text('cel: "true"\n', encoding="utf-8")
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await _create_worker("policy-test-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: policy-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: commit
worker: policy-test-worker
worker_version: "1.0.0"
policy: gate.yaml
tools:
  allow:
    - name: noop_tool
timeout_seconds: 600
"""
        msg = await service.register_manifest(step_yaml)
        assert "registered successfully" in msg.lower()

        saga_yaml = """
kind: saga
name: saga-with-policy
namespace: default
version: "1.0.0"
description: Has policy
steps:
  - id: s1
    use: policy-step
    version: "1.0.0"
    with: {}
"""
        msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()

        s = await SagaDefinition.get_or_none(
            name="saga-with-policy", namespace="default", version="1.0.0"
        )
        assert s is not None
        assert s.body["steps"][0]["use"] == "policy-step"
        step_row = await StepDefinition.get(
            name="policy-step", namespace="default", version="1.0.0"
        )
        assert step_row.body["policy"] == "gate.yaml"
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_missing_policy(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await _create_worker("policy-miss-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: policy-miss-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: commit
worker: policy-miss-worker
worker_version: "1.0.0"
policy: missing.yaml
tools:
  allow:
    - name: noop_tool
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        assert "policy" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_step_rejects_invalid_policy_cel(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    (policies_root / "bad.yaml").write_text('cel: "@@@not valid@@@"\n', encoding="utf-8")
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await _create_worker("policy-bad-cel-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: policy-bad-cel-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: commit
worker: policy-bad-cel-worker
worker_version: "1.0.0"
policy: bad.yaml
tools:
  allow:
    - name: noop_tool
timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(step_yaml)
        assert "policy" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_legacy_policy_warns_once_per_deploy(monkeypatch, tmp_path, caplog):
    import logging

    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    (policies_root / "legacy-check.yaml").write_text('cel: "true"\n', encoding="utf-8")
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await _create_worker("legacy-policy-worker")
        service = RegistryService()
        step_yaml = """
kind: step
name: legacy-policy-step
namespace: default
version: "1.0.0"
inputs: {}
step_kind: commit
worker: legacy-policy-worker
worker_version: "1.0.0"
policy: legacy-check
tools:
  allow:
    - name: noop_tool
timeout_seconds: 600
"""
        await service.register_manifest(step_yaml)
        steps = "\n".join(
            f"""  - id: s{i}
    use: legacy-policy-step
    version: "1.0.0"
    with: {{}}"""
            for i in range(5)
        )
        saga_yaml = f"""
kind: saga
name: saga-legacy-policy-multi
namespace: default
version: "1.0.0"
description: Same legacy policy on five steps
steps:
{steps}
"""
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()
        legacy_warnings = [r for r in caplog.records if "legacy .yaml suffix" in r.message]
        assert len(legacy_warnings) == 1
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()
