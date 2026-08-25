"""Unit tests for saga-wins tools.bind overlay."""

from common.tool_arg_bind import bound_arguments_from_step, overlay_bound_tool_arguments


def test_overlay_saga_wins_over_junk_and_empty() -> None:
    schema = {
        "type": "object",
        "properties": {
            "container_id": {"type": "string"},
            "command": {"type": "string"},
        },
    }
    out = overlay_bound_tool_arguments(
        {"container_id": "llm-garbled", "command": "ls"},
        {"container_id": "saga-real-id"},
        input_schema=schema,
    )
    assert out == {"container_id": "saga-real-id", "command": "ls"}

    out_empty = overlay_bound_tool_arguments(
        {"container_id": "", "command": "ls"},
        {"container_id": "saga-real-id"},
        input_schema=schema,
    )
    assert out_empty["container_id"] == "saga-real-id"


def test_overlay_skips_keys_not_on_tool_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
    }
    out = overlay_bound_tool_arguments(
        {"command": "ls"},
        {"container_id": "saga-id", "problem_statement": "fix"},
        input_schema=schema,
    )
    assert out == {"command": "ls"}
    assert "container_id" not in out


def test_overlay_noop_without_schema_or_bound() -> None:
    llm = {"a": 1}
    assert overlay_bound_tool_arguments(llm, {"a": 2}, input_schema=None) == {"a": 1}
    assert overlay_bound_tool_arguments(llm, {}, input_schema={"properties": {"a": {}}}) == {"a": 1}


def test_bound_arguments_from_step_selects_keys() -> None:
    assert bound_arguments_from_step(
        {"container_id": "c1", "problem": "x", "other": 1},
        ["container_id", "missing"],
    ) == {"container_id": "c1"}
    assert bound_arguments_from_step({"a": 1}, None) == {}
    assert bound_arguments_from_step({"a": 1}, []) == {}
