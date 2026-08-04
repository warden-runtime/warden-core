import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model


def status_value(status: Any) -> str:
    """Normalize Tortoise enum or string status for audit payloads and hooks."""
    return status.value if hasattr(status, "value") else str(status)


def coerce_dict(value: Any) -> dict[str, Any]:
    """Return *value* when it is a mapping; otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def format_exception_chain(exc: BaseException) -> str:
    """Flatten ExceptionGroup / TaskGroup failures for logs and error_details."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(format_exception_chain(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def unwrap_execution_step_error(exc: BaseException) -> Any | None:
    """Return the first ExecutionStepError nested in an ExceptionGroup, if any."""
    from common.agent_adapter import ExecutionStepError

    if isinstance(exc, ExecutionStepError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = unwrap_execution_step_error(sub)
            if found is not None:
                return found
    return None


def tool_call_args_to_dict(args: Any) -> dict[str, Any]:
    """Normalize tool-call arguments to a dict for MCP invoke and hashing."""
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    model_dump = getattr(args, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=False)
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    return {"value": args}


def tool_call_arguments_hash(arguments: Any) -> str:
    """Hash tool call args the same way as governance tool audit."""
    return hash_canonical_dict(tool_call_args_to_dict(arguments))


DEFAULT_TOOL_ARG_COERCION_DEPTH = 2


def _coerce_boolean_string(stripped: str) -> bool | str:
    lower = stripped.lower()
    if lower in {"true", "1"}:
        return True
    if lower in {"false", "0"}:
        return False
    return stripped


def _coerce_integer_string(stripped: str) -> int | str:
    if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
        return int(stripped)
    return stripped


def _coerce_number_string(stripped: str) -> float | str:
    try:
        if stripped.count(".") == 1 and stripped.replace(".", "", 1).replace("-", "", 1).isdigit():
            return float(stripped)
    except ValueError:
        pass
    if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
        return float(stripped)
    return stripped


def _coerce_scalar_string(value: str, json_type: str) -> Any:
    """Coerce an unambiguous scalar string to integer, number, or boolean."""
    stripped = value.strip()
    if json_type == "boolean":
        return _coerce_boolean_string(stripped)
    if json_type == "integer":
        return _coerce_integer_string(stripped)
    if json_type == "number":
        return _coerce_number_string(stripped)
    return value


def _parse_json_container_string(value: str, json_type: str) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if json_type == "array" and isinstance(parsed, list):
        return parsed
    if json_type == "object" and isinstance(parsed, dict):
        return parsed
    return value


def _coerce_array_value(
    value: list[Any],
    field_schema: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
) -> list[Any]:
    if depth >= max_depth:
        return value
    items_schema = field_schema.get("items", {})
    if not isinstance(items_schema, dict):
        items_schema = {}
    return [
        _coerce_value_for_schema(
            item,
            items_schema,
            depth=depth + 1,
            max_depth=max_depth,
        )
        for item in value
    ]


def _coerce_object_value(
    value: dict[str, Any],
    field_schema: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
) -> dict[str, Any]:
    nested_properties = field_schema.get("properties", {})
    if not isinstance(nested_properties, dict) or not nested_properties:
        return value
    coerced = dict(value)
    for prop_name, prop_schema in nested_properties.items():
        if prop_name not in coerced:
            continue
        child_depth = depth + 1
        if child_depth > max_depth:
            continue
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        coerced[prop_name] = _coerce_value_for_schema(
            coerced[prop_name],
            prop_schema,
            depth=child_depth,
            max_depth=max_depth,
        )
    return coerced


_COERCE_NULL_UNCHANGED = object()
_JSON_TYPE_PRIORITY = ("boolean", "integer", "number", "array", "object", "string")


def _split_nullable_json_type(json_type: Any) -> tuple[Any, bool]:
    """Return (non-null type token, is_nullable) for a JSON Schema ``type`` value."""
    if isinstance(json_type, list):
        non_null = [t for t in json_type if t != "null"]
        nullable = any(t == "null" for t in json_type)
        if len(non_null) == 1:
            return non_null[0], nullable
        # Multi-type unions are not bindable as Pydantic unions; fall back to str.
        return "string", nullable
    return json_type, False


_JSON_NULL_TYPE = "null"


def _union_branch_type_token(branch: dict[str, Any]) -> tuple[str | None, bool] | None:
    """Return ``(non_null_token | None, branch_allows_null)`` or None if unparseable."""
    raw_type = branch.get("type")
    if raw_type is None:
        return None
    type_name, branch_null = _split_nullable_json_type(raw_type)
    if type_name == _JSON_NULL_TYPE:
        return None, True
    if not isinstance(type_name, str):
        return None
    return type_name, branch_null


def peel_simple_nullable_union(field_schema: dict[str, Any]) -> tuple[str, bool] | None:
    """Peel ``anyOf`` / ``oneOf`` of the form ``[T, null]`` into ``(type_token, nullable)``.

    Returns None when the schema has no such union, or when more than one non-null
    branch is present (full unions are not supported).
    """
    for key in ("anyOf", "oneOf"):
        branches = field_schema.get(key)
        if not isinstance(branches, list) or not branches:
            continue
        nullable = False
        non_null_tokens: list[str] = []
        for branch in branches:
            if not isinstance(branch, dict):
                return None
            parsed = _union_branch_type_token(branch)
            if parsed is None:
                return None
            type_name, branch_null = parsed
            if branch_null:
                nullable = True
            if type_name is None:
                continue
            non_null_tokens.append(type_name)
        if len(non_null_tokens) == 1:
            return non_null_tokens[0], nullable
        return None
    return None


def resolve_bindable_json_type(field_schema: dict[str, Any]) -> tuple[str, bool]:
    """Resolve a JSON Schema field to ``(type_token, is_nullable)`` for bind layers.

    Understands ``type`` strings/lists and simple ``anyOf``/``oneOf`` ``[T, null]``.
    Falls back to ``("string", False)`` when the shape is not bindable.
    """
    if "type" in field_schema:
        token, nullable = _split_nullable_json_type(field_schema.get("type"))
        if isinstance(token, str):
            return token, nullable
        return "string", nullable
    peeled = peel_simple_nullable_union(field_schema)
    if peeled is not None:
        return peeled
    return "string", False


def _schema_type_names(field_schema: dict[str, Any]) -> frozenset[str]:
    if "type" in field_schema:
        raw_type = field_schema["type"]
        if isinstance(raw_type, str):
            return frozenset({raw_type})
        if isinstance(raw_type, list):
            names = {item for item in raw_type if isinstance(item, str)}
            return frozenset(names) if names else frozenset({"string"})
        return frozenset({"string"})
    peeled = peel_simple_nullable_union(field_schema)
    if peeled is not None:
        token, nullable = peeled
        names = {token}
        if nullable:
            names.add("null")
        return frozenset(names)
    return frozenset({"string"})


def _schema_allows_null(type_names: frozenset[str]) -> bool:
    return "null" in type_names


def _primary_json_type(type_names: frozenset[str]) -> str:
    non_null = type_names - {"null"}
    for json_type in _JSON_TYPE_PRIORITY:
        if json_type in non_null:
            return json_type
    return "string"


def _try_coerce_null_string(value: Any, *, allows_null: bool) -> Any:
    if not allows_null or not isinstance(value, str):
        return _COERCE_NULL_UNCHANGED
    if value.strip().lower() in ("null", "none"):
        return None
    return _COERCE_NULL_UNCHANGED


def _coerce_string_for_primary_type(
    value: str,
    primary: str,
    *,
    depth: int,
    max_depth: int,
) -> Any:
    if primary in ("integer", "number", "boolean"):
        return _coerce_scalar_string(value, primary)
    if primary in ("array", "object"):
        if depth >= max_depth:
            return value
        return _parse_json_container_string(value, primary)
    return value


def _coerce_dict_to_string(value: dict[str, Any]) -> Any:
    """Flatten common LLM {path, symbol} objects when schema expects a string."""
    path = value.get("path")
    if isinstance(path, str) and path.strip():
        symbol = value.get("symbol")
        if isinstance(symbol, str) and symbol.strip():
            return f"{path.strip()}::{symbol.strip()}"
        return path.strip()
    return value


def _coerce_list_to_string(value: list[Any]) -> Any:
    """Join LLM string lists when schema expects a single string (e.g. constraints)."""
    if not value or not all(isinstance(item, str) for item in value):
        return value
    parts = [item.strip() for item in value if item.strip()]
    if not parts:
        return value
    if len(parts) == 1:
        return parts[0]
    return "; ".join(parts)


def _coerce_value_for_schema(
    value: Any,
    field_schema: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
) -> Any:
    """Best-effort coercion of a single value against a JSON Schema field definition."""
    type_names = _schema_type_names(field_schema)
    coerced_null = _try_coerce_null_string(value, allows_null=_schema_allows_null(type_names))
    if coerced_null is not _COERCE_NULL_UNCHANGED:
        return coerced_null

    primary = _primary_json_type(type_names)
    if primary == "string":
        if isinstance(value, dict):
            return _coerce_dict_to_string(value)
        if isinstance(value, list):
            return _coerce_list_to_string(value)
        return value

    if isinstance(value, str):
        value = _coerce_string_for_primary_type(
            value,
            primary,
            depth=depth,
            max_depth=max_depth,
        )

    if primary == "array" and isinstance(value, list):
        return _coerce_array_value(value, field_schema, depth=depth, max_depth=max_depth)

    if primary == "object" and isinstance(value, dict):
        return _coerce_object_value(value, field_schema, depth=depth, max_depth=max_depth)

    return value


def _omit_null_key_if_disallowed(
    result: dict[str, Any],
    field_name: str,
    field_schema: dict[str, Any],
) -> bool:
    """Drop ``field_name`` when value is null and schema disallows null. Return True if dropped."""
    if result.get(field_name) is not None:
        return False
    if field_name not in result:
        return False
    if _schema_allows_null(_schema_type_names(field_schema)):
        return False
    del result[field_name]
    return True


def _omit_disallowed_nulls(
    data: dict[str, Any],
    schema: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
) -> dict[str, Any]:
    """Drop object keys whose value is null when the field schema does not allow null."""
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return data

    result = dict(data)
    for field_name, field_schema in properties.items():
        if field_name not in result:
            continue
        if not isinstance(field_schema, dict):
            field_schema = {}
        if _omit_null_key_if_disallowed(result, field_name, field_schema):
            continue
        value = result[field_name]
        type_names = _schema_type_names(field_schema)
        if (
            isinstance(value, dict)
            and depth < max_depth
            and _primary_json_type(type_names) == "object"
        ):
            result[field_name] = _omit_disallowed_nulls(
                value,
                field_schema,
                depth=depth + 1,
                max_depth=max_depth,
            )
    return result


def coerce_llm_json_from_schema(
    args: dict[str, Any],
    input_schema: dict[str, Any],
    *,
    max_depth: int = DEFAULT_TOOL_ARG_COERCION_DEPTH,
) -> dict[str, Any]:
    """Admit sloppy LLM JSON against a JSON Schema before strict validation.

    Coerces stringified JSON arrays/objects and ambiguous scalar strings when the
    declared JSON Schema type expects a non-string value. ``string`` fields are
    never JSON-parsed. Drops present ``null`` on fields whose schema does not
    allow null (treat as absent). Recursion is limited to *max_depth* levels
    (default 2: top-level fields plus one nested level inside arrays/objects).
    Best-effort: values that cannot be coerced are left unchanged. Used for MCP
    tool args and reason-step ``output_schema`` admission.
    """
    if not isinstance(args, dict):
        return {}
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    if not isinstance(properties, dict):
        return dict(args)

    result = dict(args)
    for field_name, field_schema in properties.items():
        if field_name not in result:
            continue
        if not isinstance(field_schema, dict):
            field_schema = {}
        result[field_name] = _coerce_value_for_schema(
            result[field_name],
            field_schema,
            depth=0,
            max_depth=max_depth,
        )
    return _omit_disallowed_nulls(result, input_schema, depth=0, max_depth=max_depth)


coerce_tool_args_from_schema = coerce_llm_json_from_schema
"""Alias of :func:`coerce_llm_json_from_schema` (MCP / legacy import name)."""


def hash_canonical_dict(data: dict[str, Any]) -> str:
    """Deterministic SHA-256 of a dict using the same JSON rules as audit payload hashing."""
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_model_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"M_{cleaned}"
    return cleaned


_PRIMITIVE_JSON_TYPES: dict[str, type[Any]] = {
    "integer": int,
    "number": float,
    "boolean": bool,
    "string": str,
}


def _object_field_type(field_info: dict[str, Any], *, model_name: str) -> type[Any]:
    properties = field_info.get("properties")
    if isinstance(properties, dict) and properties:
        return create_pydantic_model_from_schema(field_info, model_name=model_name)
    return dict[str, Any]


def _array_field_type(field_info: dict[str, Any], *, model_name: str) -> type[Any]:
    items_info = field_info.get("items", {})
    if not isinstance(items_info, dict):
        items_info = {}
    items_type, _ = _split_nullable_json_type(items_info.get("type", "string"))
    if items_type == "object" and isinstance(items_info.get("properties"), dict):
        item_model = create_pydantic_model_from_schema(items_info, model_name=f"{model_name}Item")
        return list[item_model]  # type: ignore[valid-type]
    item_primitive = _PRIMITIVE_JSON_TYPES.get(
        items_type if isinstance(items_type, str) else "", str
    )
    return list[item_primitive]  # type: ignore[valid-type]


def _field_type_from_json_schema(
    field_info: dict[str, Any],
    *,
    model_name: str,
) -> Any:
    json_type, nullable = _split_nullable_json_type(field_info.get("type", "string"))
    if json_type == "object":
        base: Any = _object_field_type(field_info, model_name=model_name)
    elif json_type == "array":
        base = _array_field_type(field_info, model_name=model_name)
    elif isinstance(json_type, str) and json_type in _PRIMITIVE_JSON_TYPES:
        base = _PRIMITIVE_JSON_TYPES[json_type]
    else:
        base = str
    if nullable:
        return base | None
    return base


def create_pydantic_model_from_schema(
    schema: dict[str, Any], model_name: str = "DynamicOutput"
) -> type[BaseModel]:
    """Dynamically creates a Pydantic model from a simplified JSON Schema subset.

    Used for structured LLM output. Supports: string, integer, number, boolean,
    nested objects (``type: object`` with ``properties``), arrays of primitives or
    objects, and nullable unions via ``type: ["T", "null"]``. Optional fields use
    default None. Extra keys use ``extra="forbid"`` only when the schema sets
    ``additionalProperties: false``; otherwise extras are allowed (aligned with
    typical JSON Schema defaults).

    Args:
        schema: Dict with "properties" (and optional "required"). Each
            property may have "type", "items" (for arrays), "properties" (for
            nested objects), and "description".
        model_name: Name of the generated model class.

    Returns:
        A Pydantic BaseModel subclass with fields derived from schema.
    """
    fields: dict[str, Any] = {}
    safe_root = _sanitize_model_name(model_name)

    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    extra = "forbid" if schema.get("additionalProperties") is False else "allow"

    for field_name, field_info in properties.items():
        if not isinstance(field_info, dict):
            field_info = {}
        nested_name = _sanitize_model_name(f"{safe_root}_{field_name}")
        field_type = _field_type_from_json_schema(field_info, model_name=nested_name)
        description = field_info.get("description", "")

        if field_name in required_fields:
            fields[field_name] = (field_type, Field(..., description=description))
        else:
            fields[field_name] = (field_type | None, Field(None, description=description))

    return create_model(
        safe_root,
        __config__=ConfigDict(extra=extra),
        **fields,
    )


_OUTPUT_SCHEMA_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
        "$ref",
        "$defs",
        "definitions",
    }
)


def assert_output_schema_bind_supported(schema: dict[str, Any], *, path: str = "$") -> None:
    """Reject composition/conditional keywords that silently no-op at Pydantic bind.

    Validation-only constraints (``minLength``, ``enum``, ``additionalProperties``,
    ``type: ["T","null"]``, etc.) are allowed. Raises ``ValueError`` with the JSON
    path of the first unsupported keyword.
    """
    for key, value in schema.items():
        child_path = f"{path}.{key}"
        if key in _OUTPUT_SCHEMA_UNSUPPORTED_KEYWORDS:
            raise ValueError(
                f"output_schema uses unsupported keyword {key!r} at {child_path}; "
                "flatten conditionals / inline $ref before deploy "
                f"(unsupported: {', '.join(sorted(_OUTPUT_SCHEMA_UNSUPPORTED_KEYWORDS))})"
            )
        if isinstance(value, dict):
            assert_output_schema_bind_supported(value, path=child_path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    assert_output_schema_bind_supported(item, path=f"{child_path}[{i}]")
