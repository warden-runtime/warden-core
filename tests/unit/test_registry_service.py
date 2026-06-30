"""Unit tests for engine.registry.service.RegistryService."""

import json

import pytest
import yaml
from common.config import get_settings
from common.models import SagaDefinition, WorkerDefinition
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

SAGA_YAML = """
kind: saga
name: test-saga
namespace: default
version: "1.0.0"
description: A test saga
steps:
  - id: step-1
    kind: reason
    name: First
    worker: test-worker
    worker_version: "1.0.0"
    with: {}
    prompt: step1.j2
    timeout_seconds: 600
"""


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
    """register_manifest raises ValueError when kind is not worker or saga."""
    service = RegistryService()
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest("kind: pipeline\nname: p1")
    assert "unknown" in str(exc_info.value).lower() or "kind" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_register_manifest_worker_creates_or_updates():
    """register_manifest with kind worker creates or updates WorkerDefinition."""
    service = RegistryService()
    msg = await service.register_manifest(WORKER_YAML)

    assert "registered successfully" in msg.lower()
    assert "test-worker" in msg

    w = await WorkerDefinition.get_or_none(name="test-worker", namespace="default", version="1.0.0")
    assert w is not None
    assert w.model_provider == "openai"
    assert w.model_name == "gpt-4o"
    assert w.version == "1.0.0"
    assert w.system_prompt == "You are helpful."
    assert w.adapter == "langchain"


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
async def test_register_manifest_saga_requires_workers():
    """register_manifest with kind saga raises when required workers not registered."""
    service = RegistryService()
    saga_missing_worker = """
kind: saga
name: need-worker
namespace: default
version: "1.0.0"
description: Needs worker
steps:
  - id: s1
    kind: reason
    name: S1
    worker: unregistered-worker
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    timeout_seconds: 600
"""
    with pytest.raises(ValueError) as exc_info:
        await service.register_manifest(saga_missing_worker)
    assert "not registered" in str(exc_info.value).lower() or "unregistered-worker" in str(
        exc_info.value
    )


@pytest.mark.asyncio
async def test_register_manifest_saga_creates_when_workers_exist():
    """register_manifest with kind saga creates SagaDefinition when workers are registered."""
    await WorkerDefinition.create(
        namespace="default",
        name="test-worker",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    service = RegistryService()
    msg = await service.register_manifest(SAGA_YAML)

    assert "registered successfully" in msg.lower()
    assert "test-saga" in msg

    s = await SagaDefinition.get_or_none(name="test-saga", namespace="default", version="1.0.0")
    assert s is not None
    assert "steps" in s.body
    assert len(s.body["steps"]) == 1
    assert s.body["steps"][0]["worker"] == "test-worker"


@pytest.mark.asyncio
async def test_register_manifest_commit_step_requires_exactly_one_tool():
    """SagaBlueprint rejects commit steps that do not have exactly one tool in tools.allow."""
    await WorkerDefinition.create(
        namespace="default",
        name="commit-saga-worker",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    service = RegistryService()
    bad_saga = """
kind: saga
name: bad-commit
namespace: default
version: "1.0.0"
description: Invalid commit
steps:
  - id: c1
    kind: commit
    name: Bad
    worker: commit-saga-worker
    worker_version: "1.0.0"
    with: {}
    tools:
      allow:
        - name: tool_a
        - name: tool_b
    timeout_seconds: 600
"""
    with pytest.raises(ValidationError):
        await service.register_manifest(bad_saga)


@pytest.mark.asyncio
async def test_register_manifest_saga_validates_output_schema_and_compensation(
    monkeypatch, tmp_path
):
    """Saga registration resolves output_schema (JSON) and compensation (YAML) under their roots."""
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
        await WorkerDefinition.create(
            namespace="default",
            name="schema-test-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-with-schema-ref
namespace: default
version: "1.0.0"
description: Has output schema ref
steps:
  - id: s1
    kind: reason
    name: S1
    worker: schema-test-worker
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    output_schema: out.json
    compensation: undo.yaml
    timeout_seconds: 600
"""
        msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()

        s = await SagaDefinition.get_or_none(
            name="saga-with-schema-ref", namespace="default", version="1.0.0"
        )
        assert s is not None
        step0 = s.body["steps"][0]
        assert step0.get("output_schema") == "out.json"
        assert step0.get("compensation") == "undo.yaml"
        embedded = step0.get("compensation_definition")
        assert isinstance(embedded, dict)
        assert embedded.get("worker") == "schema-test-worker"
        assert embedded["tools"]["allow"][0]["name"] == "noop_tool"
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        monkeypatch.delenv("COMPENSATIONS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_missing_prompt_file(monkeypatch, tmp_path):
    """Saga registration fails when a reason-step prompt file is missing under PROMPTS_ROOT."""
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts_root))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="prompt-test-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-bad-prompt
namespace: default
version: "1.0.0"
description: Missing prompt file
steps:
  - id: s1
    kind: reason
    name: S1
    worker: prompt-test-worker
    worker_version: "1.0.0"
    with: {}
    prompt: missing.j2
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
        err = str(exc_info.value).lower()
        assert "prompt" in err
        assert "not found" in err or "invalid" in err
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_prompt_without_prompts_root(monkeypatch):
    """Saga registration fails when PROMPTS_ROOT is unset but a reason step has prompt."""
    monkeypatch.delenv("PROMPTS_ROOT", raising=False)
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="no-prompts-root-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-no-prompts-root
namespace: default
version: "1.0.0"
description: PROMPTS_ROOT required
steps:
  - id: s1
    kind: reason
    name: S1
    worker: no-prompts-root-worker
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
        assert "prompts_root" in str(exc_info.value).lower() or "prompts_root" in str(
            exc_info.value
        )
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_prompt_variable_not_in_with(monkeypatch, tmp_path):
    """Saga registration validates Jinja variables against the step with map."""
    prompts_root = tmp_path / "prompts"
    prompts_root.mkdir()
    (prompts_root / "needs_name.j2").write_text("Hello {{ name }}", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts_root))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="prompt-var-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-bad-prompt-vars
namespace: default
version: "1.0.0"
description: Prompt var missing from with
steps:
  - id: s1
    kind: reason
    name: S1
    worker: prompt-var-worker
    worker_version: "1.0.0"
    with: {}
    prompt: needs_name.j2
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
        assert "name" in str(exc_info.value).lower() or "with" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_missing_output_schema_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEMAS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="schema-test-worker-2",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-bad-schema
namespace: default
version: "1.0.0"
description: Missing file
steps:
  - id: s1
    kind: reason
    name: S1
    worker: schema-test-worker-2
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    output_schema: schemas/does-not-exist.json
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
        assert "output_schema" in str(exc_info.value) or "not found" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("SCHEMAS_ROOT", raising=False)
        monkeypatch.delenv("COMPENSATIONS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_accepts_step_resources_allow():
    """Saga registration accepts step-level resources.allow entries."""
    await WorkerDefinition.create(
        namespace="default",
        name="resource-worker",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    service = RegistryService()
    saga_yaml = """
kind: saga
name: saga-with-resources
namespace: default
version: "1.0.0"
description: Has resources
steps:
  - id: s1
    kind: reason
    name: S1
    worker: resource-worker
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    resources:
      allow:
        - uri: "file:///policies/fraud-v3.md"
        - uri: "postgres://risk/profiles/{customer_id}"
    timeout_seconds: 600
"""
    msg = await service.register_manifest(saga_yaml)
    assert "registered successfully" in msg.lower()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_resource_without_uri():
    """Saga registration fails when resources.allow entry omits required uri."""
    await WorkerDefinition.create(
        namespace="default",
        name="bad-resource-worker",
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi.",
    )
    service = RegistryService()
    saga_yaml = """
kind: saga
name: saga-bad-resources
namespace: default
version: "1.0.0"
description: Bad resources
steps:
  - id: s1
    kind: reason
    name: S1
    worker: bad-resource-worker
    worker_version: "1.0.0"
    with: {}
    prompt: p.j2
    resources:
      allow:
        - description: "missing uri field"
    timeout_seconds: 600
"""
    with pytest.raises(ValidationError):
        await service.register_manifest(saga_yaml)


@pytest.mark.asyncio
async def test_register_manifest_saga_validates_policy_at_deploy(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    (policies_root / "gate.yaml").write_text('cel: "true"\n', encoding="utf-8")
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="policy-test-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-with-policy
namespace: default
version: "1.0.0"
description: Has policy
steps:
  - id: s1
    kind: commit
    name: S1
    worker: policy-test-worker
    worker_version: "1.0.0"
    policy: gate.yaml
    with: {}
    tools:
      allow:
        - name: noop_tool
    timeout_seconds: 600
"""
        msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_missing_policy(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="policy-miss-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-bad-policy
namespace: default
version: "1.0.0"
description: Missing policy
steps:
  - id: s1
    kind: commit
    name: S1
    worker: policy-miss-worker
    worker_version: "1.0.0"
    policy: missing.yaml
    with: {}
    tools:
      allow:
        - name: noop_tool
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
        assert "policy" in str(exc_info.value).lower()
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_manifest_saga_rejects_invalid_policy_cel(monkeypatch, tmp_path):
    policies_root = tmp_path / "policies"
    policies_root.mkdir()
    (policies_root / "bad.yaml").write_text('cel: "@@@not valid@@@"\n', encoding="utf-8")
    monkeypatch.setenv("POLICIES_ROOT", str(policies_root))
    get_settings.cache_clear()
    try:
        await WorkerDefinition.create(
            namespace="default",
            name="policy-bad-cel-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        saga_yaml = """
kind: saga
name: saga-bad-policy-cel
namespace: default
version: "1.0.0"
description: Bad CEL
steps:
  - id: s1
    kind: commit
    name: S1
    worker: policy-bad-cel-worker
    worker_version: "1.0.0"
    policy: bad.yaml
    with: {}
    tools:
      allow:
        - name: noop_tool
    timeout_seconds: 600
"""
        with pytest.raises(ValueError) as exc_info:
            await service.register_manifest(saga_yaml)
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
        await WorkerDefinition.create(
            namespace="default",
            name="legacy-policy-worker",
            model_provider="openai",
            model_name="gpt-4o",
            system_prompt="Hi.",
        )
        service = RegistryService()
        steps = "\n".join(
            f"""  - id: s{i}
    kind: commit
    name: S{i}
    worker: legacy-policy-worker
    worker_version: "1.0.0"
    policy: legacy-check
    with: {{}}
    tools:
      allow:
        - name: noop_tool
    timeout_seconds: 600"""
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
        with caplog.at_level(logging.WARNING):
            msg = await service.register_manifest(saga_yaml)
        assert "registered successfully" in msg.lower()
        legacy_warnings = [r for r in caplog.records if "legacy .yaml suffix" in r.message]
        assert len(legacy_warnings) == 1
    finally:
        monkeypatch.delenv("POLICIES_ROOT", raising=False)
        get_settings.cache_clear()
