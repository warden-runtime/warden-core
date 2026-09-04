from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class ModelProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    AZURE = "azure"
    LOCAL = "local"
    MOCK = "mock"


class MCPServerConfig(BaseModel):
    name: str
    transport: Literal["streamable_http", "stdio"] = "streamable_http"
    url: str | None = None  # streamable_http: MCP HTTP endpoint
    command: str | None = None  # stdio: local binary execution
    args: list[str] = Field(default_factory=list)  # stdio: arguments to command
    cwd: str | None = None  # stdio: working directory for the process
    env: dict[str, str] | None = None  # stdio: environment variables for the process
    headers: dict[str, str] | None = None  # streamable_http: HTTP headers (e.g. Authorization)

    @field_validator("transport", mode="before")
    @classmethod
    def reject_legacy_sse(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() == "sse":
            raise ValueError("transport 'sse' was removed; use 'streamable_http'")
        return value

    @model_validator(mode="after")
    def validate_transport_fields(self) -> Self:
        if self.transport == "streamable_http":
            if not (self.url and str(self.url).strip()):
                raise ValueError("streamable_http transport requires a non-empty url")
        elif self.transport == "stdio":
            if not (self.command and str(self.command).strip()):
                raise ValueError("stdio transport requires a non-empty command")
        return self


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

    tool_sources: list[MCPServerConfig] = Field(default_factory=list)
    adapter: str = "langchain"

    model_config = ConfigDict(use_enum_values=True)

    @model_validator(mode="after")
    def validate_mock_model_name(self) -> Self:
        if self.provider == ModelProvider.MOCK and not str(self.model_name).strip():
            raise ValueError("mock provider requires a non-empty model_name (demo script label)")
        return self


_WORKER_BLUEPRINT_ADAPTER: TypeAdapter[WorkerBlueprint] = TypeAdapter(WorkerBlueprint)


def parse_worker_blueprint(data: Any) -> WorkerBlueprint:
    """Validate a worker mapping (YAML/JSON / stored body) into a WorkerBlueprint."""
    return _WORKER_BLUEPRINT_ADAPTER.validate_python(data)
