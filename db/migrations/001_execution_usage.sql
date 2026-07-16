-- Sibling of execution_timing: provider-reported LLM token usage per step row.
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS execution_usage JSONB NULL;
