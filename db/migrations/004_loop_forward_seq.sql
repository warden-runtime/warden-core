-- Loop execution: monotonic forward_seq, loop tags on steps, frozen orchestration on instances.
ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS forward_seq INTEGER NULL;

ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS loop_id VARCHAR(128) NULL;

ALTER TABLE saga_step_instances
  ADD COLUMN IF NOT EXISTS iteration INTEGER NULL;

-- Backfill existing rows: preserve prior linear order_index as forward_seq.
UPDATE saga_step_instances
SET forward_seq = order_index
WHERE forward_seq IS NULL;

ALTER TABLE saga_step_instances
  ALTER COLUMN forward_seq SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_saga_step_instances_forward_seq
  ON saga_step_instances (saga_trace_id, forward_seq);

ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS frozen_steps JSONB NULL;

ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS loop_definitions JSONB NULL;

ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS loop_state JSONB NULL;
