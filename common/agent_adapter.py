"""
Port and DTOs for agent adapters. No LangChain, MCP, or ADK imports.

Adapter contract (any run_step implementation must satisfy):
- Allowlist: every tool name that appears in the execution state (e.g. in tool_calls)
  must be in the step's allowed tools (from tool_specs) or the designated submit tool
  (e.g. _submit). Reject with ExecutionStepError (disallowed_tools, allowed_tools) otherwise.
- Final output: reason steps emit ``{ \"data\": <business dict>, \"facts\": <dict>? }`` when
  tool-facts extractors are configured; commit steps emit ``{ \"data\": <MCP JSON> }`` only.
  Reason-step reasoning audit
  is recorded via ``AdapterHooks.after_reason_step`` at the adapter boundary, not on ``StepResult``.
- output_schema: validates the **inner** business object (the ``data`` value), enforced by the
  adapter and again in the engine on ingest.
- Tool failures: if any tool execution indicates failure (e.g. via return content or exception),
  fail the step with ExecutionStepError so the worker reports STEP_FAILED.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from common.resource_specs import ResourceSpec

AgentAdapterMode = Literal["react", "simple"]


class StepResult(BaseModel):
    """Result of run_step: envelope with ``data`` and optional tool-derived ``facts``."""

    output: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class CompensationResult(BaseModel):
    """Result of run_compensation: output payload from the compensation flow."""

    output: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class ExecutionStepError(Exception):
    """Raised by adapters when run_step or run_compensation fails; handler uses it for STEP_FAILED."""

    def __init__(
        self, message: str, tool: str | None = None, error_details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.tool = tool
        self.error_details = error_details or {"error": message, "tool": tool or "unknown"}


class AgentAdapterPort(ABC):
    """
    Port for agent adapters. Implementations run a single agentic step (LLM with
    tools, execute tool_calls through governance, repeat until done or max turns)
    and compensation; every tool invocation must go through governance.
    """

    @abstractmethod
    async def run_step(
        self,
        *,
        system_prompt: str,
        prompt_template: str,
        arguments: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        max_turns: int | None = None,
        max_step_tokens: int | None = None,
        facts_extractors: list[dict[str, Any]] | None = None,
        agent_adapter: AgentAdapterMode = "react",
    ) -> StepResult:
        """Run agentic loop: LLM with tools, execute tool_calls through governance, repeat until done.

        Implements the adapter contract: allowlist on every tool name in state, final output
        via submit tool or fallback, output_schema validation when set, tool failure detection.

        Args:
            system_prompt: System prompt for the model.
            prompt_template: User prompt template (e.g. Jinja); filled with arguments.
            arguments: Resolved arguments for the template.
            tool_specs: List of tool dicts (name, optional strict_schema/output_schema); used as allowlist.
            resource_specs: Optional list of resource dicts (uri, optional metadata).
            context: Optional extra context (e.g. headers).
            output_schema: Optional step-level JSON Schema for final output; adapter validates payload when set.
            max_turns: Max LLM invocations for the ReAct loop; defaults to saga step YAML value.
            max_step_tokens: Optional accumulated provider total_tokens budget; None means unlimited.
            agent_adapter: ``react`` for ReAct + _submit; ``simple`` for single structured turn.

        Returns:

        Raises:
            ExecutionStepError: When a tool call fails or governance rejects (e.g. tool not in allowlist).
        """
        ...

    @abstractmethod
    async def run_commit(
        self,
        *,
        arguments: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> StepResult:
        """Run a commit step: invoke exactly one governed MCP tool with arguments (no LLM).

        Args:
            arguments: Resolved kwargs for the single allowed tool.
            tool_specs: Exactly one tool dict (name, optional strict_schema/output_schema).
            resource_specs: Optional list of resource dicts (uri, optional metadata).
            context: Optional extra context (e.g. headers for MCP).
            output_schema: Optional step-level JSON Schema for the returned output dict.

        Returns:
            StepResult with output dict (adapter-defined shape, e.g. tool name + result).

        Raises:
            ExecutionStepError: When tool_specs is not exactly one tool, tool fails, or validation fails.
        """
        ...

    @abstractmethod
    async def run_compensation(
        self,
        *,
        compensation_prompt: str,
        original_input: dict[str, Any],
        step_output: dict[str, Any] | None,
        failure_reason: dict[str, Any] | None,
        context_snapshot: dict[str, Any],
        tool_specs: list[dict[str, Any]],
        resource_specs: list[ResourceSpec] | None = None,
        context: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        idempotency_key: str | None = None,
        max_turns: int | None = None,
    ) -> CompensationResult:
        """Run compensation (undo) flow with real tool execution.

        Args:
            compensation_prompt: Prompt for the compensation step.
            original_input: Resolved input that was used for the forward step.
            step_output: Output from the step being compensated (or None).
            failure_reason: Error details that triggered compensation.
            context_snapshot: Saga context at time of compensation.
            tool_specs: Tool allowlist for compensation.
            resource_specs: Optional resource allowlist for compensation.
            context: Optional extra context (e.g. headers).
            system_prompt: Frozen forward worker system prompt; defaults to worker row.
            idempotency_key: Command idempotency key injected into undo tool arguments.
            max_turns: Max LLM invocations for multi-tool compensation ReAct.

        Returns:
            CompensationResult with output payload.

        Raises:
            Exception: On LLM or tool failure (implementation-dependent).
        """
        ...
