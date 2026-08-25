from typing import Annotated, Any, Literal, TypeAlias

from jsonpath_ng import parse as parse_jsonpath
from jsonpath_ng.exceptions import JsonPathParserError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SagaStepKind: TypeAlias = Literal["reason", "commit", "spawn_sagas", "join_sagas"]
SAGA_STEP_KINDS: frozenset[str] = frozenset({"reason", "commit", "spawn_sagas", "join_sagas"})
WORKER_STEP_KINDS: frozenset[str] = frozenset({"reason", "commit"})
ENGINE_NATIVE_STEP_KINDS: frozenset[str] = frozenset({"spawn_sagas", "join_sagas"})
ENGINE_NATIVE_WORKER = ""
ENGINE_NATIVE_WORKER_VERSION = "0.0.0"
MAX_CHILDREN_HARD_CAP = 16
AgentAdapterMode: TypeAlias = Literal["react", "simple"]
DEFAULT_AGENT_ADAPTER: AgentAdapterMode = "react"

DEFAULT_MAX_TURNS = 10
MAX_TURNS_LIMIT = 200


def is_engine_native_kind(kind: str) -> bool:
    """True when the step kind is executed by the engine (no worker command)."""
    return kind in ENGINE_NATIVE_STEP_KINDS


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


class Skill(BaseModel):
    """A step-level allowlist entry for a worker-scoped skill on disk."""

    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def skill_name_non_empty(cls, v: str) -> str:
        stripped = (v or "").strip()
        if not stripped:
            raise ValueError("skill name must be non-empty")
        if stripped == "load_skill":
            raise ValueError("skill name 'load_skill' is reserved")
        return stripped


class SkillsSpec(BaseModel):
    """Step-level skill allowlist (refs under SKILLS_ROOT/<worker>/<name>.md)."""

    model_config = ConfigDict(extra="forbid")

    allow: list[Skill] = Field(default_factory=list)


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
    """Compensation (undo) definition: exactly one MCP tool, deterministic like a commit."""

    worker: str
    worker_version: str
    with_spec: dict[str, StepParameterSpec] = Field(default_factory=dict, alias="with")
    tools: ToolsSpec
    resources: ResourcesSpec | None = None

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="after")
    def validate_exactly_one_tool(self) -> "CompensationStep":
        if len(self.tools.allow) != 1:
            raise ValueError("compensation requires exactly one tool in tools.allow")
        if self.tools.allow[0].name == "load_skill":
            raise ValueError("tools.allow must not include reserved virtual tool name 'load_skill'")
        return self


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
        description="Maximum LLM invocations in the ReAct loop for reason steps.",
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
    skills: SkillsSpec | None = Field(
        default=None,
        description=(
            "Optional worker-scoped skill allowlist for react reason steps. "
            "Skill frontmatter allowed_tools union with tools.allow into the effective MCP allowlist."
        ),
    )
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
        skills = self.skills.allow if self.skills else []
        if skills:
            raise ValueError("simple agent-adapter requires an empty skills.allow")
        if self.facts:
            raise ValueError("facts require tool results; incompatible with simple agent-adapter")
        return self

    @model_validator(mode="after")
    def validate_tools_allow_rejects_load_skill(self) -> "ReasonSagaStep":
        tools = self.tools.allow if self.tools else []
        for tool in tools:
            if tool.name == "load_skill":
                raise ValueError(
                    "tools.allow must not include reserved virtual tool name 'load_skill'"
                )
        return self


class CommitSagaStep(_SagaStepBase):
    """Deterministic MCP commit step: exactly one tool in tools.allow."""

    kind: Literal["commit"]
    tools: ToolsSpec

    @model_validator(mode="after")
    def validate_exactly_one_tool(self) -> "CommitSagaStep":
        if len(self.tools.allow) != 1:
            raise ValueError("commit steps require exactly one tool in tools.allow")
        if self.tools.allow[0].name == "load_skill":
            raise ValueError("tools.allow must not include reserved virtual tool name 'load_skill'")
        return self


class SpawnSpec(BaseModel):
    """Fan-out configuration for a ``spawn_sagas`` step."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    saga_name: str
    saga_version: str
    items_from: str
    item_var: str = "item"
    result_from: str
    max_children: int | None = Field(default=None, ge=1, le=MAX_CHILDREN_HARD_CAP)
    input: dict[str, StepParameterSpec] = Field(default_factory=dict)

    @field_validator("items_from", "result_from")
    @classmethod
    def jsonpath_must_start_with_dollar(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped.startswith("$"):
            raise ValueError("JSONPath must start with '$'")
        return stripped

    @field_validator("item_var")
    @classmethod
    def item_var_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("item_var must be non-empty")
        return stripped

    @field_validator("saga_name", "saga_version")
    @classmethod
    def saga_ref_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("saga_name and saga_version must be non-empty")
        return stripped


class JoinSpec(BaseModel):
    """Wait-all barrier configuration for a ``join_sagas`` step."""

    model_config = ConfigDict(extra="forbid")

    spawn_step_id: str
    allow_zero_success: bool = True

    @field_validator("spawn_step_id")
    @classmethod
    def spawn_step_id_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("spawn_step_id must be non-empty")
        return stripped


class _EngineNativeStepBase(BaseModel):
    """Fields shared by engine-native spawn/join steps (no worker / tools / hitl)."""

    id: str
    name: str
    timeout_seconds: int = 600
    when: StepWhenSpec | None = None

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class SpawnSagasStep(_EngineNativeStepBase):
    """Engine-native fan-out: start child sagas for each item."""

    kind: Literal["spawn_sagas"]
    spawn: SpawnSpec


class JoinSagasStep(_EngineNativeStepBase):
    """Engine-native wait-all barrier over children of a spawn step."""

    kind: Literal["join_sagas"]
    join: JoinSpec


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


# Loop body steps stay reason/commit only.
SagaStep = Annotated[
    ReasonSagaStep | CommitSagaStep,
    Field(discriminator="kind"),
]

# Materializable forward steps (top-level executable + spawn/join).
ExecutableSagaStep = Annotated[
    ReasonSagaStep | CommitSagaStep | SpawnSagasStep | JoinSagasStep,
    Field(discriminator="kind"),
]

TopLevelSagaStep = Annotated[
    ReasonSagaStep | CommitSagaStep | LoopSagaStep | SpawnSagasStep | JoinSagasStep,
    Field(discriminator="kind"),
]


def iter_executable_step_ids(steps: list[TopLevelSagaStep]) -> list[str]:
    """Collect executable step ids across top-level steps and loop bodies."""
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


def _compile_jsonpath(path: str, *, step_id: str, field_name: str) -> None:
    try:
        parse_jsonpath(path)
    except JsonPathParserError as e:
        raise ValueError(f"spawn step {step_id!r} {field_name} is not a valid JSONPath: {e}") from e


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

    @model_validator(mode="after")
    def validate_spawn_join_wiring(self) -> "SagaBlueprint":
        spawn_ids: set[str] = set()
        joins: list[JoinSagasStep] = []
        for step in self.steps:
            if isinstance(step, SpawnSagasStep):
                spawn_ids.add(step.id)
                _compile_jsonpath(step.spawn.items_from, step_id=step.id, field_name="items_from")
                _compile_jsonpath(step.spawn.result_from, step_id=step.id, field_name="result_from")
            elif isinstance(step, JoinSagasStep):
                joins.append(step)

        seen_targets: set[str] = set()
        for join in joins:
            target = join.join.spawn_step_id
            if target not in spawn_ids:
                raise ValueError(
                    f"join step {join.id!r} references unknown spawn_step_id {target!r}"
                )
            if target in seen_targets:
                raise ValueError(f"spawn_step_id {target!r} is targeted by more than one join")
            seen_targets.add(target)
        return self
