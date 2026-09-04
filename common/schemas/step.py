"""First-class step definition blueprints (capability catalog entries)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from common.schemas.saga import (
    DEFAULT_AGENT_ADAPTER,
    DEFAULT_MAX_TURNS,
    MAX_TURNS_LIMIT,
    AgentAdapterMode,
    ResourcesSpec,
    SkillsSpec,
    StepFactsExtractor,
    ToolsSpec,
    simple_agent_adapter_constraints,
)

StepKind: TypeAlias = Literal["reason", "commit"]


class StepInputSpec(BaseModel):
    """One declared input port on a step definition.

    ``schema`` is an optional Draft-7 JSON Schema fragment. At step deploy it must be
    valid/supported; at saga deploy ``value:`` literals are checked; at schedule time
    resolved ``with`` values are checked. Port *names* (required/unknown) are always
    enforced when hydrating a ``use:`` ref into a runtime step.
    """

    model_config = ConfigDict(extra="forbid")

    required: bool = True
    description: str | None = None
    schema_: dict[str, Any] | None = Field(
        default=None,
        alias="schema",
        description="Optional JSON Schema fragment for this port (enforced at deploy/schedule).",
    )


class _StepBlueprintShared(BaseModel):
    """Fields shared by reason and commit catalog steps (no saga JSONPath / when / id)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    kind: Literal["step"] = "step"
    name: str
    namespace: str = "default"
    version: str
    description: str | None = None
    title: str | None = Field(
        default=None,
        description="Human-readable label for operators; defaults to name when omitted.",
    )
    inputs: dict[str, StepInputSpec] = Field(default_factory=dict)

    worker: str
    worker_version: str
    compensation: str | None = None
    timeout_seconds: int = 600
    output_schema: str | None = None
    policy: str | None = None
    hitl: bool = False
    hitl_max_retries: int | None = Field(default=None, ge=0)
    hitl_retry_guidance: str | None = Field(default=None, max_length=4096)
    resources: ResourcesSpec | None = None

    @field_validator("name", "version", "worker", "worker_version")
    @classmethod
    def non_empty_identity(cls, v: str) -> str:
        stripped = (v or "").strip()
        if not stripped:
            raise ValueError("must be non-empty")
        return stripped

    @field_validator("inputs")
    @classmethod
    def input_keys_non_empty(cls, v: dict[str, StepInputSpec]) -> dict[str, StepInputSpec]:
        for key in v:
            if not str(key).strip():
                raise ValueError("inputs keys must be non-empty")
        return v

    @model_validator(mode="after")
    def hitl_retry_fields_require_hitl(self) -> _StepBlueprintShared:
        if not self.hitl and (
            self.hitl_max_retries is not None or self.hitl_retry_guidance is not None
        ):
            raise ValueError("hitl_max_retries and hitl_retry_guidance require hitl: true")
        return self

    def display_title(self) -> str:
        """Operator-facing step name used when hydrating into a saga body."""
        if self.title and str(self.title).strip():
            return str(self.title).strip()
        return self.name

    def _shared_capability_dict(self, *, step_kind: StepKind) -> dict[str, Any]:
        """Capability fields for a hydrated runtime node.

        Catalog YAML uses ``step_kind`` under ``kind: step``. Hydration maps that onto
        the executable saga step's ``kind`` (``reason`` | ``commit``) so runtime
        discriminators stay a single ``kind`` field.
        """
        data: dict[str, Any] = {
            "kind": step_kind,
            "worker": self.worker,
            "worker_version": self.worker_version,
            "timeout_seconds": self.timeout_seconds,
            "hitl": self.hitl,
            "step_definition_name": self.name,
            "step_definition_version": self.version,
            "inputs": {
                key: spec.model_dump(by_alias=True, exclude_none=True)
                for key, spec in self.inputs.items()
            },
        }
        if self.compensation is not None:
            data["compensation"] = self.compensation
        if self.output_schema is not None:
            data["output_schema"] = self.output_schema
        if self.policy is not None:
            data["policy"] = self.policy
        if self.hitl_max_retries is not None:
            data["hitl_max_retries"] = self.hitl_max_retries
        if self.hitl_retry_guidance is not None:
            data["hitl_retry_guidance"] = self.hitl_retry_guidance
        if self.resources is not None:
            data["resources"] = self.resources.model_dump(by_alias=True, exclude_none=True)
        return data


class ReasonStepBlueprint(_StepBlueprintShared):
    """Catalog reason step: LLM capability with optional tools / facts / skills.

    YAML: ``kind: step`` + ``step_kind: reason``. Hydrates to a runtime node with
    ``kind: reason`` (see :meth:`_StepBlueprintShared._shared_capability_dict`).
    """

    step_kind: Literal["reason"]
    prompt: str
    agent_adapter: AgentAdapterMode = Field(
        default=DEFAULT_AGENT_ADAPTER,
        alias="agent-adapter",
    )
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1, le=MAX_TURNS_LIMIT)
    max_step_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    tools: ToolsSpec | None = None
    skills: SkillsSpec | None = None
    facts: list[StepFactsExtractor] | None = None

    @model_validator(mode="after")
    def validate_reason_capability(self) -> ReasonStepBlueprint:
        _assert_reason_prompt(self.prompt)
        _assert_unique_facts_into(self.facts)
        _assert_simple_adapter_ok(self)
        _assert_no_load_skill_in_allow(self.tools)
        _assert_tools_bind_subset_of_inputs(self)
        return self

    def to_capability_dict(self) -> dict[str, Any]:
        """Fields for a hydrated reason saga step (no id/with/when)."""
        data = self._shared_capability_dict(step_kind="reason")
        data["prompt"] = self.prompt
        data["agent-adapter"] = self.agent_adapter
        data["max_turns"] = self.max_turns
        if self.max_step_tokens is not None:
            data["max_step_tokens"] = self.max_step_tokens
        if self.max_completion_tokens is not None:
            data["max_completion_tokens"] = self.max_completion_tokens
        if self.tools is not None:
            data["tools"] = self.tools.model_dump(by_alias=True, exclude_none=True)
        if self.skills is not None:
            data["skills"] = self.skills.model_dump(by_alias=True, exclude_none=True)
        if self.facts is not None:
            data["facts"] = [f.model_dump(by_alias=True, exclude_none=True) for f in self.facts]
        return data


def _assert_reason_prompt(prompt: str) -> None:
    if not str(prompt).strip():
        raise ValueError("reason steps require a non-empty prompt (prompt template ref)")


def _assert_unique_facts_into(facts: list[StepFactsExtractor] | None) -> None:
    if not facts:
        return
    into_keys = [spec.into for spec in facts]
    if len(into_keys) != len(set(into_keys)):
        raise ValueError("facts extractors must have unique 'into' keys per step")


def _assert_simple_adapter_ok(step: ReasonStepBlueprint) -> None:
    if step.agent_adapter != "simple":
        return
    err = simple_agent_adapter_constraints(
        tools=step.tools,
        resources=step.resources,
        skills=step.skills,
        facts=step.facts,
    )
    if err is not None:
        raise ValueError(err)


def _assert_no_load_skill_in_allow(tools: ToolsSpec | None) -> None:
    allow = tools.allow if tools else []
    for tool in allow:
        if tool.name == "load_skill":
            raise ValueError("tools.allow must not include reserved virtual tool name 'load_skill'")


def _assert_tools_bind_subset_of_inputs(step: ReasonStepBlueprint) -> None:
    bind = step.tools.bind if step.tools else []
    if not bind:
        return
    input_keys = set(step.inputs.keys())
    missing = [key for key in bind if key not in input_keys]
    if missing:
        raise ValueError(
            "tools.bind keys must also be declared in step inputs: "
            + ", ".join(repr(k) for k in missing)
        )


class CommitStepBlueprint(_StepBlueprintShared):
    """Catalog commit step: exactly one MCP tool; no LLM / agent-adapter fields.

    YAML: ``kind: step`` + ``step_kind: commit``. Hydrates to a runtime node with
    ``kind: commit``.
    """

    step_kind: Literal["commit"]
    tools: ToolsSpec

    @model_validator(mode="after")
    def validate_commit_capability(self) -> CommitStepBlueprint:
        if len(self.tools.allow) != 1:
            raise ValueError("commit steps require exactly one tool in tools.allow")
        if self.tools.allow[0].name == "load_skill":
            raise ValueError("tools.allow must not include reserved virtual tool name 'load_skill'")
        if self.tools.bind:
            raise ValueError("tools.bind is not supported on commit steps")
        return self

    def to_capability_dict(self) -> dict[str, Any]:
        """Fields for a hydrated commit saga step (no id/with/when)."""
        data = self._shared_capability_dict(step_kind="commit")
        data["tools"] = self.tools.model_dump(by_alias=True, exclude_none=True)
        return data


StepBlueprint: TypeAlias = Annotated[
    ReasonStepBlueprint | CommitStepBlueprint,
    Field(discriminator="step_kind"),
]

_STEP_BLUEPRINT_ADAPTER: TypeAdapter[ReasonStepBlueprint | CommitStepBlueprint] = TypeAdapter(
    StepBlueprint
)


def parse_step_blueprint(data: Any) -> ReasonStepBlueprint | CommitStepBlueprint:
    """Validate a catalog step mapping (YAML/JSON) into a discriminated blueprint."""
    return _STEP_BLUEPRINT_ADAPTER.validate_python(data)
