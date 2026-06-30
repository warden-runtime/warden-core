"""Unit tests for workers.utils."""

from workers.utils import _coerce_rendered_string, resolve_input


def test_resolve_input_plain_string_without_jinja():
    assert resolve_input("Hello", {}) == "Hello"


def test_resolve_input_renders_jinja_and_keeps_prose():
    result = resolve_input("Claim {{ claim_id }}", {"claim_id": "abc-1"})
    assert result == "Claim abc-1"


def test_resolve_input_coerces_scalar_booleans_and_numbers():
    assert resolve_input("{{ flag }}", {"flag": "True"}) is True
    assert resolve_input("{{ n }}", {"n": "42"}) == 42
    assert resolve_input("{{ amount }}", {"amount": "500.0"}) == 500.0


def test_resolve_input_does_not_literal_eval_dict_like_rendered_string():
    rendered = '{"nested": [1, 2, 3]}'
    assert resolve_input("{{ payload }}", {"payload": rendered}) == rendered


def test_coerce_rendered_string_rejects_ambiguous_content():
    assert _coerce_rendered_string("Hello World") == "Hello World"
    assert _coerce_rendered_string("[1, 2]") == "[1, 2]"


def test_resolve_input_nested_dict_templates():
    result = resolve_input(
        {"id": "{{ claim_id }}", "note": "static"},
        {"claim_id": "c-9"},
    )
    assert result == {"id": "c-9", "note": "static"}
