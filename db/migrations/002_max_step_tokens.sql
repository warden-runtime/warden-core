-- Optional per-reason-step financial token budget (provider-reported total_tokens).
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS max_step_tokens INTEGER NULL;
