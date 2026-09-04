-- Freeze resolved policy CEL onto step rows at saga start (parity with compensation_definition).
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS policy_definition JSONB NULL;
