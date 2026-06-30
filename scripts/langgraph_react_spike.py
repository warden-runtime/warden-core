"""
LangGraph ReAct spike: explore create_react_agent API, state shape, and _submit extraction.

Run with: uv run python scripts/langgraph_react_spike.py

Requires OPENAI_API_KEY for a real run. Without it, uses a mock LLM that returns
predetermined tool calls so we can still inspect state structure.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from typing import Any

# LangGraph and LangChain (spike is self-contained; no workers/engine imports)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

# Optional: real LLM for full exploration
try:
    from langchain_openai import ChatOpenAI
except ImportError:
    ChatOpenAI = None  # type: ignore[misc, assignment]


def _create_llm():
    """Use ChatOpenAI if OPENAI_API_KEY is set; otherwise a mock that returns tool calls."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key and ChatOpenAI is not None:
        return ChatOpenAI(model="gpt-4o-mini", temperature=0, api_key=api_key)

    # Mock: minimal chat model that returns one AIMessage with _submit tool_call then content-only
    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    def _make_result(call_count: int) -> ChatResult:
        if call_count == 1:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "_submit",
                                    "args": {"result": {"answer": "42", "done": True}},
                                    "id": "call_submit_1",
                                }
                            ],
                        )
                    )
                ]
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Done."))])

    class _MockReActModel(BaseChatModel):
        """Returns first a _submit tool call, then a final content message."""

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: CallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            call_count = getattr(self, "_call_count", 0)
            self._call_count = call_count + 1
            return _make_result(self._call_count)

        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            call_count = getattr(self, "_call_count", 0)
            self._call_count = call_count + 1
            return _make_result(self._call_count)

        def bind_tools(
            self,
            tools: Sequence[Any],
            **kwargs: Any,
        ) -> _MockReActModel:
            """Return self; mock ignores bound tools and returns fixed responses."""
            return self

        @property
        def _llm_type(self) -> str:
            return "mock_react"

    return _MockReActModel()


class SubmitArgs(BaseModel):
    """Schema for _submit tool (spike: small structured payload)."""

    result: dict[str, Any] = Field(default_factory=dict, description="Final structured result")


def ping() -> str:
    """No-op tool: returns pong."""
    return "pong"


def submit_result(result: dict[str, Any]) -> str:
    """Virtual _submit tool: agent calls this with the final structured output."""
    return "Submitted"


def _build_tools():
    ping_tool = StructuredTool.from_function(
        func=ping,
        name="ping",
        description="No-op tool; returns pong.",
    )
    submit_tool = StructuredTool.from_function(
        func=submit_result,
        name="_submit",
        description="Call exactly once when the task is complete with the final structured result.",
        args_schema=SubmitArgs,
    )
    return [ping_tool, submit_tool]


def _inspect_state(state: dict[str, Any]) -> None:
    """Print state structure and how to detect _submit and get its arguments."""
    print("\n--- State keys ---")
    print(list(state.keys()))

    messages = state.get("messages", [])
    print("\n--- Message count ---")
    print(len(messages))

    print("\n--- Message types and order ---")
    for i, msg in enumerate(messages):
        typ = type(msg).__name__
        content_preview = ""
        tool_calls_preview = ""
        if hasattr(msg, "content"):
            c = msg.content
            content_preview = (str(c)[:60] + "..") if c and len(str(c)) > 60 else str(c) or ""
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            names = [
                tc.get("name", tc) if isinstance(tc, dict) else getattr(tc, "name", tc)
                for tc in msg.tool_calls
            ]
            tool_calls_preview = f" tool_calls={names}"
        print(f"  [{i}] {typ}: content={content_preview!r}{tool_calls_preview}")

    # _submit detection: find AIMessage with tool_calls containing _submit, get args from there
    submit_args = None
    for msg in reversed(messages):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                if name == "_submit":
                    submit_args = (
                        tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    )
                    break
        if submit_args is not None:
            break

    print("\n--- _submit detection (from last AIMessage with tool_calls containing _submit) ---")
    print(
        "Extracted _submit args:", json.dumps(submit_args, default=str) if submit_args else "None"
    )


async def main() -> None:
    from langgraph.prebuilt import create_react_agent

    llm = _create_llm()
    tools = _build_tools()
    agent = create_react_agent(llm, tools)

    initial_messages: list[BaseMessage] = [
        HumanMessage(
            content='Say hello, then call _submit with result = { "answer": "hello", "done": true }.'
        ),
    ]

    config = {"recursion_limit": 10}
    print('Input state shape: {"messages": [...]}')
    print("Config:", config)
    print("\nInvoking agent.ainvoke(...)")

    state = await agent.ainvoke({"messages": initial_messages}, config=config)

    print("\n--- Final state (after ainvoke) ---")
    _inspect_state(state)


async def experiment_tool_exception() -> None:
    """Run this to answer: If a tool raises, does the exception propagate to ainvoke caller?"""
    from langchain_core.tools import tool

    @tool
    def bad_tool() -> str:
        """Tool that raises an exception (for spike: test if it propagates)."""
        raise ValueError("Tool failed on purpose")

    from langgraph.prebuilt import create_react_agent

    llm = _create_llm()
    agent = create_react_agent(llm, [bad_tool])
    try:
        await agent.ainvoke(
            {"messages": [HumanMessage(content="Call the bad_tool.")]},
            config={"recursion_limit": 5},
        )
        print("Result: no exception (LangGraph caught it)")
    except Exception as e:
        print("Result: exception propagated:", type(e).__name__, str(e))


async def experiment_recursion_limit() -> None:
    """Run with recursion_limit=2 and inspect how many messages; answers what recursion_limit counts."""
    from langgraph.prebuilt import create_react_agent

    llm = _create_llm()
    tools = _build_tools()
    agent = create_react_agent(llm, tools)
    state = await agent.ainvoke(
        {"messages": [HumanMessage(content="Call ping, then _submit with result={}.")]},
        config={"recursion_limit": 2},
    )
    print("recursion_limit=2 -> message count:", len(state.get("messages", [])))
    _inspect_state(state)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "tool_exception":
        asyncio.run(experiment_tool_exception())
    elif len(sys.argv) > 1 and sys.argv[1] == "recursion_limit":
        asyncio.run(experiment_recursion_limit())
    else:
        asyncio.run(main())
