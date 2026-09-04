"""Manifest deployment API: POST /v1/manifests."""

import asyncio
import json
import logging

import yaml
from common.catalog_errors import CatalogError
from common.config import get_settings
from fastapi import APIRouter, HTTPException, Request

from engine.api.http_errors import http_exception_for_catalog
from engine.api.schemas import ManifestDeployResponse
from engine.registry.service import RegistryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manifests", tags=["manifests"])

_ALLOWED_MANIFEST_MEDIA = frozenset(
    {
        "application/json",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
    }
)


@router.post(
    "",
    response_model=ManifestDeployResponse,
    status_code=200,
    responses={
        400: {
            "description": (
                "Manifest validation failed (invalid YAML/JSON, unknown kind, schema errors)."
            ),
        },
        404: {
            "description": "Referenced catalog definition (worker/step/saga) is missing.",
        },
        409: {
            "description": (
                "Referenced catalog definition exists but is inactive "
                "(``INACTIVE_CATALOG_DEFINITION``)."
            ),
        },
        413: {
            "description": "Manifest body exceeds MANIFEST_MAX_BODY_BYTES.",
        },
        415: {
            "description": "Unsupported Content-Type; use application/json or YAML media types.",
        },
    },
)
async def post_manifests(request: Request) -> ManifestDeployResponse:
    """Register a worker, step, or saga manifest. Accepts YAML or JSON body.

    Body must define a mapping with `kind` (worker | step | saga) and kind-specific
    fields. Same schema as the file-based manifests used by the CLI.

    Returns:
        ManifestDeployResponse with a success message.

    Raises:
        HTTPException: 413 when body is too large; 400 on invalid body, unknown kind,
        or missing worker/step dependencies.
    """
    body_bytes = await request.body()
    max_bytes = get_settings().manifest_max_body_bytes
    if len(body_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Manifest body exceeds limit of {max_bytes} bytes.",
        )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_MANIFEST_MEDIA:
        raise HTTPException(
            status_code=415,
            detail=(
                "Unsupported Content-Type; use application/json, application/x-yaml, "
                "text/yaml, or text/x-yaml."
            ),
        )

    try:
        if content_type == "application/json":
            data = json.loads(body_bytes.decode("utf-8"))
        else:
            raw = body_bytes.decode("utf-8")
            try:
                data = await asyncio.to_thread(yaml.safe_load, raw)
            except yaml.YAMLError as e:
                raise ValueError(f"Invalid YAML: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("Manifest body must be a mapping (e.g. kind, name, ...).")

        service = RegistryService()
        message = await service.register_manifest_from_dict(data)
        return ManifestDeployResponse(message=message)
    except CatalogError as e:
        logger.warning("manifest deploy catalog conflict: %s", e)
        raise http_exception_for_catalog(e) from e
    except ValueError as e:
        logger.warning("manifest deploy rejected: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
