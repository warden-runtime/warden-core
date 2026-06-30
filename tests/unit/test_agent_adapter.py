"""Unit tests for the agent adapter port and DTOs."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from common.agent_adapter import (
    CompensationResult,
    ExecutionStepError,
    StepResult,
)
from workers.adapter_resolver import resolve_adapter
from workers.adapters.langchain import LangChainAdapter


def test_step_result_output():
    """StepResult accepts output dict (e.g. results + summary)."""
    result = StepResult(
        output={"data": {"results": [{"tool": "t1", "output": "ok"}], "summary": "Done."}},
    )
    assert result.output["data"]["results"] == [{"tool": "t1", "output": "ok"}]
    assert result.output["data"]["summary"] == "Done."


def test_step_result_defaults():
    """StepResult defaults to empty dict."""
    result = StepResult()
    assert result.output == {}


def test_compensation_result_output():
    """CompensationResult accepts output dict."""
    result = CompensationResult(output={"rolled_back": True})
    assert result.output == {"rolled_back": True}


def test_execution_step_error_carries_details():
    """ExecutionStepError carries message, tool, and error_details for handler."""
    err = ExecutionStepError(
        "Tool failed", tool="get_weather", error_details={"error": "Timeout", "tool": "get_weather"}
    )
    assert str(err) == "Tool failed"
    assert err.tool == "get_weather"
    assert err.error_details == {"error": "Timeout", "tool": "get_weather"}


def test_execution_step_error_default_error_details():
    """ExecutionStepError builds error_details from message and tool if not provided."""
    err = ExecutionStepError("Oops", tool="my_tool")
    assert err.error_details == {"error": "Oops", "tool": "my_tool"}


def test_resolve_adapter_returns_langchain_adapter():
    """resolve_adapter returns LangChainAdapter when adapter is langchain or missing."""
    worker_def = MagicMock()
    worker_def.adapter = "langchain"
    secret = MagicMock()
    secret.api_key = "sk-fake"
    port = resolve_adapter(worker_definition=worker_def, secret=secret)
    assert isinstance(port, LangChainAdapter)


def test_resolve_adapter_defaults_to_langchain_when_adapter_missing():
    """resolve_adapter defaults to langchain when worker_definition has no adapter attr."""
    worker_def = SimpleNamespace(
        model_provider="openai",
        model_name="gpt-4o",
        system_prompt="Hi",
        tool_sources=[],
        compensation_prompt=None,
    )
    secret = SimpleNamespace(api_key="sk-fake")
    port = resolve_adapter(worker_definition=worker_def, secret=secret)
    assert isinstance(port, LangChainAdapter)


def test_resolve_adapter_raises_for_unknown_adapter():
    """resolve_adapter raises ValueError for unknown adapter name."""
    worker_def = SimpleNamespace(adapter="unknown")
    secret = SimpleNamespace(api_key="sk-fake")
    with pytest.raises(ValueError, match="Unknown adapter"):
        resolve_adapter(worker_definition=worker_def, secret=secret)
