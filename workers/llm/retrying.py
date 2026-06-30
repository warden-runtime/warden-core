"""Retry decorator for ChatModelPort implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from common.config import get_settings
from common.llm import ChatMessage, ChatModelPort, ChatResponse, ToolProtocol
from common.retry import retry_async
from workers.llm.retry_policy import is_transient_llm_error


@dataclass(frozen=True)
class LlmRetryPolicy:
    """Retry policy applied around ChatModelPort.ainvoke."""

    max_attempts: int
    base_delay_s: float
    max_delay_s: float
    is_retryable: Callable[[BaseException], bool] = field(default=is_transient_llm_error)


class RetryingChatModelPort(ChatModelPort):
    """Decorator that retries transient failures on ainvoke while preserving bind_tools."""

    def __init__(self, inner: ChatModelPort, policy: LlmRetryPolicy) -> None:
        self._inner = inner
        self._policy = policy

    def get_underlying_model(self) -> Any:
        return self._inner.get_underlying_model()

    def bind_tools(self, tools: Sequence[ToolProtocol]) -> RetryingChatModelPort:
        return RetryingChatModelPort(self._inner.bind_tools(tools), self._policy)

    async def ainvoke(self, messages: Sequence[ChatMessage]) -> ChatResponse:
        return await retry_async(
            lambda: self._inner.ainvoke(messages),
            is_retryable=self._policy.is_retryable,
            max_attempts=self._policy.max_attempts,
            base_delay_s=self._policy.base_delay_s,
            max_delay_s=self._policy.max_delay_s,
        )


def llm_retry_policy_from_settings() -> LlmRetryPolicy:
    """Build retry policy from application settings."""
    settings = get_settings()
    return LlmRetryPolicy(
        max_attempts=settings.llm_retry_max_attempts,
        base_delay_s=settings.llm_retry_base_delay_s,
        max_delay_s=settings.llm_retry_max_delay_s,
    )


def wrap_llm_with_retry(
    llm: ChatModelPort,
    policy: LlmRetryPolicy | None = None,
) -> ChatModelPort:
    """
    Wrap llm in RetryingChatModelPort when retry is enabled in settings.

    Returns the original port when retry is disabled or max_attempts is 1.
    """
    settings = get_settings()
    if not settings.llm_retry_enabled or settings.llm_retry_max_attempts <= 1:
        return llm
    resolved = policy if policy is not None else llm_retry_policy_from_settings()
    return RetryingChatModelPort(llm, resolved)
