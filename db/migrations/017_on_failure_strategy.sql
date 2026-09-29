-- Post-failure hold: freeze on_failure.strategy onto forward step rows at materialize.
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS on_failure_strategy VARCHAR(32) NOT NULL DEFAULT 'auto_compensate';
