"""Configuration for warden-mcp."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load settings from environment and optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Allow Settings(engine_url=...) in tests while env still uses ENGINE_URL.
        populate_by_name=True,
    )

    engine_url: str = Field(
        default="http://127.0.0.1:8000",
        validation_alias=AliasChoices("ENGINE_URL", "WARDEN_MCP_ENGINE_URL"),
        description="Warden engine API base URL (http or https only).",
    )
    mcp_transport: Literal["stdio", "streamable-http"] = Field(
        default="stdio",
        validation_alias="WARDEN_MCP_TRANSPORT",
        description="MCP server transport.",
    )
    mcp_host: str = Field(
        default="127.0.0.1",
        validation_alias="WARDEN_MCP_HOST",
        description="Bind host for streamable-http transport.",
    )
    mcp_port: int = Field(
        default=8765,
        validation_alias="WARDEN_MCP_PORT",
        description="Bind port for streamable-http transport.",
    )
    http_timeout_s: float = Field(
        default=30.0,
        validation_alias="WARDEN_MCP_HTTP_TIMEOUT_S",
        description="Timeout for engine HTTP requests.",
    )
    manifest_max_body_bytes: int = Field(
        default=2_097_152,
        validation_alias="WARDEN_MCP_MANIFEST_MAX_BYTES",
        description="Maximum manifest yaml_content size before POST (default 2 MiB).",
    )

    @field_validator("engine_url")
    @classmethod
    def _validate_engine_url(cls, value: str) -> str:
        parsed = HttpUrl(value)
        if parsed.scheme not in ("http", "https"):
            msg = "engine_url must use http or https"
            raise ValueError(msg)
        return str(parsed).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
