-- Soft-disable flag on workers for catalog parity with saga/step definitions.
ALTER TABLE worker_definitions
  ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
