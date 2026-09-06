-- Freeze catalog saga name/version onto instances at start (operator / list DX).
ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS definition_name VARCHAR(128) NULL;

ALTER TABLE saga_instances
  ADD COLUMN IF NOT EXISTS definition_version VARCHAR(50) NULL;

-- Backfill from catalog where the definition row still exists.
UPDATE saga_instances AS si
SET
  definition_name = sd.name,
  definition_version = sd.version
FROM saga_definitions AS sd
WHERE si.definition_id = sd.id::text
  AND si.definition_name IS NULL;
