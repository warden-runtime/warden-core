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


def _coerce_value_for_schema(
    value: Any,
    field_schema: dict[str, Any],
    *,
    depth: int,
    max_depth: int,
) -> Any:
    """Best-effort coercion of a single value against a JSON Schema field definition."""
    json_type = field_schema.get("type", "string")
    if json_type == "string":
        return value

    if isinstance(value, str):
        if json_type in {"integer", "number", "boolean"}:
            return _coerce_scalar_string(value, json_type)
        if json_type in {"array", "object"}:
            if depth >= max_depth:
                return value
            value = _parse_json_container_string(value, json_type)

    if json_type == "array" and isinstance(value, list):
        return _coerce_array_value(value, field_schema, depth=depth, max_depth=max_depth)

    if json_type == "object" and isinstance(value, dict):
        return _coerce_object_value(value, field_schema, depth=depth, max_depth=max_depth)

    return value


def coerce_llm_json_from_schema(
    args: dict[str, Any],
    input_schema: dict[str, Any],
    *,
    max_depth: int = DEFAULT_TOOL_ARG_COERCION_DEPTH,
) -> dict[str, Any]:
    """Admit sloppy LLM JSON against a JSON Schema before strict validation.

    Coerces stringified JSON arrays/objects and ambiguous scalar strings when the
    declared JSON Schema type expects a non-string value. ``string`` fields are
    never JSON-parsed. Recursion is limited to *max_depth* levels (default 2:
    top-level fields plus one nested level inside arrays/objects). Best-effort:
    values that cannot be coerced are left unchanged. Used for MCP tool args and
    reason-step ``output_schema`` admission.
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
    return result


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


def _field_type_from_json_schema(field_info: dict[str, Any]) -> type[Any]:
    json_type = field_info.get("type", "string")
    if json_type == "integer":
        return int
    if json_type == "number":
        return float
    if json_type == "boolean":
        return bool
    if json_type == "array":
        items_info = field_info.get("items", {})
        items_type = items_info.get("type", "string") if isinstance(items_info, dict) else "string"
        if items_type == "integer":
            return list[int]
        if items_type == "number":
            return list[float]
        if items_type == "boolean":
            return list[bool]
        return list[str]
    return str


def create_pydantic_model_from_schema(
    schema: dict[str, Any], model_name: str = "DynamicOutput"
) -> type[BaseModel]:
    """Dynamically creates a Pydantic model from a simplified JSON Schema subset.

    Used for structured LLM output and dynamic tool arguments. Supports types:
    string, integer, number, boolean, and arrays of those primitives. Optional
    fields use default None. Unknown top-level keys are rejected (extra=forbid).

    Args:
        schema: Dict with "properties" (and optional "required"). Each
            property may have "type", "items" (for arrays), and "description".
        model_name: Name of the generated model class.

    Returns:
        A Pydantic BaseModel subclass with fields derived from schema.
    """
    fields: dict[str, Any] = {}

    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))

    for field_name, field_info in properties.items():
        if not isinstance(field_info, dict):
            field_info = {}
        field_type = _field_type_from_json_schema(field_info)
        description = field_info.get("description", "")

        if field_name in required_fields:
            fields[field_name] = (field_type, Field(..., description=description))
        else:
            fields[field_name] = (field_type | None, Field(None, description=description))

    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
