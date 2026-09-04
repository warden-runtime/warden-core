"""Unit tests for prompt file includes under PROMPTS_ROOT."""

from pathlib import Path

import pytest
from common.prompts import render_prompt_file
from workers.utils import resolve_input, resolve_step_prompt


def test_render_prompt_file_includes_sibling_partial(tmp_path: Path):
    partials = tmp_path / "partials"
    partials.mkdir()
    (partials / "profile.j2").write_text("Profile: {{ name }}\n", encoding="utf-8")
    (tmp_path / "main.j2").write_text(
        "{% include 'partials/profile.j2' %}Task: {{ task }}\n",
        encoding="utf-8",
    )
    rendered = render_prompt_file(str(tmp_path), "main.j2", {"name": "Ada", "task": "review"})
    assert "Profile: Ada" in rendered
    assert "Task: review" in rendered


def test_render_prompt_file_rejects_escape_include(tmp_path: Path):
    (tmp_path / "main.j2").write_text("{% include '../secret.j2' %}\n", encoding="utf-8")
    outside = tmp_path.parent / "secret.j2"
    outside.write_text("LEAK\n", encoding="utf-8")
    with pytest.raises(ValueError, match="include|escapes|not found"):
        render_prompt_file(str(tmp_path), "main.j2", {})


def test_resolve_input_string_templates_unchanged():
    assert resolve_input("Hello {{ name }}", {"name": "Ada"}) == "Hello Ada"


def test_resolve_step_prompt_uses_file_loader_when_prompt_ref_set(tmp_path: Path, monkeypatch):
    (tmp_path / "step.j2").write_text("Hi {{ who }}\n", encoding="utf-8")
    monkeypatch.setenv("PROMPTS_ROOT", str(tmp_path))
    from common.config import get_settings

    get_settings.cache_clear()
    try:
        rendered = resolve_step_prompt(
            prompt_template="ignored body",
            template_context={"who": "Bob"},
            context={"prompt_ref": "step.j2"},
        )
        assert "Hi Bob" in rendered
    finally:
        get_settings.cache_clear()


def test_resolve_step_prompt_falls_back_to_resolve_input_without_ref():
    rendered = resolve_step_prompt(
        prompt_template="Claim {{ claim_id }}",
        template_context={"claim_id": "c-1"},
        context={},
    )
    assert rendered == "Claim c-1"


def test_render_prompt_file_rejects_attr_escape_ssti(tmp_path: Path):
    (tmp_path / "evil.j2").write_text(
        "{{ ''.__class__.__mro__[1].__subclasses__() }}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsafe|blocked"):
        render_prompt_file(str(tmp_path), "evil.j2", {})


def test_resolve_input_rejects_attr_escape_ssti():
    with pytest.raises(ValueError, match="unsafe|blocked"):
        resolve_input("{{ ''.__class__.__mro__ }}", {})


def test_prompt_environment_strips_introspection_globals():
    from common.prompts import make_prompt_environment

    env = make_prompt_environment()
    for key in ("cycler", "joiner", "namespace", "lipsum"):
        assert key not in env.globals
    # Undefined helpers render empty rather than exposing object graphs.
    assert env.from_string("{{ cycler }}").render() == ""
