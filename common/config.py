"""
Application configuration loaded from .env and environment.

Single source of truth for DB URL, topics, engine API, prompts root,
and telemetry. Env vars override .env file. Use get_settings() for access.
"""

from functools import lru_cache
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App config: DB, topics, engine API, prompts, telemetry. Load from .env + env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = Field(
        default="postgres://admin:password@127.0.0.1:5432/engine_db",
        description="PostgreSQL URL for Tortoise; set in production.",
    )
    topic_orchestrator_events: str = Field(
        default="engine-events",
        validation_alias="ENGINE_EVENTS_TOPIC",
        description="Topic for orchestrator/engine events.",
    )
    topic_worker_commands: str = Field(
        default="worker-commands",
        validation_alias="WORKER_COMMANDS_TOPIC",
        description="Topic for worker commands.",
    )
    engine_api_host: str = Field(default="0.0.0.0", description="Engine API bind host.")
    engine_api_port: int = Field(default=8000, description="Engine API bind port.")
    engine_url: str | None = Field(
        default=None,
        description="Engine API base URL (e.g. http://localhost:8000). Required for the warden CLI.",
    )
    prompts_root: str | None = Field(
        default=None,
        validation_alias="PROMPTS_ROOT",
        description="Base directory for file-based prompt templates; required when steps use prompt_ref.",
    )

    @field_validator("prompts_root", mode="before")
    @classmethod
    def _coerce_empty_prompts_root(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    policies_root: str | None = Field(
        default=None,
        validation_alias="POLICIES_ROOT",
        description="Base directory for policy YAML files (CEL); required when a commit step sets policy.",
    )
    schemas_root: str | None = Field(
        default=None,
        validation_alias="SCHEMAS_ROOT",
        description="Base directory for step output_schema JSON file paths; required when a step sets output_schema.",
    )
    compensations_root: str | None = Field(
        default=None,
        validation_alias="COMPENSATIONS_ROOT",
        description="Base directory for compensation YAML file paths; required when a step sets compensation.",
    )
    env: str = Field(
        default="development",
        description="Deployment environment (e.g. development, production).",
    )
    otlp_endpoint: str | None = Field(
        default=None,
        validation_alias="OTLP_ENDPOINT",
        description=(
            "OTLP gRPC endpoint for traces (e.g. http://localhost:4317). "
            "When unset, the OpenTelemetry SDK default applies (including OTEL_EXPORTER_OTLP_ENDPOINT)."
        ),
    )
    otlp_insecure: bool = Field(
        default=True,
        validation_alias="OTLP_INSECURE",
        description="Use insecure gRPC channel for OTLP when true.",
    )
    log_pretty_json: bool = Field(
        default=False,
        validation_alias="WARDEN_LOG_PRETTY_JSON",
        description="When true, engine logs multi-line indented JSON for payloads and saga context.",
    )
    processed_command_stale_claim_seconds: int = Field(
        default=1800,
        ge=60,
        validation_alias="WORKER_STALE_CLAIM_SECONDS",
        description=(
            "Claims with result_emitted=False older than this are reaped so worker "
            "commands can be redelivered after a crash before emit."
        ),
    )
    processed_command_reap_interval_seconds: int = Field(
        default=60,
        ge=5,
        validation_alias="WORKER_CLAIM_REAP_INTERVAL_SECONDS",
        description="Background interval for batch reaping stale ProcessedCommand rows.",
    )
    outbox_stale_in_progress_seconds: int = Field(
        default=1800,
        ge=60,
        validation_alias="OUTBOX_STALE_IN_PROGRESS_SECONDS",
        description=(
            "Outbox rows IN_PROGRESS older than this are reset to PENDING for redelivery."
        ),
    )
    outbox_reap_interval_seconds: int = Field(
        default=60,
        ge=5,
        validation_alias="OUTBOX_REAP_INTERVAL_SECONDS",
        description="Background interval for reaping stale IN_PROGRESS outbox rows.",
    )
    outbox_reap_batch_size: int = Field(
        default=20,
        ge=1,
        le=500,
        validation_alias="OUTBOX_REAP_BATCH_SIZE",
        description="Max outbox rows reaped per maintenance tick per topic.",
    )
    worker_max_in_flight: int = Field(
        default=1,
        ge=1,
        validation_alias="WORKER_MAX_IN_FLIGHT",
        description="Max concurrent worker commands per process (Postgres consumer semaphore).",
    )
    hitl_review_timeout_seconds: int = Field(
        default=86400,
        ge=60,
        validation_alias="HITL_REVIEW_TIMEOUT_SECONDS",
        description="Max seconds a step may remain AWAITING_HUMAN before hitl.expired cleanup.",
    )
    outbox_max_payload_bytes: int = Field(
        default=262_144,
        ge=1024,
        validation_alias="OUTBOX_MAX_PAYLOAD_BYTES",
        description="Max JSON byte size for worker-commands outbox payloads before reject.",
    )
    llm_retry_enabled: bool = Field(
        default=True,
        validation_alias="WARDEN_LLM_RETRY_ENABLED",
        description="When true, worker LLM ainvoke calls use exponential backoff with jitter.",
    )
    llm_retry_max_attempts: int = Field(
        default=3,
        ge=1,
        validation_alias="WARDEN_LLM_RETRY_MAX_ATTEMPTS",
        description="Max LLM ainvoke attempts per call (including the first).",
    )
    llm_retry_base_delay_s: float = Field(
        default=1.0,
        gt=0,
        validation_alias="WARDEN_LLM_RETRY_BASE_DELAY_S",
        description="Base delay (seconds) for LLM retry exponential backoff.",
    )
    llm_retry_max_delay_s: float = Field(
        default=60.0,
        gt=0,
        validation_alias="WARDEN_LLM_RETRY_MAX_DELAY_S",
        description="Max delay cap (seconds) for LLM retry backoff before jitter.",
    )

    @model_validator(mode="after")
    def _validate_llm_retry_delays(self) -> Self:
        if self.llm_retry_max_delay_s < self.llm_retry_base_delay_s:
            msg = "llm_retry_max_delay_s must be >= llm_retry_base_delay_s"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached app settings (loaded from .env and env on first call)."""
    return Settings()
