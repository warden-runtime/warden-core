"""Validate catalog step input ports (JSON Schema fragments) and bound values."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import jsonschema

from common.governance import validate_against_schema
from common.utils import assert_output_schema_bind_supported

if TYPE_CHECKING:
    from common.schemas.saga import StepParameterSpec
    from common.schemas.step import StepInputSpec

logger = logging.getLogger(__name__)


def assert_input_port_schema_supported(schema: dict[str, Any], *, port_name: str) -> None:
    """Reject invalid or unsupported JSON Schema on a catalog input port."""
    try:
        jsonschema.Draft7Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ValueError(f"inputs[{port_name!r}].schema is not a valid Draft-7 schema: {e}") from e
    try:
        assert_output_schema_bind_supported(schema, path=f"$.inputs.{port_name}.schema")
    except ValueError as e:
        raise ValueError(f"inputs[{port_name!r}].schema: {e}") from e


def validate_step_blueprint_input_schemas(inputs: dict[str, StepInputSpec]) -> None:
    """At step deploy: every declared port schema must be valid/supported."""
    for name, port in inputs.items():
        if port.schema_ is None:
            continue
        if not isinstance(port.schema_, dict):
            raise ValueError(f"inputs[{name!r}].schema must be a JSON object")
        assert_input_port_schema_supported(port.schema_, port_name=name)


def validate_with_literals_against_input_schemas(
    *,
    step_id: str,
    with_spec: dict[str, StepParameterSpec],
    inputs: dict[str, StepInputSpec],
) -> None:
    """At saga deploy: validate ``value:`` literals against port schemas (skip ``from:``)."""
    for key, binding in with_spec.items():
        port = inputs.get(key)
        if port is None or port.schema_ is None:
            continue
        # StepParameterSpec: exactly one of from/value; value may be null.
        if binding.from_path is not None:
            continue
        label = f"step {step_id!r} input {key!r}"
        try:
            validate_against_schema(binding.value, port.schema_, label)
        except jsonschema.ValidationError as e:
            raise ValueError(f"{label} literal failed schema validation: {e.message}") from e


def assert_jsonpath_bindings_resolved(
    *,
    step_id: str,
    missing_from: dict[str, str],
) -> None:
    """At schedule time: fail clearly when a ``from:`` JSONPath matched nothing.

    Distinguishes "path absent in context" from "path present with null", so
    operators do not see a generic schema ``null`` type error for a bad path.
    """
    if not missing_from:
        return
    parts = [f"{key!r} (from: {path})" for key, path in sorted(missing_from.items())]
    raise ValueError(f"step {step_id!r}: JSONPath target missing for input(s): {', '.join(parts)}")


def validate_resolved_arguments_against_input_ports(
    *,
    step_id: str,
    resolved: dict[str, Any],
    input_ports: dict[str, Any] | None,
) -> None:
    """At schedule time: validate resolved ``with`` values against frozen input port schemas."""
    if not input_ports:
        if resolved:
            logger.warning(
                "step %r has resolved arguments but empty input_ports; "
                "schedule-time port schema validation was skipped",
                step_id,
            )
        return
    for key, value in resolved.items():
        port = input_ports.get(key)
        if not isinstance(port, dict):
            continue
        schema = port.get("schema")
        if not isinstance(schema, dict):
            continue
        label = f"step {step_id!r} input {key!r}"
        try:
            validate_against_schema(value, schema, label)
        except jsonschema.ValidationError as e:
            raise ValueError(f"{label} resolved value failed schema validation: {e.message}") from e
