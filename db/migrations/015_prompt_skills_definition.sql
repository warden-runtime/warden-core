-- Freeze inlined prompt text and skill documents onto step rows at saga start.
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS prompt_definition TEXT NULL;
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS skills_definition JSONB NULL;
