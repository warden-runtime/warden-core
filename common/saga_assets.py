"""Load saga manifest assets (step ``output_schema`` JSON paths, ``compensation`` YAML paths).

``output_schema`` paths are relative to ``SCHEMAS_ROOT``.
``compensation`` paths are relative to ``COMPENSATIONS_ROOT``.
"""

import asyncio
import json
from typing import Any

import yaml

from common.asset_paths import resolve_asset_path
from common.schemas.saga import CompensationStep


async def load_output_schema(*, schemas_root: str | None, ref: str | None) -> dict[str, Any] | None:
    """Load step output JSON Schema from ``{schemas_root}/{ref}``."""
    if not ref or not str(ref).strip():
        return None
    if not schemas_root or not str(schemas_root).strip():
        raise ValueError(
            "schemas_root is not configured; set SCHEMAS_ROOT when a step sets output_schema."
        )
    path = resolve_asset_path(
        schemas_root, ref.strip(), label="output_schema", root_var="SCHEMAS_ROOT"
    )
    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    data: Any = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"output_schema must be a JSON object: {path}")
    return data


async def assert_output_schema_readable(*, schemas_root: str | None, ref: str | None) -> None:
    """Validate ``output_schema`` path at manifest registration."""
    if not ref or not str(ref).strip():
        return
    await load_output_schema(schemas_root=schemas_root, ref=ref)


def validate_compensation_definition_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate an embedded or loaded compensation block; return JSON-safe dict."""
    comp = CompensationStep.model_validate(data)
    return comp.model_dump(mode="json", by_alias=True, exclude_none=True)


async def load_compensation_definition(
    *, compensations_root: str | None, ref: str | None
) -> dict[str, Any] | None:
    """Load and validate a compensation block from YAML under ``{compensations_root}/{ref}``."""
    if not ref or not str(ref).strip():
        return None
    if not compensations_root or not str(compensations_root).strip():
        raise ValueError(
            "compensations_root is not configured; set COMPENSATIONS_ROOT when a step sets compensation."
        )
    path = resolve_asset_path(
        compensations_root, ref.strip(), label="compensation", root_var="COMPENSATIONS_ROOT"
    )
    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    data: Any = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"compensation must be a YAML mapping: {path}")
    return validate_compensation_definition_dict(data)
