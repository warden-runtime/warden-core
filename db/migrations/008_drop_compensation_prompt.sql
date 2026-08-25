-- Drop worker_definitions.compensation_prompt (compensation is single-tool MCP).
ALTER TABLE worker_definitions
  DROP COLUMN IF EXISTS compensation_prompt;
