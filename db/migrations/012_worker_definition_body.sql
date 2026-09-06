-- Store validated worker manifests in body JSONB (saga/step pattern); drop denormalized columns.
ALTER TABLE worker_definitions
  ADD COLUMN IF NOT EXISTS body JSONB;

UPDATE worker_definitions
SET body = jsonb_build_object(
  'kind', 'worker',
  'name', name,
  'namespace', namespace,
  'version', version,
  'provider', model_provider,
  'model_name', model_name,
  'system_prompt', system_prompt,
  'tool_sources', COALESCE(tool_sources, '[]'::jsonb),
  'adapter', COALESCE(NULLIF(adapter, ''), 'langchain'),
  'temperature', 0.0
)
WHERE body IS NULL;

ALTER TABLE worker_definitions
  ALTER COLUMN body SET NOT NULL;

ALTER TABLE worker_definitions
  DROP COLUMN IF EXISTS model_provider,
  DROP COLUMN IF EXISTS model_name,
  DROP COLUMN IF EXISTS system_prompt,
  DROP COLUMN IF EXISTS tool_sources,
  DROP COLUMN IF EXISTS adapter;
