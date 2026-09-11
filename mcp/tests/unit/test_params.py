"""Tests for shared query parameter and identity helpers."""

from warden_mcp.ids import resolve_definition_identity_params
from warden_mcp.tools._params import query_params


def test_query_params_serializes_bool_lowercase() -> None:
    params = query_params(is_active=True, namespace="default", include_total=False)
    assert ("is_active", "true") in params
    assert ("include_total", "false") in params
    assert ("namespace", "default") in params


def test_query_params_skips_none() -> None:
    assert query_params(limit=None, offset=10) == [("offset", "10")]


def test_resolve_definition_identity_by_id() -> None:
    params = resolve_definition_identity_params(
        definition_id="00000000-0000-4000-8000-000000000001",
        namespace=None,
        name=None,
        version=None,
    )
    assert params == [("id", "00000000-0000-4000-8000-000000000001")]


def test_resolve_definition_identity_by_triple() -> None:
    params = resolve_definition_identity_params(
        definition_id=None,
        namespace="default",
        name="greet",
        version="0.1.0",
    )
    assert params == [
        ("namespace", "default"),
        ("name", "greet"),
        ("version", "0.1.0"),
    ]


def test_resolve_definition_identity_rejects_both() -> None:
    err = resolve_definition_identity_params(
        definition_id="00000000-0000-4000-8000-000000000001",
        namespace="default",
        name="greet",
        version="0.1.0",
    )
    assert isinstance(err, dict)
    assert err["error"] is True
