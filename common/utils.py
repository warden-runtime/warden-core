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
