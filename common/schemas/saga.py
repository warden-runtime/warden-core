from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SagaStepKind: TypeAlias = Literal["reason", "commit"]
SAGA_STEP_KINDS: frozenset[str] = frozenset({"reason", "commit"})
AgentAdapterMode: TypeAlias = Literal["react", "simple"]
DEFAULT_AGENT_ADAPTER: AgentAdapterMode = "react"

DEFAULT_MAX_TURNS = 10
MAX_TURNS_LIMIT = 200


class StepParameterSpec(BaseModel):
    """
    One entry in a step's `with` map: either pull from saga context via JSONPath
    or use a literal value.
    """

    model_config = ConfigDict(extra="forbid")

    from_path: str | None = Field(None, alias="from")
    value: Any | None = None

    @model_validator(mode="after")
    def exactly_one_of_from_or_value(self) -> "StepParameterSpec":
        # `from` alone must win over explicit JSON/YAML `value: null` (both keys present).
        from_provided = self.from_path is not None
        value_provided = "value" in self.model_fields_set and self.value is not None
        if from_provided == value_provided:
            raise ValueError("Exactly one of 'from' or 'value' is required")
        if self.from_path is not None and not self.from_path.startswith("$"):
            raise ValueError("'from' must be a JSONPath starting with '$'")
        return self


class Tool(BaseModel):
    """A step-level guardrail for a specific MCP tool."""

    name: str
    description: str | None = None
    strict_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class ToolsSpec(BaseModel):
    """Step-level tool allowlist with optional input/output schemas."""

    allow: list[Tool] = Field(default_factory=list)


class Resource(BaseModel):
    """A step-level allowlist entry for an MCP resource URI."""

    uri: str
    description: str | None = None


class ResourcesSpec(BaseModel):
    """Step-level resource allowlist."""

    allow: list[Resource] = Field(default_factory=list)


class StepWhenSpec(BaseModel):
    """Optional schedule gate: CEL evaluated against saga context before the step runs."""

    model_config = ConfigDict(extra="forbid")

    cel: str

    @field_validator("cel")
    @classmethod
    def cel_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("when.cel must be non-empty")
        return stripped


class StepFactsExtractor(BaseModel):
    """Extract structured facts from a named MCP tool result into saga context."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    into: str
    fields: dict[str, str]

    @field_validator("tool", "into")
    @classmethod
    def non_empty_identifier(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("tool and into must be non-empty")
        return stripped

    @field_validator("fields")
    @classmethod
    def fields_non_empty_and_jsonpath(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("facts.fields must contain at least one JSONPath mapping")
        for key, path in v.items():
            if not str(key).strip():
                raise ValueError("facts.fields keys must be non-empty")
            if not isinstance(path, str) or not path.strip().startswith("$"):
                raise ValueError(f"facts.fields[{key!r}] must be a JSONPath starting with '$'")
        return v


class CompensationStep(BaseModel):
    """Compensation (undo) definition for a saga step."""

    worker: str
    worker_version: str
    with_spec: dict[str, StepParameterSpec] = Field(default_factory=dict, alias="with")
    tools: ToolsSpec | None = None
    resources: ResourcesSpec | None = None
    timeout_seconds: int | None = None
    max_turns: int | None = Field(
        default=None,
        ge=1,
        le=MAX_TURNS_LIMIT,
        description=(
            "Override max LLM invocations for multi-tool compensation ReAct; "
            "omit to use the forward step max_turns."
        ),
    )

    model_config = ConfigDict(populate_by_name=True)


class _SagaStepBase(BaseModel):
    """Fields shared by reason and commit saga steps."""

    id: str
    name: str
    worker: str
    worker_version: str
    with_spec: dict[str, StepParameterSpec] = Field(default_factory=dict, alias="with")
    compensation: str | None = Field(
        default=None,
        description=(
            "Relative path under COMPENSATIONS_ROOT to a YAML file for the compensation (undo) block "
            "(worker, with, tools)."
        ),
    )
    compensation_definition: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Resolved compensation block (worker, with, tools). Populated at manifest "
            "registration; omit in author YAML."
        ),
    )
    timeout_seconds: int = 600
    max_turns: int = Field(
        default=DEFAULT_MAX_TURNS,
        ge=1,
        le=MAX_TURNS_LIMIT,
        description=(
            "Maximum LLM invocations in the ReAct loop for reason steps "
            "(and multi-tool compensation when not overridden in undo YAML)."
        ),
    )
    output_schema: str | None = Field(
        default=None,
        description=(
            "Relative path under SCHEMAS_ROOT to a JSON file: step output JSON Schema (Draft-7). "
            'Workers emit STEP_COMPLETED.output as {"data": <object>}; engine validates output.data. '
            "The resolved schema is not stored on the saga definition row."
        ),
    )
    policy: str | None = Field(
        default=None,
        description=(
            "Relative path under POLICIES_ROOT to a policy YAML file "
            "(e.g. github-issue-comment.yaml or team-a/gate.yaml). "
            "Legacy stem-only refs without .yaml still resolve via {ref}.yaml. "
            "The engine evaluates ``cel`` against a normalized binding with ``phase``, "
            "``input``, ``arguments``, ``output``, ``saga``, ``step``, ``worker``, "
            "and ``tool``. Reason step phase is ``after_reason``; commit step phase "
            "is ``before_commit``."
        ),
    )
    hitl: bool = Field(
        default=False,
        description=(
            "When true, pause for human approval at this step's safety boundary: "
            "after reason output, or before a commit tool is invoked."
        ),
    )
    hitl_max_retries: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Maximum manual HITL retries while the step is held for review. "
            "Omit or null for unlimited. Ignored when hitl is false."
        ),
    )
    hitl_retry_guidance: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Default operator guidance merged into worker arguments on each manual retry "
            "(under _hitl_retry.guidance). Per-request guidance from the retry API overrides this."
        ),
    )
    resources: ResourcesSpec | None = Field(
        default=None,
        description=(
            "Optional MCP resource allowlist for this step. URIs may be parameterized "
            "(for example, {customer_id}) and are stored/transported as literal strings "
            "in Phase 1-2."
        ),
    )
    when: StepWhenSpec | None = Field(
        default=None,
        description=(
            "Optional schedule gate. When set, the engine evaluates ``when.cel`` against "
            "``input``, ``steps``, ``saga``, and ``step`` before scheduling. False marks "
            "the step SKIPPED and continues; omitted means always eligible."
        ),
    )

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="after")
    def hitl_retry_fields_require_hitl(self) -> "_SagaStepBase":
        if not self.hitl and (
            self.hitl_max_retries is not None or self.hitl_retry_guidance is not None
        ):
            raise ValueError("hitl_max_retries and hitl_retry_guidance require hitl: true")
        return self


class ReasonSagaStep(_SagaStepBase):
    """LLM reasoning step: requires a prompt template ref; tools.allow is optional."""

    kind: Literal["reason"]
    prompt: str
    agent_adapter: AgentAdapterMode = Field(
        default=DEFAULT_AGENT_ADAPTER,
        alias="agent-adapter",
        description=(
            "Execution strategy: ``react`` runs a multi-turn ReAct loop ending in ``_submit``; "
            "``simple`` runs a single structured LLM turn with no MCP tools."
        ),
    )
    max_step_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional financial guardrail: abort the reason step when accumulated "
            "provider-reported total_tokens (prompt + completion) exceed this budget. "
            "Omit for unlimited (or fall back to WARDEN_MAX_STEP_TOKENS). "
            "Counts gross physical tokens, not cache-discounted billed tokens. "
            "Not applied during compensation."
        ),
    )
    max_completion_tokens: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional per-call generation cap passed to the LLM provider as max_tokens. "
            "Omit for no Warden override (or fall back to WARDEN_MAX_COMPLETION_TOKENS). "
            "Distinct from max_step_tokens (accumulated step budget). "
            "Not applied during compensation."
        ),
    )
    tools: ToolsSpec | None = None
    facts: list[StepFactsExtractor] | None = Field(
        default=None,
        description=(
            "Optional tool-result extractors. Worker parses MCP JSON into "
            "``steps.<step_id>.facts`` for engine CEL / when gates."
        ),
    )

    @model_validator(mode="after")
    def validate_prompt_non_empty(self) -> "ReasonSagaStep":
        if not str(self.prompt).strip():
            raise ValueError("reason steps require a non-empty prompt (prompt template ref)")
        return self

    @model_validator(mode="after")
    def validate_unique_facts_into(self) -> "ReasonSagaStep":
        if not self.facts:
            return self
        into_keys = [spec.into for spec in self.facts]
        if len(into_keys) != len(set(into_keys)):
            raise ValueError("facts extractors must have unique 'into' keys per step")
        return self

    @model_validator(mode="after")
    def validate_simple_agent_adapter_constraints(self) -> "ReasonSagaStep":
        if self.agent_adapter != "simple":
            return self
        tools = self.tools.allow if self.tools else []
        if tools:
            raise ValueError("simple agent-adapter requires an empty tools.allow")
        resources = self.resources.allow if self.resources else []
        if resources:
            raise ValueError("simple agent-adapter requires an empty resources.allow")
        if self.facts:
            raise ValueError("facts require tool results; incompatible with simple agent-adapter")
        return self


class CommitSagaStep(_SagaStepBase):
    """Deterministic MCP commit step: exactly one tool in tools.allow."""

    kind: Literal["commit"]
    tools: ToolsSpec

    @model_validator(mode="after")
    def validate_exactly_one_tool(self) -> "CommitSagaStep":
        if len(self.tools.allow) != 1:
            raise ValueError("commit steps require exactly one tool in tools.allow")
        return self


class LoopUntilSpec(BaseModel):
    """Exit condition for a loop block: evaluated after each successful body pass."""

    model_config = ConfigDict(extra="forbid")

    cel: str

    @field_validator("cel")
    @classmethod
    def cel_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("until.cel must be non-empty")
        return stripped


class LoopSagaStep(BaseModel):
    """Bounded do-while loop over nested reason/commit steps (single nesting level)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["loop"]
    id: str
    name: str | None = None
    max_iterations: int = Field(..., ge=1, description="Hard ceiling on body passes; required.")
    until: LoopUntilSpec
    steps: list[Annotated[ReasonSagaStep | CommitSagaStep, Field(discriminator="kind")]]

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("loop id must be non-empty")
        return stripped

    @field_validator("steps")
    @classmethod
    def body_non_empty(
        cls, v: list[ReasonSagaStep | CommitSagaStep]
    ) -> list[ReasonSagaStep | CommitSagaStep]:
        if not v:
            raise ValueError("loop body steps must be non-empty")
        return v


# Executable forward steps (reason/commit). Loop containers are top-level only.
SagaStep = Annotated[
    ReasonSagaStep | CommitSagaStep,
    Field(discriminator="kind"),
]

TopLevelSagaStep = Annotated[
    ReasonSagaStep | CommitSagaStep | LoopSagaStep,
    Field(discriminator="kind"),
]


def iter_executable_step_ids(steps: list[TopLevelSagaStep]) -> list[str]:
    """Collect reason/commit step ids across top-level steps and loop bodies."""
    ids: list[str] = []
    for step in steps:
        if isinstance(step, LoopSagaStep):
            ids.extend(body.id for body in step.steps)
        else:
            ids.append(step.id)
    return ids


def iter_loop_ids(steps: list[TopLevelSagaStep]) -> list[str]:
    """Collect loop container ids from a blueprint steps list."""
    return [step.id for step in steps if isinstance(step, LoopSagaStep)]


class SagaBlueprint(BaseModel):
    """Root schema for a saga definition (YAML)."""

    kind: Literal["saga"] = "saga"
    name: str
    namespace: str = "default"
    version: str
    description: str
    steps: list[TopLevelSagaStep]

    @field_validator("steps")
    @classmethod
    def ensure_unique_ids_and_no_nested_loops(
        cls, v: list[TopLevelSagaStep]
    ) -> list[TopLevelSagaStep]:
        executable_ids = iter_executable_step_ids(v)
        if len(executable_ids) != len(set(executable_ids)):
            raise ValueError("All step IDs in a blueprint must be unique.")
        loop_ids = iter_loop_ids(v)
        if len(loop_ids) != len(set(loop_ids)):
            raise ValueError("All loop IDs in a blueprint must be unique.")
        overlap = set(executable_ids) & set(loop_ids)
        if overlap:
            raise ValueError(f"Loop IDs must not collide with step IDs: {sorted(overlap)}")
        return v
