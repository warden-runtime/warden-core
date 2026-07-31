-- Optional per-reason-step completion token cap (provider max_tokens per LLM call).
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS max_completion_tokens INTEGER NULL;
