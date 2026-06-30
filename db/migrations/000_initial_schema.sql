-- Warden OSS baseline schema (greenfield installs).

CREATE TABLE saga_definitions (
  id UUID PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  name VARCHAR(128) NOT NULL,
  version VARCHAR(50) NOT NULL DEFAULT '0.0.1',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  body JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT saga_definitions_namespace_name_version_uniq UNIQUE (namespace, name, version)
);

CREATE INDEX idx_saga_definitions_namespace ON saga_definitions (namespace);

CREATE TABLE saga_instances (
  trace_id VARCHAR(32) PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  definition_id VARCHAR(128) NOT NULL,
  status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  start_idempotency_key VARCHAR(256) NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT saga_instances_namespace_trace_id_uniq UNIQUE (namespace, trace_id),
  CONSTRAINT saga_instances_namespace_start_idempotency_key_uniq UNIQUE (namespace, start_idempotency_key)
);

CREATE INDEX idx_saga_instances_namespace ON saga_instances (namespace);
CREATE INDEX idx_saga_instances_started_at ON saga_instances (started_at);

CREATE TABLE saga_step_instances (
  span_id VARCHAR(16) PRIMARY KEY,
  saga_trace_id VARCHAR(32) NOT NULL,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  saga_id VARCHAR(32) NOT NULL REFERENCES saga_instances (trace_id) ON DELETE CASCADE,
  step_id VARCHAR(128) NOT NULL,
  step_name VARCHAR(128) NOT NULL,
  order_index INTEGER NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL UNIQUE,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  end_time TIMESTAMPTZ NULL,
  timeout_seconds INTEGER NOT NULL,
  max_turns INTEGER NOT NULL DEFAULT 10,
  status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
  worker VARCHAR(255) NOT NULL,
  worker_version VARCHAR(50) NOT NULL DEFAULT '1.0.0',
  step_kind VARCHAR(32) NOT NULL,
  agent_adapter VARCHAR(32) NOT NULL DEFAULT 'react',
  tools_allow JSONB NOT NULL DEFAULT '[]'::jsonb,
  resources_allow JSONB NOT NULL DEFAULT '[]'::jsonb,
  parameters_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  resolved_arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
  prompt_ref VARCHAR(512) NULL,
  output_payload JSONB NULL,
  error_details JSONB NULL,
  compensation_definition JSONB NULL,
  output_schema JSONB NULL,
  policy_name VARCHAR(128) NULL,
  hitl_required BOOLEAN NOT NULL DEFAULT FALSE,
  hitl_max_retries INTEGER NULL,
  hitl_retry_count INTEGER NOT NULL DEFAULT 0,
  hitl_retry_guidance TEXT NULL,
  hitl_review_started_at TIMESTAMPTZ NULL,
  pending_review_payload JSONB NULL,
  when_cel TEXT NULL,
  facts_extractors JSONB NOT NULL DEFAULT '[]'::jsonb,
  compensates_span_id VARCHAR(16) NULL,
  execution_timing JSONB NULL,
  pending_engine_timing JSONB NULL
);

CREATE INDEX idx_saga_step_instances_namespace ON saga_step_instances (namespace);
CREATE INDEX idx_saga_step_instances_started_at ON saga_step_instances (started_at);
CREATE INDEX idx_saga_step_instances_compensates_span_id ON saga_step_instances (compensates_span_id);
CREATE INDEX idx_hitl_review_expiry ON saga_step_instances (hitl_review_started_at)
  WHERE status = 'AWAITING_HUMAN' AND hitl_review_started_at IS NOT NULL;

CREATE TABLE worker_definitions (
  id UUID PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  name VARCHAR(128) NOT NULL,
  version VARCHAR(50) NOT NULL DEFAULT '1.0.0',
  model_provider VARCHAR(64) NOT NULL,
  model_name VARCHAR(128) NOT NULL,
  system_prompt TEXT NOT NULL,
  compensation_prompt TEXT NULL,
  tool_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
  adapter VARCHAR(32) NOT NULL DEFAULT 'langchain',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT worker_definitions_namespace_name_version_uniq UNIQUE (namespace, name, version)
);

CREATE INDEX idx_worker_definitions_namespace ON worker_definitions (namespace);

CREATE TABLE provider_secrets (
  id UUID PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  provider VARCHAR(64) NOT NULL,
  api_key VARCHAR(512) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT provider_secrets_namespace_provider_uniq UNIQUE (namespace, provider)
);

CREATE INDEX idx_provider_secrets_namespace ON provider_secrets (namespace);

CREATE TABLE outbox_events (
  id UUID PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  saga_trace_id VARCHAR(32) NOT NULL,
  step_span_id VARCHAR(16) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  destination_topic VARCHAR(255) NOT NULL,
  idempotency_key VARCHAR(256) NULL,
  trace_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload JSONB NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT outbox_events_namespace_destination_topic_idempotency_key_uniq
    UNIQUE (namespace, destination_topic, idempotency_key)
);

CREATE INDEX idx_outbox_events_namespace ON outbox_events (namespace);
CREATE INDEX idx_outbox_events_saga_trace_id ON outbox_events (saga_trace_id);
CREATE INDEX idx_outbox_events_destination_topic ON outbox_events (destination_topic);
CREATE INDEX idx_outbox_events_status ON outbox_events (status);
CREATE INDEX idx_outbox_events_topic_status_updated
  ON outbox_events (destination_topic, status, updated_at);

CREATE TABLE processed_commands (
  idempotency_key VARCHAR(256) PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  result_emitted BOOLEAN NOT NULL DEFAULT FALSE,
  claim_token UUID NOT NULL DEFAULT gen_random_uuid(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_processed_commands_namespace ON processed_commands (namespace);

CREATE TABLE processed_ingest_events (
  event_dedup_key VARCHAR(256) PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE processed_operator_recoveries (
  dedup_key VARCHAR(256) PRIMARY KEY,
  request_fingerprint VARCHAR(128) NOT NULL,
  response_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
