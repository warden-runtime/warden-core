"""Worker definition lookup must use the full (namespace, name, version) identity."""

from uuid import uuid4

import pytest
from common.catalog_errors import CatalogDefinitionNotFoundError
from common.models import ProviderSecret, WorkerDefinition
from tests.factories import worker_definition_body
from workers.logic import load_worker_config


@pytest.mark.asyncio
async def test_load_worker_config_selects_exact_version() -> None:
    """Same worker name with two versions returns the blueprint matching worker_version."""
    await WorkerDefinition.create(
        namespace="default",
        name="versioned-worker",
        version="1.0.0",
        body=worker_definition_body(
            name="versioned-worker",
            version="1.0.0",
            model_name="gpt-4o-mini",
            system_prompt="v1",
        ),
    )
    await WorkerDefinition.create(
        namespace="default",
        name="versioned-worker",
        version="2.0.0",
        body=worker_definition_body(
            name="versioned-worker",
            version="2.0.0",
            model_name="gpt-4o",
            system_prompt="v2",
        ),
    )
    await ProviderSecret.create(
        id=uuid4(),
        namespace="default",
        provider="openai",
        api_key="sk-test",
    )

    worker, _secret = await load_worker_config("versioned-worker", "default", "2.0.0")

    assert worker.version == "2.0.0"
    assert worker.model_name == "gpt-4o"
    assert worker.system_prompt == "v2"


@pytest.mark.asyncio
async def test_load_worker_config_raises_when_version_missing() -> None:
    await WorkerDefinition.create(
        namespace="default",
        name="only-v1",
        version="1.0.0",
        body=worker_definition_body(name="only-v1", version="1.0.0"),
    )
    with pytest.raises(CatalogDefinitionNotFoundError, match="not found"):
        await load_worker_config("only-v1", "default", "9.9.9")
