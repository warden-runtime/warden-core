"""Unit tests for common.saga_assets."""

import json
from pathlib import Path

import pytest
import yaml
from common.saga_assets import load_compensation_definition, load_output_schema

pytestmark = pytest.mark.asyncio


async def test_load_output_schema_none_ref_returns_none(tmp_path: Path) -> None:
    assert await load_output_schema(schemas_root=str(tmp_path), ref=None) is None
    assert await load_output_schema(schemas_root=str(tmp_path), ref="") is None


async def test_load_output_schema_requires_root_when_ref_set(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SCHEMAS_ROOT"):
        await load_output_schema(schemas_root=None, ref="a.json")


async def test_load_output_schema_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        await load_output_schema(schemas_root=str(tmp_path), ref="../etc/passwd")


async def test_load_output_schema_rejects_absolute_ref(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute paths"):
        await load_output_schema(schemas_root=str(tmp_path), ref="/etc/passwd")


async def test_load_output_schema_reads_nested_file(tmp_path: Path) -> None:
    sub = tmp_path / "nested"
    sub.mkdir()
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    (sub / "s.json").write_text(json.dumps(schema), encoding="utf-8")
    loaded = await load_output_schema(schemas_root=str(tmp_path), ref="nested/s.json")
    assert loaded == schema


async def test_load_output_schema_rejects_non_object_root(tmp_path: Path) -> None:
    (tmp_path / "list.json").write_text("[1,2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        await load_output_schema(schemas_root=str(tmp_path), ref="list.json")


async def test_load_compensation_requires_root_when_ref_set(tmp_path: Path) -> None:
    (tmp_path / "c.yaml").write_text('worker: w1\nworker_version: "1.0.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="COMPENSATIONS_ROOT"):
        await load_compensation_definition(compensations_root=None, ref="c.yaml")


async def test_load_compensation_yaml_round_trip(tmp_path: Path) -> None:
    body = {
        "worker": "w1",
        "worker_version": "1.0.0",
        "with": {"x": {"from": "$.input.x"}},
        "tools": {"allow": [{"name": "t1"}]},
    }
    (tmp_path / "c.yaml").write_text(yaml.dump(body), encoding="utf-8")
    loaded = await load_compensation_definition(compensations_root=str(tmp_path), ref="c.yaml")
    assert loaded["worker"] == "w1"
    assert "with" in loaded
    assert loaded["tools"]["allow"][0]["name"] == "t1"
