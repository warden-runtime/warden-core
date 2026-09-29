"""Post-failure hold strategy shared by catalog steps and saga runtime steps."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

OnFailureStrategy: TypeAlias = Literal["auto_compensate", "await_operator"]
DEFAULT_ON_FAILURE_STRATEGY: OnFailureStrategy = "auto_compensate"


class OnFailureSpec(BaseModel):
    """How the engine reacts after this forward step fails or times out."""

    model_config = ConfigDict(extra="forbid")

    strategy: OnFailureStrategy = Field(
        default=DEFAULT_ON_FAILURE_STRATEGY,
        description=(
            "auto_compensate: enter COMPENSATING (or FAILED if nothing to unwind). "
            "await_operator: hold the saga at AWAITING_RECOVERY until an operator "
            "retries the forward step or starts compensation."
        ),
    )
