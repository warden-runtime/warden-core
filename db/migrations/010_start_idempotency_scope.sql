-- Scope start idempotency to (namespace, definition_id, start_idempotency_key).
ALTER TABLE saga_instances
  DROP CONSTRAINT IF EXISTS saga_instances_namespace_start_idempotency_key_uniq;

CREATE UNIQUE INDEX IF NOT EXISTS saga_instances_namespace_definition_start_idempotency_key_uniq
  ON saga_instances (namespace, definition_id, start_idempotency_key)
  WHERE start_idempotency_key IS NOT NULL;
