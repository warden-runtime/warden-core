"""Policy gate types shared by OSS kernel (no ledger envelope dependency)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PolicyPhase = Literal["after_reason", "before_commit"]
PolicyDenialCode = Literal["POLICY_REASON_DENIED", "POLICY_COMMIT_DENIED"]


class PolicyEvaluatedPayload(BaseModel):
    policy_name: str
    policy_version: str
    artifact_source_hash: str
    phase: PolicyPhase
    binding_hash: str

    model_config = ConfigDict(extra="forbid")


class PolicyFailedPayload(BaseModel):
    policy_name: str
    policy_version: str
    artifact_source_hash: str
    phase: PolicyPhase
    binding_hash: str
    denial_code: PolicyDenialCode

    model_config = ConfigDict(extra="forbid")


class PolicyErroredPayload(BaseModel):
    phase: PolicyPhase
    error_code: str
    message: str = Field(max_length=512)
    policy_name: str | None = None

    model_config = ConfigDict(extra="forbid")
