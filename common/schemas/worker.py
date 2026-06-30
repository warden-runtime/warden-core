from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    MOCK = "mock"


class MCPServerConfig(BaseModel):
    name: str
    transport: str = "sse"  # "sse" or "stdio"
    url: str | None = None  # sse execution
    command: str | None = None  # local binary execution (stdio)
    args: list[str] = Field(default_factory=list)  # stdio: arguments to command
    cwd: str | None = None  # stdio: working directory for the process
    env: dict[str, str] | None = None  # stdio: environment variables for the process
    headers: dict[str, str] | None = None  # sse: HTTP headers (e.g. Authorization) for sse_client


class WorkerBlueprint(BaseModel):
    kind: Literal["worker"]
    name: str  # The unique slug used by Sagas (e.g., "fraud-analyst")
    namespace: str = "default"
    version: str
    description: str | None = None

    # Model Configuration
    provider: ModelProvider
    model_name: str  # e.g., "gpt-4o" or "llama-3-70b"
    temperature: float = 0.0

    # The "Soul" of the Agent
    system_prompt: str
    compensation_prompt: str | None = "You are in rollback mode. Undo the previous operation."

    tool_sources: list[MCPServerConfig] = Field(default_factory=list)
    adapter: str = "langchain"

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def validate_mock_model_name(self) -> Self:
        if self.provider == ModelProvider.MOCK and not str(self.model_name).strip():
            raise ValueError("mock provider requires a non-empty model_name (demo script label)")
        return self
