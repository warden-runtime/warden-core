"""Tiered structured LLM completion for agent-adapter: simple reason steps."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

from common.agent_adapter import ExecutionStepError
from common.error_details import build_step_error_details
from common.execution_timing import WorkerTimingAccumulator, elapsed_ms
from common.governance import admit_and_validate
from common.llm import ChatMessage, ChatModelPort, ChatResponse
from common.utils import create_pydantic_model_from_schema
from workers.adapters.simple_schema import resolve_effective_schema
from workers.adapters.state_utils import parse_json_object_from_assistant_text

logger = logging.getLogger(__name__)

_JSON_MODE_SYSTEM_APPENDIX = (
    "\n\nRespond with a single JSON object matching the required output schema. "
    "Do not include markdown fences or prose outside the JSON object."
)


class SchemaBoundChatModel(ChatModelPort):
    """ChatModelPort wrapper that constrains ainvoke to schema-shaped JSON output."""

    def __init__(self, inner: ChatModelPort, schema: dict[str, Any]) -> None:
        self._inner = inner
        self._schema = schema

    def get_underlying_model(self) -> Any:
        return self._inner.get_underlying_model()

    def bind_tools(self, tools: Sequence[Any]) -> SchemaBoundChatModel:
        return SchemaBoundChatModel(self._inner.bind_tools(tools), self._schema)

    def bind_json_schema(self, schema: dict[str, Any]) -> SchemaBoundChatModel:
        return SchemaBoundChatModel(self._inner, schema)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        payload = await invoke_structured_output(
            self._inner,
            list(messages),
            self._schema,
        )
        return ChatResponse(content=json.dumps(payload, ensure_ascii=False))


_RESPONSE_PREVIEW_LEN = 500


def _raise_structured_output_failed(
    *,
    last_error: str | None,
    response_preview: str | None,
) -> NoReturn:
    message = last_error or "Model did not return valid structured JSON for this step."
    extra: dict[str, Any] = {"error": last_error or "structured_output_failed"}
    if response_preview:
        extra["response_preview"] = response_preview[:_RESPONSE_PREVIEW_LEN]
    raise ExecutionStepError(
        message,
        error_details=build_step_error_details(
            code="structured_output_failed",
            message=message,
            **extra,
        ),
    )


def _raise_empty_structured_result() -> NoReturn:
    message = "Structured step output must be a non-empty JSON object."
    raise ExecutionStepError(
        message,
        error_details=build_step_error_details(
            code="empty_structured_result",
            message=message,
            error="empty_structured_result",
        ),
    )


def _raise_schema_validation_failed(exc: Exception) -> NoReturn:
    logger.exception("Structured step output schema validation failed: %s", exc)
    message = str(exc)
    raise ExecutionStepError(
        message,
        error_details=build_step_error_details(
            code="OUTPUT_SCHEMA_VALIDATION_FAILED",
            message=message,
            error=message,
            validation="output_schema",
        ),
    ) from exc


async def invoke_structured_output(
    llm: ChatModelPort,
    messages: list[ChatMessage],
    schema: dict[str, Any],
    *,
    timing_acc: WorkerTimingAccumulator | None = None,
) -> dict[str, Any]:
    """Run tiered structured completion and return a validated business dict."""
    llm_start: float | None = None
    if timing_acc is not None:
        llm_start = time.perf_counter()

    payload: dict[str, Any] | None = None
    last_error: str | None = None
    response_preview: str | None = None

    payload, last_error = await _try_native_structured_output(llm, messages, schema)
    if payload is None:
        payload, last_error, response_preview = await _try_json_mode_output(llm, messages, schema)

    if timing_acc is not None and llm_start is not None:
        timing_acc.add_ms("llm_ms", elapsed_ms(llm_start))

    if payload is None:
        _raise_structured_output_failed(
            last_error=last_error,
            response_preview=response_preview,
        )

    if not payload:
        _raise_empty_structured_result()

    try:
        return admit_and_validate(payload, schema, "step output (structured)")
    except Exception as exc:
        _raise_schema_validation_failed(exc)


async def _try_native_structured_output(
    llm: ChatModelPort,
    messages: list[ChatMessage],
    schema: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    underlying = llm.get_underlying_model()
    if underlying is None:
        return None, "provider does not expose native structured output"
    try:
        model_cls = create_pydantic_model_from_schema(schema, model_name="SimpleStepOutput")
        structured = underlying.with_structured_output(model_cls, method="json_schema")
        lc_messages = [_chat_message_to_langchain(m) for m in messages]
        result = await structured.ainvoke(lc_messages)
        if hasattr(result, "model_dump"):
            payload = result.model_dump(mode="json", exclude_none=False)
            return (payload if isinstance(payload, dict) else None), None
        if isinstance(result, dict):
            return result, None
    except Exception as exc:
        logger.debug("Native structured output failed: %s", exc)
        return None, f"native structured output failed: {exc}"
    return None, "native structured output returned unexpected type"


async def _try_json_mode_output(
    llm: ChatModelPort,
    messages: list[ChatMessage],
    schema: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    augmented = _append_json_mode_instruction(messages, schema)
    try:
        response = await llm.ainvoke(augmented)
    except Exception as exc:
        logger.debug("JSON-mode ainvoke failed: %s", exc)
        return None, f"json-mode invoke failed: {exc}", None

    content = response.content or ""
    payload = parse_json_object_from_assistant_text(content)
    if payload is None:
        return None, "model response was not parseable JSON", content or None
    return payload, None, None


def _append_json_mode_instruction(
    messages: list[ChatMessage],
    schema: dict[str, Any],
) -> list[ChatMessage]:
    schema_hint = json.dumps(schema, ensure_ascii=False)
    appendix = f"{_JSON_MODE_SYSTEM_APPENDIX}\nRequired schema: {schema_hint}"
    out: list[ChatMessage] = []
    system_seen = False
    for msg in messages:
        if msg.role == "system" and not system_seen:
            out.append(ChatMessage(role="system", content=f"{msg.content}{appendix}"))
            system_seen = True
        else:
            out.append(msg)
    if not system_seen:
        out.insert(0, ChatMessage(role="system", content=appendix.strip()))
    return out


def _chat_message_to_langchain(msg: ChatMessage) -> Any:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    if msg.role == "system":
        return SystemMessage(content=msg.content)
    if msg.role == "human":
        return HumanMessage(content=msg.content)
    if msg.role == "assistant":
        lc_tool_calls = None
        if msg.tool_calls:
            lc_tool_calls = [
                {"name": tc.name, "args": tc.args, "id": tc.id} for tc in msg.tool_calls
            ]
        return AIMessage(content=msg.content, tool_calls=lc_tool_calls or [])
    if msg.role == "tool":
        return ToolMessage(
            content=msg.content,
            tool_call_id=msg.tool_call_id or "",
            name=msg.name or "",
        )
    raise ValueError(f"Unknown ChatMessage role: {msg.role!r}")


__all__ = [
    "SchemaBoundChatModel",
    "invoke_structured_output",
    "resolve_effective_schema",
]
