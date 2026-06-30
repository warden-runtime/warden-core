"""Unit tests for engine.utils (resolve_parameters_spec, load_prompt_content)."""

import tempfile
from pathlib import Path

import pytest
from engine.utils import (
    load_prompt_content,
    resolve_parameters_spec,
    resolve_prompts_root,
    validate_prompt_variables,
)


class TestResolveParametersSpec:
    """Tests for resolve_parameters_spec."""

    def test_from_jsonpath(self):
        context = {"input": {"amount": 100, "currency": "USD"}, "steps": {}}
        spec = {
            "amount": {"from": "$.input.amount"},
            "currency": {"from": "$.input.currency"},
        }
        result = resolve_parameters_spec(spec, context)
        assert result == {"amount": 100, "currency": "USD"}

    def test_value_literal(self):
        spec = {
            "action": {"value": "refund"},
            "flag": {"value": True},
        }
        result = resolve_parameters_spec(spec, {})
        assert result == {"action": "refund", "flag": True}

    def test_mixed_from_and_value(self):
        context = {"input": {"user_id": "u-1"}, "steps": {}}
        spec = {
            "user_id": {"from": "$.input.user_id"},
            "action": {"value": "refund"},
        }
        result = resolve_parameters_spec(spec, context)
        assert result == {"user_id": "u-1", "action": "refund"}

    def test_missing_path_returns_none(self):
        spec = {"missing": {"from": "$.input.nonexistent"}}
        result = resolve_parameters_spec(spec, {"input": {}})
        assert result == {"missing": None}

    def test_empty_spec(self):
        assert resolve_parameters_spec({}, {"input": {}}) == {}


class TestResolvePromptsRoot:
    """Tests for resolve_prompts_root."""

    def test_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert resolve_prompts_root(tmp) == str(Path(tmp).resolve())

    def test_rejects_empty(self):
        with pytest.raises(ValueError) as exc_info:
            resolve_prompts_root(None)
        assert "PROMPTS_ROOT" in str(exc_info.value)

    def test_rejects_non_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "notadir"
            file_path.write_text("x", encoding="utf-8")
            with pytest.raises(ValueError) as exc_info:
                resolve_prompts_root(str(file_path))
            assert "not a directory" in str(exc_info.value).lower()


class TestLoadPromptContent:
    """Tests for load_prompt_content."""

    def test_load_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.j2"
            path.write_text("Hello {{ name }}", encoding="utf-8")
            content = load_prompt_content(tmp, "prompt.j2")
            assert content == "Hello {{ name }}"

    def test_rejects_empty_prompts_root(self):
        with pytest.raises(ValueError) as exc_info:
            load_prompt_content("", "prompt.j2")
        assert "PROMPTS_ROOT" in str(exc_info.value)

    def test_rejects_empty_prompt_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError) as exc_info:
                load_prompt_content(tmp, "")
            assert (
                "prompt_ref" in str(exc_info.value).lower()
                or "non-empty" in str(exc_info.value).lower()
            )

    def test_rejects_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError) as exc_info:
                load_prompt_content(tmp, "nonexistent.j2")
            assert "not found" in str(exc_info.value).lower()

    def test_rejects_path_escaping_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError) as exc_info:
                load_prompt_content(tmp, "../../../etc/passwd")
            err = str(exc_info.value).lower()
            assert "escape" in err or ".." in err

    def test_rejects_prefix_sibling_escape(self):
        """Paths must not escape via a directory name that extends the root prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "prompts"
            root.mkdir()
            sibling = Path(tmp) / "prompts_evil"
            sibling.mkdir()
            (sibling / "secret.j2").write_text("pwned", encoding="utf-8")
            with pytest.raises(ValueError) as exc_info:
                load_prompt_content(str(root), "../prompts_evil/secret.j2")
            err = str(exc_info.value).lower()
            assert "escape" in err or ".." in err

    def test_strips_leading_slash_from_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.j2"
            path.write_text("ok", encoding="utf-8")
            content = load_prompt_content(tmp, "/a.j2")
            assert content == "ok"


class TestValidatePromptVariables:
    """Tests for validate_prompt_variables."""

    def test_accepts_when_all_vars_in_spec(self):
        validate_prompt_variables("Hello {{ name }} and {{ count }}", {"name", "count"})

    def test_accepts_nested_attr_uses_first_segment(self):
        validate_prompt_variables("Value: {{ user.email }}", {"user"})

    def test_accepts_no_vars(self):
        validate_prompt_variables("Plain text.", set())

    def test_accepts_extra_spec_keys(self):
        validate_prompt_variables("{{ a }}", {"a", "b"})

    def test_raises_on_missing_var(self):
        with pytest.raises(ValueError) as exc_info:
            validate_prompt_variables("{{ missing }}", {"other"})
        assert "missing" in str(exc_info.value)
        assert "not defined" in str(exc_info.value).lower() or "with" in str(exc_info.value).lower()

    def test_raises_on_multiple_missing(self):
        with pytest.raises(ValueError) as exc_info:
            validate_prompt_variables("{{ a }} {{ b }}", {"a"})
        assert "b" in str(exc_info.value)
