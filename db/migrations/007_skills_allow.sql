-- Step-level skill allowlist refs (SKILLS_ROOT/<worker>/<name>.md).
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS skills_allow JSONB NOT NULL DEFAULT '[]'::jsonb;
