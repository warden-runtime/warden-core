import asyncio
import contextlib
import logging
import os
import re
import sys
from asyncio import wait_for
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import anyio.lowlevel
from anyio.abc import Process
from anyio.streams.text import TextReceiveStream
from common.agent_adapter import ExecutionStepError
from common.governance import execute_with_governance
from common.plugins.context import (
    ExecutionScope,
    db_conn_from_injection,
    execution_scope_from_injection,
)
from common.plugins.registry import get_registry
from common.resource_specs import ResourceSpec
from common.utils import format_exception_chain
from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    StdioServerParameters,
    _create_platform_compatible_process,
    _get_executable_command,
    _terminate_process_tree,
    get_default_environment,
)
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from mcp.types import Tool as McpTool
from pydantic import AnyUrl, BaseModel, Field, create_model
from tortoise.backends.base.client import BaseDBAsyncClient
from workers.resource_runtime import (
    READ_RESOURCE_TOOL_NAME,
    ResourceAllowlist,
    compile_resource_allowlist,
    normalize_resource_content,
    validate_and_bind_resource_uri,
)

logger = logging.getLogger(__name__)
_MCP_CALL_TIMEOUT_S = float(os.getenv("WARDEN_MCP_CALL_TIMEOUT_S", "10"))
_RESOURCE_LIST_MAX_PAGES = int(os.getenv("WARDEN_MCP_RESOURCE_LIST_MAX_PAGES", "5"))
_RESOURCE_LIST_MAX_ITEMS = int(os.getenv("WARDEN_MCP_RESOURCE_LIST_MAX_ITEMS", "1000"))
_SSE_HEADER_ENV_RE = re.compile(r"\$\{(?:ENV:)?([A-Z0-9_]+)\}", re.IGNORECASE)
WARDEN_TOOL_INPUT_SCHEMA_ATTR = "warden_input_schema"


def get_warden_tool_input_schema(tool: Any) -> dict[str, Any] | None:
    """Return the MCP inputSchema stashed on a LangChain StructuredTool, if present."""
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    schema = metadata.get(WARDEN_TOOL_INPUT_SCHEMA_ATTR)
    return schema if isinstance(schema, dict) else None


def _format_mcp_exc(exc: BaseException) -> str:
    """Backward-compatible alias for format_exception_chain."""
    return format_exception_chain(exc)


def _env_names_from_docker_args(args: object) -> list[str]:
    """Return bare env var names from ``docker run -e VAR`` (no ``=value``) flags."""
    if not isinstance(args, list):
        return []
    names: list[str] = []
    index = 0
    while index < len(args):
        flag = args[index]
        if flag == "-e" and index + 1 < len(args):
            value = args[index + 1]
            if isinstance(value, str) and "=" not in value:
                names.append(value)
            index += 2
            continue
        index += 1
    return names


def _stdio_env_inherit_names(source_config: dict[str, Any]) -> list[str]:
    """Unique env var names forwarded via docker ``-e VAR`` and ``env_inherit``."""
    names = _env_names_from_docker_args(source_config.get("args", []))
    extra = source_config.get("env_inherit", [])
    if isinstance(extra, list):
        names.extend(name for name in extra if isinstance(name, str))
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _missing_stdio_env_vars(source_config: dict[str, Any]) -> list[str]:
    """Names required by stdio inherit config that are unset or empty on the worker."""
    resolved = _resolve_stdio_subprocess_env(source_config)
    return [
        name
        for name in _stdio_env_inherit_names(source_config)
        if not resolved.get(name, "").strip()
    ]


def _resolve_stdio_subprocess_env(source_config: dict[str, Any]) -> dict[str, str]:
    """Build stdio subprocess env: MCP defaults + docker ``-e`` inherit + explicit map."""
    env = dict(get_default_environment())
    inherit_names = _env_names_from_docker_args(source_config.get("args", []))
    extra = source_config.get("env_inherit", [])
    if isinstance(extra, list):
        inherit_names.extend(name for name in extra if isinstance(name, str))
    for name in inherit_names:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    explicit = source_config.get("env")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            if isinstance(key, str) and value is not None:
                env[key] = str(value)
    return env


def _interpolate_sse_header_value(raw: str) -> str:
    """Replace ``${ENV:VAR}`` / ``${VAR}`` placeholders from the worker process env."""

    def _replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        value = os.environ.get(env_name)
        if value is None:
            logger.warning(
                "SSE header references unset environment variable %s; substituting empty string",
                env_name,
            )
            return ""
        return value

    return _SSE_HEADER_ENV_RE.sub(_replace, raw)


def _resolve_sse_headers(source_config: dict[str, Any]) -> dict[str, str] | None:
    """Build SSE client headers from manifest map, resolving ``${ENV:VAR}`` placeholders."""
    explicit = source_config.get("headers")
    if not isinstance(explicit, dict) or not explicit:
        return None
    headers: dict[str, str] = {}
    for key, value in explicit.items():
        if not isinstance(key, str) or value is None:
            continue
        text = str(value)
        if _SSE_HEADER_ENV_RE.search(text):
            text = _interpolate_sse_header_value(text)
        headers[key] = text
    return headers or None


def _hook_kwargs(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    worker_definition = context.get("worker_definition")
    if worker_definition is None:
        return {}
    return {"worker_definition": worker_definition}


def _mcp_field_type(field_def: dict[str, Any]) -> type:
    json_type = field_def.get("type")
    if json_type == "integer":
        return int
    if json_type == "number":
        return float
    if json_type == "boolean":
        return bool
    if json_type == "array":
        return list
    if json_type == "object":
        return dict
    return str


class _ReadResourceArgs(BaseModel):
    uri: str = Field(..., description="Full MCP resource URI to read.")


async def _emit_resource_allowlist_loaded(
    *,
    allowlist: ResourceAllowlist,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> None:
    if scope is None:
        return
    await get_registry().tools.on_resource_allowlist_loaded(
        scope=scope,
        resource_uris=allowlist.templates,
        conn=conn,
        resource_count=len(allowlist.templates),
        **hook_kw,
    )


async def _discover_resources_for_source(
    *,
    session: ClientSession,
    source_name: str | None,
) -> list[str]:
    try:
        resources = await _list_resources_paginated(
            session,
            timeout_s=_MCP_CALL_TIMEOUT_S,
            max_pages=_RESOURCE_LIST_MAX_PAGES,
            max_items=_RESOURCE_LIST_MAX_ITEMS,
        )
    except Exception as exc:
        logger.warning(
            "MCP source %s list_resources failed: %s",
            source_name or "?",
            exc,
            exc_info=True,
        )
        return []
    return resources


async def _list_resources_paginated(
    session: ClientSession,
    *,
    timeout_s: float,
    max_pages: int,
    max_items: int,
) -> list[str]:
    items: list[str] = []
    cursor: str | None = None
    for _ in range(max_pages):
        if cursor:
            response = await wait_for(session.list_resources(cursor=cursor), timeout=timeout_s)
        else:
            response = await wait_for(session.list_resources(), timeout=timeout_s)
        resources = getattr(response, "resources", [])
        items.extend(str(resource.uri) for resource in resources)
        if len(items) >= max_items:
            return items[:max_items]
        cursor = (
            getattr(response, "nextCursor", None)
            or getattr(response, "next_cursor", None)
            or getattr(response, "cursor", None)
        )
        if not cursor:
            break
    return items


async def _read_resource_from_sessions(
    *,
    uri: str,
    source_sessions: list[tuple[str | None, ClientSession]],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> str:
    if scope is not None:
        await get_registry().tools.on_resource_read_requested(
            scope=scope,
            resource_uri=uri,
            conn=conn,
            **hook_kw,
        )

    last_error: str | None = None
    source_failures: list[dict[str, Any]] = []
    for source_name, session in source_sessions:
        try:
            result = await wait_for(
                session.read_resource(AnyUrl(uri)),
                timeout=_MCP_CALL_TIMEOUT_S,
            )
            text, meta = normalize_resource_content(list(result.contents))
            if scope is not None:
                await get_registry().tools.on_resource_read_completed(
                    scope=scope,
                    resource_uri=uri,
                    conn=conn,
                    content_meta=meta,
                    source_name=source_name,
                    **hook_kw,
                )
            return text
        except Exception as exc:
            last_error = _format_mcp_exc(exc)
            source_failures.append({"name": source_name, "error": last_error})
            logger.warning(
                "read_resource failed for %s via source %s: %s",
                uri,
                source_name or "?",
                last_error,
            )

    error_message = last_error or "resource not found on any MCP source"
    if scope is not None:
        await get_registry().tools.on_resource_read_failed(
            scope=scope,
            resource_uri=uri,
            error_message=error_message,
            conn=conn,
            **hook_kw,
        )
    raise ExecutionStepError(
        f"MCP resource read failed for {uri!r}: {error_message}",
        error_details={
            "code": "RESOURCE_READ_FAILED",
            "resource_uri": uri,
            "error": error_message,
            "source_failures": source_failures,
        },
    )


def _build_read_resource_tool(
    *,
    allowlist: ResourceAllowlist,
    source_sessions: list[tuple[str | None, ClientSession]],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    worker_definition: Any,
    saga_vars: dict[str, Any],
) -> StructuredTool:
    hook_kw = {"worker_definition": worker_definition}

    async def _execute_read_resource(uri: str) -> str:
        cleaned = uri.strip()
        matched_template = allowlist.assert_allowed(cleaned)
        try:
            validate_and_bind_resource_uri(matched_template, cleaned, saga_vars)
        except ExecutionStepError as exc:
            if scope is not None:
                await get_registry().tools.on_resource_read_failed(
                    scope=scope,
                    resource_uri=cleaned,
                    error_message=str(exc),
                    conn=conn,
                    **hook_kw,
                )
            raise
        return await _read_resource_from_sessions(
            uri=cleaned,
            source_sessions=source_sessions,
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
        )

    return StructuredTool.from_function(
        coroutine=_execute_read_resource,
        name=READ_RESOURCE_TOOL_NAME,
        description=("Read an MCP resource by URI. Only URIs allowed for this step may be read."),
        args_schema=_ReadResourceArgs,
    )


async def _load_tools_from_session(
    *,
    session: ClientSession,
    source_name: str | None,
    spec_by_name: dict[str, dict[str, Any]],
    allowed_tools: list[str],
    loaded_tool_names: set[str],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    worker_definition: Any,
) -> list[StructuredTool]:
    mcp_response = await wait_for(session.list_tools(), timeout=_MCP_CALL_TIMEOUT_S)
    loaded: list[StructuredTool] = []
    for mcp_tool in mcp_response.tools:
        if mcp_tool.name not in allowed_tools or mcp_tool.name in loaded_tool_names:
            continue
        if scope is not None:
            await get_registry().tools.on_discovered(
                scope=scope,
                tool_name=mcp_tool.name,
                conn=conn,
                source_name=source_name,
                **hook_kw,
            )
        logger.info("Loading MCP tool: %s from source %s", mcp_tool.name, source_name)
        loaded.append(
            _convert_mcp_to_langchain(
                mcp_tool,
                session,
                step_spec=spec_by_name.get(mcp_tool.name),
                scope=scope,
                conn=conn,
                worker_definition=worker_definition,
            )
        )
        loaded_tool_names.add(mcp_tool.name)
    return loaded


async def _process_mcp_source(
    *,
    source: dict[str, Any],
    exit_stack: AsyncExitStack,
    spec_by_name: dict[str, dict[str, Any]],
    allowed_tools: list[str],
    loaded_tool_names: set[str],
    allowlist: ResourceAllowlist | None,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    worker_definition: Any,
    source_sessions: list[tuple[str | None, ClientSession]],
    source_failures: list[dict[str, Any]],
    required_resource_patterns: list[re.Pattern[str]],
    has_required_resources: dict[str | None, bool],
) -> list[StructuredTool]:
    source_name = source.get("name")
    source_url = source.get("url")
    try:
        session = await _connect_to_source(source, exit_stack)
        if not session:
            await _record_source_attempt(
                scope=scope,
                conn=conn,
                hook_kw=hook_kw,
                source_name=source_name,
                source_url=source_url,
                outcome="skipped",
                error="connect_skipped_or_unsupported",
            )
            _append_source_failure(
                source_failures=source_failures,
                source_name=source_name,
                source_url=source_url,
                error="connect_skipped_or_unsupported",
            )
            return []

        await _record_source_attempt(
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
            source_name=source_name,
            source_url=source_url,
            outcome="connected",
        )

        source_sessions.append((source_name, session))
        if allowlist is not None:
            discovered = await _discover_resources_for_source(
                session=session,
                source_name=source_name,
            )
            has_required_resources[source_name] = _source_has_required_resources(
                discovered_resources=discovered,
                required_patterns=required_resource_patterns,
            )
        return await _load_tools_from_session(
            session=session,
            source_name=source_name,
            spec_by_name=spec_by_name,
            allowed_tools=allowed_tools,
            loaded_tool_names=loaded_tool_names,
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
            worker_definition=worker_definition,
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        detail = _format_mcp_exc(exc)
        await _record_source_attempt(
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
            source_name=source_name,
            source_url=source_url,
            outcome="failed",
            error=detail,
        )
        _append_source_failure(
            source_failures=source_failures,
            source_name=source_name,
            source_url=source_url,
            error=detail,
        )
        logger.warning(
            "Skipping MCP source %s after failure: %s",
            source_name or source,
            detail,
            exc_info=True,
        )
        return []


async def build_tools_for_worker(
    worker_def,  # WorkerDefinition ORM model
    tool_specs: list[dict[str, Any]],
    exit_stack: AsyncExitStack,
    context: dict[str, Any] | None = None,
    resource_specs: list[ResourceSpec] | None = None,
) -> list[StructuredTool]:
    """Connect to MCP sources, filter by tool_specs, return LangChain tools with governance."""
    scope = execution_scope_from_injection(context)
    conn = db_conn_from_injection(context)
    hook_kw = _hook_kwargs(context)
    allowlist = compile_resource_allowlist(resource_specs)
    final_tools: list[StructuredTool] = []
    spec_by_name: dict[str, dict[str, Any]] = {}
    for spec in tool_specs or []:
        name = spec.get("name")
        if isinstance(name, str):
            spec_by_name[name] = spec
    allowed_tools = list(spec_by_name.keys())
    loaded_tool_names: set[str] = set()
    source_failures: list[dict[str, Any]] = []
    source_sessions: list[tuple[str | None, ClientSession]] = []
    saga_vars = _saga_vars_from_context(context)
    required_patterns = _required_resource_patterns(allowlist)
    has_required_resources: dict[str | None, bool] = {}

    if not worker_def.tool_sources:
        return await _handle_no_sources(
            allowlist=allowlist,
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
            allowed_tools=allowed_tools,
        )

    for source in worker_def.tool_sources:
        final_tools.extend(
            await _process_mcp_source(
                source=source,
                exit_stack=exit_stack,
                spec_by_name=spec_by_name,
                allowed_tools=allowed_tools,
                loaded_tool_names=loaded_tool_names,
                allowlist=allowlist,
                scope=scope,
                conn=conn,
                hook_kw=hook_kw,
                worker_definition=hook_kw.get("worker_definition"),
                source_sessions=source_sessions,
                source_failures=source_failures,
                required_resource_patterns=required_patterns,
                has_required_resources=has_required_resources,
            )
        )

    if allowlist is not None and source_sessions and not any(has_required_resources.values()):
        raise ExecutionStepError(
            "No connected MCP source can satisfy required resource allowlist.",
            error_details={
                "code": "RESOURCE_REQUIRED_SOURCE_UNAVAILABLE",
                "resource_uris": allowlist.templates,
                "source_failures": source_failures,
                "source_resource_coverage": has_required_resources,
            },
        )

    missing = await _emit_tool_loaded_and_get_missing(
        allowed_tools=allowed_tools,
        loaded_tool_names=loaded_tool_names,
        scope=scope,
        conn=conn,
        hook_kw=hook_kw,
    )
    await _raise_for_missing_tools_if_any(
        missing=missing,
        scope=scope,
        conn=conn,
        hook_kw=hook_kw,
        source_failures=source_failures,
    )

    if allowlist is not None:
        await _emit_resource_allowlist_loaded(
            allowlist=allowlist,
            scope=scope,
            conn=conn,
            hook_kw=hook_kw,
        )
        final_tools.append(
            _build_read_resource_tool_or_raise(
                allowlist=allowlist,
                source_sessions=source_sessions,
                scope=scope,
                conn=conn,
                hook_kw=hook_kw,
                source_failures=source_failures,
                saga_vars=saga_vars,
            )
        )

    return final_tools


def _append_source_failure(
    *,
    source_failures: list[dict[str, Any]],
    source_name: str | None,
    source_url: str | None,
    error: str,
) -> None:
    source_failures.append({"name": source_name, "url": source_url, "error": error})


async def _record_source_attempt(
    *,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    source_name: str | None,
    source_url: str | None,
    outcome: str,
    error: str | None = None,
) -> None:
    if scope is None:
        return
    await get_registry().tools.on_mcp_source_attempted(
        scope=scope,
        source=source_name or "",
        conn=conn,
        source_name=source_name,
        source_url=source_url,
        outcome=outcome,
        error=error,
        **hook_kw,
    )


async def _handle_no_sources(
    *,
    allowlist: ResourceAllowlist | None,
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    allowed_tools: list[str],
) -> list[StructuredTool]:
    if allowlist is not None:
        raise ExecutionStepError(
            "MCP sources are required to read resources but worker has no tool_sources.",
            error_details={
                "code": "MCP_SOURCES_REQUIRED",
                "resource_uris": allowlist.templates,
            },
        )
    if scope is not None:
        await get_registry().tools.on_loaded(
            scope=scope,
            tool_name="",
            conn=conn,
            tool_names=[],
            missing_tool_names=allowed_tools,
            **hook_kw,
        )
    return []


async def _emit_tool_loaded_and_get_missing(
    *,
    allowed_tools: list[str],
    loaded_tool_names: set[str],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
) -> list[str]:
    missing = [name for name in allowed_tools if name not in loaded_tool_names]
    if scope is not None:
        await get_registry().tools.on_loaded(
            scope=scope,
            tool_name="",
            conn=conn,
            tool_names=sorted(loaded_tool_names),
            missing_tool_names=missing,
            **hook_kw,
        )
    return missing


def _mcp_unavailable_message(
    *,
    missing_tools: list[str],
    source_failures: list[dict[str, Any]],
) -> str:
    """Human-readable STEP_FAILED message when required MCP tools did not load."""
    base = (
        "Required MCP tool(s) could not be loaded (server unreachable or tool not listed): "
        + ", ".join(missing_tools)
    )
    if not source_failures:
        return base
    first = source_failures[0]
    detail = first.get("error") if isinstance(first, dict) else None
    if not detail:
        return base
    source_name = first.get("name") if isinstance(first, dict) else None
    prefix = f"MCP source {source_name!r} failed: " if source_name else "MCP source failed: "
    return f"{prefix}{detail}"


async def _raise_for_missing_tools_if_any(
    *,
    missing: list[str],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    source_failures: list[dict[str, Any]],
) -> None:
    if not missing:
        return
    if scope is not None:
        await get_registry().tools.on_load_failed(
            scope=scope,
            tool_name=missing[0],
            error_message="MCP_UNAVAILABLE",
            conn=conn,
            missing_tool_names=missing,
            **hook_kw,
        )
    message = _mcp_unavailable_message(missing_tools=missing, source_failures=source_failures)
    raise ExecutionStepError(
        message,
        error_details={
            "code": "MCP_UNAVAILABLE",
            "message": message,
            "missing_tools": missing,
            "source_failures": source_failures,
        },
    )


def _build_read_resource_tool_or_raise(
    *,
    allowlist: ResourceAllowlist,
    source_sessions: list[tuple[str | None, ClientSession]],
    scope: ExecutionScope | None,
    conn: BaseDBAsyncClient | None,
    hook_kw: dict[str, Any],
    source_failures: list[dict[str, Any]],
    saga_vars: dict[str, Any],
) -> StructuredTool:
    if not source_sessions:
        raise ExecutionStepError(
            "MCP resource reads require at least one connected MCP source.",
            error_details={
                "code": "MCP_SOURCES_REQUIRED",
                "resource_uris": allowlist.templates,
                "source_failures": source_failures,
            },
        )
    return _build_read_resource_tool(
        allowlist=allowlist,
        source_sessions=source_sessions,
        scope=scope,
        conn=conn,
        worker_definition=hook_kw.get("worker_definition"),
        saga_vars=saga_vars,
    )


def _required_resource_patterns(allowlist: ResourceAllowlist | None) -> list[re.Pattern[str]]:
    if allowlist is None:
        return []
    return [pattern.regex for pattern in allowlist.patterns]


def _source_has_required_resources(
    *,
    discovered_resources: list[str],
    required_patterns: list[re.Pattern[str]],
) -> bool:
    if not required_patterns:
        return True
    if not discovered_resources:
        # Discovery can be partial/unavailable on some MCP servers; defer hard failure to read-time.
        return True
    return any(
        pattern.fullmatch(uri) for pattern in required_patterns for uri in discovered_resources
    )


def _saga_vars_from_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    value = context.get("saga_vars")
    if isinstance(value, dict):
        return value
    return {}


_STDIO_PROCESS_WAIT_S = 2.0


async def _terminate_stdio_process_if_running(process: Process | None) -> None:
    """Last-resort stdio MCP subprocess cleanup when context unwind is partial."""
    if process is None or process.returncode is not None:
        return
    process.terminate()
    with contextlib.suppress(ProcessLookupError, TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=_STDIO_PROCESS_WAIT_S)


def _stdio_server_env(params: StdioServerParameters) -> dict[str, str]:
    if params.env is not None:
        return {**get_default_environment(), **params.env}
    return get_default_environment()


async def _stdio_stdout_reader(
    *,
    process: Process,
    read_stream_writer: Any,
    encoding: str,
    encoding_error_handler: str,
) -> None:
    if not process.stdout:
        raise RuntimeError("Opened MCP stdio process is missing stdout")
    try:
        async with read_stream_writer:
            buffer = ""
            async for chunk in TextReceiveStream(
                process.stdout,
                encoding=encoding,
                errors=encoding_error_handler,
            ):
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    try:
                        message = JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        logger.exception("Failed to parse JSONRPC message from MCP stdio server")
                        await read_stream_writer.send(exc)
                        continue
                    await read_stream_writer.send(SessionMessage(message))
    except anyio.ClosedResourceError:
        await anyio.lowlevel.checkpoint()


async def _stdio_stdin_writer(
    *,
    process: Process,
    write_stream_reader: Any,
    encoding: str,
    encoding_error_handler: str,
) -> None:
    if not process.stdin:
        raise RuntimeError("Opened MCP stdio process is missing stdin")
    try:
        async with write_stream_reader:
            async for session_message in write_stream_reader:
                payload = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                await process.stdin.send(
                    (payload + "\n").encode(encoding=encoding, errors=encoding_error_handler)
                )
    except anyio.ClosedResourceError:
        await anyio.lowlevel.checkpoint()


async def _shutdown_stdio_process(process: Process) -> None:
    if process.stdin:
        with contextlib.suppress(Exception):
            await process.stdin.aclose()
    try:
        with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
            await process.wait()
    except TimeoutError:
        await _terminate_process_tree(process)
    except ProcessLookupError:
        pass


@asynccontextmanager
async def _tracked_stdio_client(params: StdioServerParameters):
    """Stdio MCP transport with an exposed process handle for forced teardown."""
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    command = _get_executable_command(params.command)
    process = await _create_platform_compatible_process(
        command=command,
        args=params.args,
        env=_stdio_server_env(params),
        errlog=sys.stderr,
        cwd=params.cwd,
    )

    async def stdout_reader() -> None:
        await _stdio_stdout_reader(
            process=process,
            read_stream_writer=read_stream_writer,
            encoding=params.encoding,
            encoding_error_handler=params.encoding_error_handler,
        )

    async def stdin_writer() -> None:
        await _stdio_stdin_writer(
            process=process,
            write_stream_reader=write_stream_reader,
            encoding=params.encoding,
            encoding_error_handler=params.encoding_error_handler,
        )

    try:
        async with anyio.create_task_group() as tg, process:
            tg.start_soon(stdout_reader)
            tg.start_soon(stdin_writer)
            yield read_stream, write_stream, process
    finally:
        await _shutdown_stdio_process(process)
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()


async def _connect_stdio_source(
    *,
    source_config: dict[str, Any],
    stack: AsyncExitStack,
) -> ClientSession | None:
    cmd = source_config.get("command")
    if not isinstance(cmd, str):
        raise ValueError("stdio MCP source requires a string 'command'")
    missing_env = _missing_stdio_env_vars(source_config)
    if missing_env:
        joined = ", ".join(missing_env)
        raise RuntimeError(
            f"Required worker environment variable(s) not set or empty: {joined}. "
            f"Set {joined} in .env and restart the worker container."
        )
    params = StdioServerParameters(
        command=cmd,
        args=source_config.get("args", []),
        cwd=source_config.get("cwd"),
        env=_resolve_stdio_subprocess_env(source_config),
    )
    transport = await stack.enter_async_context(_tracked_stdio_client(params))
    read_stream, write_stream, process = transport
    stack.push_async_callback(_terminate_stdio_process_if_running, process)
    session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
    await session.initialize()
    return session


async def _connect_to_source(
    source_config: dict[str, Any], stack: AsyncExitStack
) -> ClientSession | None:
    """Connect to MCP server via SSE or Stdio; register with stack for cleanup."""
    transport_type = source_config.get("transport", "sse").lower()

    if transport_type == "sse":
        url = source_config.get("url")
        if not url:
            logger.error("SSE source missing URL: %s", source_config)
            return None

        headers = _resolve_sse_headers(source_config)
        streams = await stack.enter_async_context(sse_client(url, headers=headers))
        read_stream, write_stream = streams

        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    if transport_type == "stdio":
        return await _connect_stdio_source(source_config=source_config, stack=stack)

    return None


def _convert_mcp_to_langchain(
    mcp_tool: McpTool,
    session: ClientSession,
    step_spec: dict[str, Any] | None = None,
    scope: ExecutionScope | None = None,
    conn: BaseDBAsyncClient | None = None,
    worker_definition: Any = None,
) -> StructuredTool:
    """Create a LangChain StructuredTool that calls MCP and runs governance."""
    input_schema = mcp_tool.inputSchema or {}
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    fields = {}
    for field_name, field_def in properties.items():
        field_type = _mcp_field_type(field_def)
        if field_name in required:
            fields[field_name] = (field_type, ...)
        else:
            fields[field_name] = (field_type | None, None)

    pydantic_schema = create_model(f"{mcp_tool.name}Schema", **fields)

    async def _execute_tool(**kwargs: Any) -> str:
        arguments = {k: v for k, v in kwargs.items() if v is not None}

        async def raw_executor() -> str:
            logger.info("Calling MCP Tool %s with %s", mcp_tool.name, arguments)
            result = await session.call_tool(mcp_tool.name, arguments=arguments)
            output_text = []
            for content in result.content:
                if content.type == "text":
                    output_text.append(content.text)
                elif content.type == "image":
                    output_text.append(f"[Image: {content.mimeType}]")
            return "\n".join(output_text)

        try:
            return await execute_with_governance(
                tool_name=mcp_tool.name,
                arguments=arguments,
                step_spec=step_spec,
                executor=raw_executor,
                scope=scope,
                conn=conn,
                worker_definition=worker_definition,
            )
        except Exception as e:
            logger.exception("Error executing %s: %s", mcp_tool.name, e)
            raise

    tool = StructuredTool.from_function(
        coroutine=_execute_tool,
        name=mcp_tool.name,
        description=mcp_tool.description or "",
        args_schema=pydantic_schema,
        metadata={WARDEN_TOOL_INPUT_SCHEMA_ATTR: input_schema},
    )
    return tool
