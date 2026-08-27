-- Reason-step tools.bind keys frozen onto saga_step_instances at materialize.
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS tools_bind JSONB NOT NULL DEFAULT '[]'::jsonb;
