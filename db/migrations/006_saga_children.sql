ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS parent_trace_id VARCHAR(32) NULL;

CREATE INDEX IF NOT EXISTS idx_saga_instances_parent_trace_id
  ON saga_instances (namespace, parent_trace_id);

CREATE TABLE IF NOT EXISTS saga_children (
  id UUID PRIMARY KEY,
  namespace VARCHAR(50) NOT NULL DEFAULT 'default',
  parent_trace_id VARCHAR(32) NOT NULL,
  spawn_step_id VARCHAR(128) NOT NULL,
  spawn_span_id VARCHAR(16) NOT NULL,
  item_id VARCHAR(256) NOT NULL,
  child_trace_id VARCHAR(32) NOT NULL,
  idempotency_key VARCHAR(256) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT saga_children_idempotency_key_uniq UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_saga_children_parent_spawn
  ON saga_children (namespace, parent_trace_id, spawn_step_id);

CREATE INDEX IF NOT EXISTS idx_saga_children_child_trace
  ON saga_children (child_trace_id);
