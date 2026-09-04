"""Freeze-at-start for prompts: includes + post-start disk edits."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from common.models import SagaInstance, SagaStepInstance
from common.prompts import freeze_prompt_definition
from engine.api.saga_start import start_saga
from engine.logic import _require_frozen_skills_definition
from engine.registry.service import RegistryService
from workers.utils import resolve_step_prompt

if TYPE_CHECKING:
    from pathlib import Path


def test_freeze_prompt_definition_inlines_static_include(tmp_path: Path) -> None:
    partials = tmp_path / "partials"
    partials.mkdir()
    (partials / "profile.j2").write_text("Profile: {{ name }}\n", encoding="utf-8")
    (tmp_path / "main.j2").write_text(
        "{% include 'partials/profile.j2' %}Task: {{ task }}\n",
        encoding="utf-8",
    )
    frozen = freeze_prompt_definition(str(tmp_path), "main.j2")
    assert "{% include" not in frozen
    assert "Profile: {{ name }}" in frozen
    assert "Task: {{ task }}" in frozen
    rendered = resolve_step_prompt(
        prompt_template=frozen,
        template_context={"name": "Ada", "task": "review"},
    )
    assert "Profile: Ada" in rendered
    assert "Task: review" in rendered


def test_freeze_prompt_definition_leaves_comment_and_raw_includes(tmp_path: Path) -> None:
    (tmp_path / "inner.j2").write_text("INNER", encoding="utf-8")
    (tmp_path / "main.j2").write_text(
        "{# {% include 'inner.j2' %} #}\n{% raw %}{% include 'inner.j2' %}{% endraw %}\nOuter\n",
        encoding="utf-8",
    )
    frozen = freeze_prompt_definition(str(tmp_path), "main.j2")
    assert "{# {% include 'inner.j2' %} #}" in frozen
    assert "{% raw %}{% include 'inner.j2' %}{% endraw %}" in frozen
    assert "INNER" not in frozen
    assert "Outer" in frozen


@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("{% include other %}\n", "dynamic"),
        ("{% include 'x.j2' ignore missing %}\n", "ignore missing|modifier"),
        ("{% include 'x.j2' with context %}\n", "with context|modifier"),
        ("{% include 'x.j2' without context %}\n", "without context|modifier"),
    ],
)
def test_freeze_prompt_definition_rejects_unsupported_includes(
    tmp_path: Path, template: str, match: str
) -> None:
    (tmp_path / "x.j2").write_text("x\n", encoding="utf-8")
    (tmp_path / "main.j2").write_text(template, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        freeze_prompt_definition(str(tmp_path), "main.j2")


def test_freeze_prompt_definition_rejects_cycle(tmp_path: Path) -> None:
    (tmp_path / "a.j2").write_text("{% include 'b.j2' %}\n", encoding="utf-8")
    (tmp_path / "b.j2").write_text("{% include 'a.j2' %}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        freeze_prompt_definition(str(tmp_path), "a.j2")


def test_resolve_step_prompt_uses_frozen_template_not_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "step.j2").write_text("FROM_DISK {{ who }}\n", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(tmp_path))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        rendered = resolve_step_prompt(
            prompt_template="FROZEN {{ who }}",
            template_context={"who": "Bob"},
        )
        assert rendered == "FROZEN Bob"
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


def test_require_frozen_skills_definition_requires_coverage() -> None:
    step = SimpleNamespace(span_id="abcd1234", skills_definition=[])
    with pytest.raises(ValueError, match="missing embeds"):
        _require_frozen_skills_definition(step, [{"name": "triage"}])

    step.skills_definition = [
        {"name": "triage", "body": "", "allowed_tools": []},
    ]
    with pytest.raises(ValueError, match="incomplete embeds"):
        _require_frozen_skills_definition(step, [{"name": "triage"}])

    step.skills_definition = [
        {"name": "triage", "body": "ok", "allowed_tools": "github.get_issue"},
    ]
    with pytest.raises(ValueError, match="incomplete embeds"):
        _require_frozen_skills_definition(step, [{"name": "triage"}])

    step.skills_definition = [
        {"name": "triage", "body": "ok\n", "allowed_tools": []},
    ]
    _require_frozen_skills_definition(step, [{"name": "triage"}])


WORKER_YAML = """
kind: worker
name: freeze-prompt-worker
namespace: default
version: "1.0.0"
provider: mock
model_name: demo
system_prompt: You are helpful.
tool_sources: []
adapter: langchain
"""


@pytest.mark.asyncio
async def test_start_saga_freezes_prompt_and_ignores_later_disk_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "greet.j2").write_text("Hello {{ name }} from v1\n", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        service = RegistryService()
        await service.register_manifest(WORKER_YAML)
        step_yaml = """
kind: step
name: freeze-prompt-step
namespace: default
version: "1.0.0"
title: Greet
inputs:
  name:
    required: true
step_kind: reason
worker: freeze-prompt-worker
worker_version: "1.0.0"
prompt: greet.j2
tools:
  allow: []
timeout_seconds: 300
"""
        await service.register_manifest(step_yaml)
        saga_yaml = """
kind: saga
name: freeze-prompt-saga
namespace: default
version: "1.0.0"
description: Prompt freeze
steps:
  - id: greet
    use: freeze-prompt-step
    version: "1.0.0"
    with:
      name:
        from: "$.input.name"
"""
        await service.register_manifest(saga_yaml)
        result = await start_saga(
            namespace="default",
            name="freeze-prompt-saga",
            version="1.0.0",
            input={"name": "Ada"},
        )
        saga = await SagaInstance.get(trace_id=result.trace_id)
        frozen = saga.frozen_steps[0]
        assert frozen["prompt"] == "greet.j2"
        assert frozen["prompt_definition"] == "Hello {{ name }} from v1\n"

        step = await SagaStepInstance.get(saga_trace_id=result.trace_id, step_id="greet")
        assert step.prompt_definition == "Hello {{ name }} from v1\n"

        (prompts / "greet.j2").write_text("Hello {{ name }} from v2 EDITED\n", encoding="utf-8")
        (prompts / "greet.j2").unlink()

        rendered = resolve_step_prompt(
            prompt_template=step.prompt_definition or "",
            template_context={"name": "Ada"},
        )
        assert "from v1" in rendered
        assert "EDITED" not in rendered
        assert "v2" not in rendered
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_start_saga_freezes_skills_and_ignores_later_disk_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "greet.j2").write_text("Hello\n", encoding="utf-8")
    skills = tmp_path / "skills"
    worker_skills = skills / "freeze-prompt-worker"
    worker_skills.mkdir(parents=True)
    skill_path = worker_skills / "triage.md"
    skill_path.write_text(
        """---
name: triage
description: Classify.
allowed_tools: []
---
Skill body v1
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROMPTS_ROOT", str(prompts))
    monkeypatch.setenv("SKILLS_ROOT", str(skills))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        service = RegistryService()
        await service.register_manifest(WORKER_YAML)
        step_yaml = """
kind: step
name: freeze-skills-step
namespace: default
version: "1.0.0"
title: Greet
inputs: {}
step_kind: reason
worker: freeze-prompt-worker
worker_version: "1.0.0"
prompt: greet.j2
skills:
  allow:
    - name: triage
tools:
  allow: []
timeout_seconds: 300
"""
        await service.register_manifest(step_yaml)
        saga_yaml = """
kind: saga
name: freeze-skills-saga
namespace: default
version: "1.0.0"
description: Skills freeze
steps:
  - id: greet
    use: freeze-skills-step
    version: "1.0.0"
    with: {}
"""
        await service.register_manifest(saga_yaml)
        result = await start_saga(
            namespace="default",
            name="freeze-skills-saga",
            version="1.0.0",
            input={},
        )
        saga = await SagaInstance.get(trace_id=result.trace_id)
        frozen = saga.frozen_steps[0]
        assert frozen["skills_definition"][0]["body"] == "Skill body v1\n"

        step = await SagaStepInstance.get(saga_trace_id=result.trace_id, step_id="greet")
        assert step.skills_definition[0]["body"] == "Skill body v1\n"

        skill_path.write_text(
            """---
name: triage
description: Classify.
allowed_tools: []
---
Skill body v2 EDITED
""",
            encoding="utf-8",
        )
        skill_path.unlink()

        from workers.adapters.langchain import _resolve_skills_for_step

        _, index = _resolve_skills_for_step(
            skill_specs=[{"name": "triage"}],
            extras=[],
            skills_definition=step.skills_definition,
        )
        assert index == [{"name": "triage", "description": "Classify."}]
        assert step.skills_definition[0]["body"] == "Skill body v1\n"
        assert "EDITED" not in step.skills_definition[0]["body"]
    finally:
        monkeypatch.delenv("PROMPTS_ROOT", raising=False)
        monkeypatch.delenv("SKILLS_ROOT", raising=False)
        get_settings.cache_clear()


def test_assert_skill_document_complete_rejects_empty_body(tmp_path: Path) -> None:
    from common.skills import SkillLoadError, assert_skill_document_complete, load_skill_document

    worker = tmp_path / "demo-worker"
    worker.mkdir()
    (worker / "empty.md").write_text(
        """---
name: empty
description: No body.
allowed_tools: []
---
""",
        encoding="utf-8",
    )
    doc = load_skill_document(str(tmp_path), "demo-worker", "empty")
    with pytest.raises(SkillLoadError, match="cannot be frozen"):
        assert_skill_document_complete(doc, skill_id="empty")
