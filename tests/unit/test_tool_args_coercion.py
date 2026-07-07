"""Unit tests for schema-aware tool argument coercion."""

from __future__ import annotations

from common.utils import coerce_tool_args_from_schema

_COMMANDS_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["commands"],
}

_META_SCHEMA = {
    "type": "object",
    "properties": {
        "meta": {"type": "object", "properties": {"k": {"type": "string"}}},
    },
}

_SCALAR_SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "weight": {"type": "number"},
        "enabled": {"type": "boolean"},
        "body": {"type": "string"},
    },
}

_NESTED_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
            },
        },
    },
}

_NESTED_OPTS_SCHEMA = {
    "type": "object",
    "properties": {
        "opts": {"type": "array", "items": {"type": "integer"}},
    },
}

_TAGS_IN_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "payload": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

_DEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "config": {
            "type": "object",
            "properties": {
                "inner": {
                    "type": "object",
                    "properties": {
                        "nums": {"type": "array", "items": {"type": "integer"}},
                    },
                },
            },
        },
    },
}


def test_coerce_top_level_stringified_array():
    result = coerce_tool_args_from_schema(
        {"commands": '["echo foo"]'},
        _COMMANDS_SCHEMA,
    )
    assert result == {"commands": ["echo foo"]}


def test_coerce_top_level_stringified_object():
    result = coerce_tool_args_from_schema(
        {"meta": '{"k":"v"}'},
        _META_SCHEMA,
    )
    assert result == {"meta": {"k": "v"}}


def test_coerce_top_level_integer_string():
    result = coerce_tool_args_from_schema({"count": "42"}, _SCALAR_SCHEMA)
    assert result["count"] == 42


def test_coerce_top_level_number_string():
    result = coerce_tool_args_from_schema({"weight": "3.14"}, _SCALAR_SCHEMA)
    assert result["weight"] == 3.14


def test_coerce_top_level_boolean_string():
    result = coerce_tool_args_from_schema({"enabled": "true"}, _SCALAR_SCHEMA)
    assert result["enabled"] is True


def test_string_field_never_json_parsed():
    raw = '{"x":1}'
    result = coerce_tool_args_from_schema({"body": raw}, _SCALAR_SCHEMA)
    assert result["body"] == raw


def test_invalid_json_array_string_left_unchanged():
    raw = "[not json"
    result = coerce_tool_args_from_schema({"commands": raw}, _COMMANDS_SCHEMA)
    assert result["commands"] == raw


def test_nested_object_property_stringified_array():
    result = coerce_tool_args_from_schema(
        {"payload": {"tags": '["a","b"]'}},
        _TAGS_IN_OBJECT_SCHEMA,
    )
    assert result == {"payload": {"tags": ["a", "b"]}}


def test_nested_array_of_objects_coerces_item_fields():
    result = coerce_tool_args_from_schema(
        {"items": '[{"id":"1"},{"id":"2"}]'},
        _NESTED_ITEMS_SCHEMA,
    )
    assert result == {"items": [{"id": 1}, {"id": 2}]}


def test_nested_object_property_stringified_array_field():
    result = coerce_tool_args_from_schema(
        {"opts": "[1,2]"},
        _NESTED_OPTS_SCHEMA,
    )
    assert result == {"opts": [1, 2]}


def test_depth_limit_stops_at_two_levels_without_crashing():
    config_json = '{"inner": {"nums": "[9,10]"}}'
    result = coerce_tool_args_from_schema(
        {"config": config_json},
        _DEEP_SCHEMA,
    )
    assert result["config"]["inner"]["nums"] == "[9,10]"


def test_does_not_mutate_input_dict():
    args = {"commands": '["echo"]'}
    result = coerce_tool_args_from_schema(args, _COMMANDS_SCHEMA)
    assert result is not args
    assert args["commands"] == '["echo"]'
    assert result["commands"] == ["echo"]


def test_unknown_fields_left_untouched():
    result = coerce_tool_args_from_schema(
        {"commands": '["x"]', "extra": "value"},
        _COMMANDS_SCHEMA,
    )
    assert result["extra"] == "value"
