-- First-class step definitions + pin catalog identity onto step instances.
CREATE TABLE IF NOT EXISTS step_definitions (
    id UUID PRIMARY KEY,
    namespace VARCHAR(50) NOT NULL DEFAULT 'default',
    name VARCHAR(128) NOT NULL,
    version VARCHAR(50) NOT NULL DEFAULT '0.0.1',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS step_definitions_namespace_name_version_uniq
  ON step_definitions (namespace, name, version);

CREATE INDEX IF NOT EXISTS step_definitions_namespace_idx
  ON step_definitions (namespace);

ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS step_definition_name VARCHAR(128) NULL;

ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS step_definition_version VARCHAR(50) NULL;

ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS input_ports JSONB NOT NULL DEFAULT '{}'::jsonb;
