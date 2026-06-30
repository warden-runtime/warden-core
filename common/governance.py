"""
Governance layer for tool execution: input/output validation and policy (e.g. CEL) in one place.

Execution code hands off to this layer with kwargs and a step spec; the layer runs
validation (and eventually CEL, audit, etc.) then invokes the raw executor.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import jsonschema
from tortoise.backends.base.client import BaseDBAsyncClient

from common.plugins.context import ExecutionScope
from common.plugins.registry import get_registry

logger = logging.getLogger(__name__)


def validate_against_schema(data: Any, schema: dict[str, Any], label: str) -> None:
    """Validate data against JSON Schema; log and re-raise on failure.

    Args:
        data: Instance to validate (any JSON-serializable structure).
        schema: JSON Schema dict (Draft-7 compatible).
        label: Label for log messages (e.g. "Tool foo input").

    Raises:
        jsonschema.ValidationError: If data does not conform to schema.
    """
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        logger.exception("%s validation failed: %s", label, e)
        raise


def _validate_execution_output(
    raw_output: str,
    output_schema: dict[str, Any],
    tool_name: str,
) -> None:
    """Validate tool output text against a JSON Schema (JSON body or wrapped plain text)."""
    label = f"Tool {tool_name} output"
    try:
        parsed = json.loads(raw_output)
        validate_against_schema(parsed, output_schema, label)
    except json.JSONDecodeError:
        validate_against_schema({"text": raw_output}, output_schema, label)


async def _validate_tool_input(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    strict_schema: dict[str, Any] | None,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> None:
    if not strict_schema:
        return
    try:
        validate_against_schema(arguments, strict_schema, f"Tool {tool_name} input")
        if scope is not None:
            await get_registry().tools.on_input_validation_passed(
                scope=scope,
                tool_name=tool_name,
                conn=conn,
                **hook_kw,
            )
    except jsonschema.ValidationError as e:
        if scope is not None:
            await get_registry().tools.on_input_validation_failed(
                scope=scope,
                tool_name=tool_name,
                error_message=str(e),
                conn=conn,
                **hook_kw,
            )
        raise


async def _run_tool_executor(
    *,
    executor: Callable[[], Awaitable[str]],
    tool_name: str,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> str:
    try:
        raw_output = await executor()
        if scope is not None:
            await get_registry().tools.on_execution_completed(
                scope=scope,
                tool_name=tool_name,
                output=raw_output,
                conn=conn,
                **hook_kw,
            )
        return raw_output
    except Exception as e:
        if scope is not None:
            await get_registry().tools.on_execution_failed(
                scope=scope,
                tool_name=tool_name,
                error_message=str(e),
                conn=conn,
                **hook_kw,
            )
        raise


async def _validate_tool_output(
    *,
    raw_output: str,
    output_schema: dict[str, Any] | None,
    tool_name: str,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> None:
    if not output_schema or not raw_output.strip():
        return
    try:
        _validate_execution_output(raw_output, output_schema, tool_name)
        if scope is not None:
            await get_registry().tools.on_output_validation_passed(
                scope=scope,
                tool_name=tool_name,
                conn=conn,
                **hook_kw,
            )
    except jsonschema.ValidationError as e:
        if scope is not None:
            await get_registry().tools.on_output_validation_failed(
                scope=scope,
                tool_name=tool_name,
                error_message=str(e),
                conn=conn,
                **hook_kw,
            )
        raise


async def execute_with_governance(
    tool_name: str,
    arguments: dict[str, Any],
    step_spec: dict[str, Any] | None,
    executor: Callable[[], Awaitable[str]],
    scope: ExecutionScope | None = None,
    conn: BaseDBAsyncClient | None = None,
    *,
    worker_definition: Any = None,
) -> str:
    """
    Run tool execution through the governance layer: input validation, then executor, then output validation.

    step_spec may contain strict_schema (input), output_schema (output). Room for CEL or other
    policy checks before/after executor in the future.

    Args:
        tool_name: Tool name for logging and error messages.
        arguments: Validated input kwargs passed to the executor (validated here if strict_schema).
        step_spec: Step-level policy (strict_schema, output_schema; future: CEL, etc.).
        executor: Async callable that performs the raw tool call and returns output text. No args.
        scope: Optional execution scope for tool.* ledger rows via registry hooks.
        conn: Optional DB connection; must match the worker execution transaction when set.
        worker_definition: Optional worker definition for full audit context in hooks.

    Returns:
        Raw tool output string.

    Raises:
        jsonschema.ValidationError: If input or output validation fails.
    """
    spec = step_spec or {}
    strict_schema = spec.get("strict_schema")
    output_schema = spec.get("output_schema")
    hook_kw = {"worker_definition": worker_definition}

    if scope is not None:
        await get_registry().tools.on_call_requested(
            scope=scope,
            tool_name=tool_name,
            arguments=arguments,
            conn=conn,
            **hook_kw,
        )

    await _validate_tool_input(
        tool_name=tool_name,
        arguments=arguments,
        strict_schema=strict_schema,
        scope=scope,
        conn=conn,
        hook_kw=hook_kw,
    )
    raw_output = await _run_tool_executor(
        executor=executor,
        tool_name=tool_name,
        scope=scope,
        conn=conn,
        hook_kw=hook_kw,
    )
    await _validate_tool_output(
        raw_output=raw_output,
        output_schema=output_schema,
        tool_name=tool_name,
        scope=scope,
        conn=conn,
        hook_kw=hook_kw,
    )
    return raw_output
