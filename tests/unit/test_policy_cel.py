"""Unit tests for common.policy CEL loading and evaluation."""

import pytest
from common.policy import (
    PolicyEvaluationError,
    compile_cel_program,
    evaluate_cel_bool,
    load_policy_artifact,
)
from common.policy.cel_eval import _cel_bool_to_python
from common.policy.loader import PolicyArtifact, load_policy_artifact_with_meta


@pytest.mark.asyncio
async def test_load_policy_artifact(tmp_path) -> None:
    p = tmp_path / "demo.yaml"
    p.write_text(
        'name: demo\nversion: "2"\ncel: "output.x == 1"\n',
        encoding="utf-8",
    )
    art = await load_policy_artifact(policies_root=str(tmp_path), policy_name="demo")
    assert art.name == "demo"
    assert art.version == "2"
    assert "output.x" in art.cel_source


def test_evaluate_cel_bool_passes_and_fails() -> None:
    art = PolicyArtifact(name="t", version="1", cel_source="output.data.amount <= 5000")
    assert art.cel_source is not None
    prog = compile_cel_program(art.cel_source)
    assert (
        evaluate_cel_bool(
            artifact=art,
            cel_program=prog,
            binding={"output": {"data": {"amount": 100}}},
        )
        is True
    )
    assert (
        evaluate_cel_bool(
            artifact=art,
            cel_program=prog,
            binding={"output": {"data": {"amount": 99999}}},
        )
        is False
    )


@pytest.mark.asyncio
async def test_load_policy_requires_cel_expression(tmp_path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text('name: bad\nversion: "1"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="cel"):
        await load_policy_artifact(policies_root=str(tmp_path), policy_name="empty")


def test_compile_cel_program_invalid() -> None:
    with pytest.raises(PolicyEvaluationError):
        compile_cel_program("@@@not valid@@@")


@pytest.mark.asyncio
async def test_load_policy_missing_root() -> None:
    with pytest.raises(ValueError, match="policies_root"):
        await load_policy_artifact(policies_root=None, policy_name="x")


def test_cel_bool_to_python_rejects_non_bool() -> None:
    with pytest.raises(PolicyEvaluationError, match="bool"):
        _cel_bool_to_python("true")


@pytest.mark.asyncio
async def test_load_policy_explicit_yaml_path(tmp_path) -> None:
    (tmp_path / "gate.yaml").write_text('cel: "true"\n', encoding="utf-8")
    art, used_legacy = await load_policy_artifact_with_meta(
        policies_root=str(tmp_path), policy_ref="gate.yaml"
    )
    assert art.cel_source == "true"
    assert used_legacy is False


@pytest.mark.asyncio
async def test_load_policy_legacy_stem_fallback(tmp_path) -> None:
    (tmp_path / "legacy.yaml").write_text('cel: "true"\n', encoding="utf-8")
    art, used_legacy = await load_policy_artifact_with_meta(
        policies_root=str(tmp_path), policy_ref="legacy"
    )
    assert art.cel_source == "true"
    assert used_legacy is True


@pytest.mark.asyncio
async def test_load_policy_subdir_path(tmp_path) -> None:
    sub = tmp_path / "teams" / "marketing"
    sub.mkdir(parents=True)
    (sub / "gate.yaml").write_text('cel: "true"\n', encoding="utf-8")
    art, used_legacy = await load_policy_artifact_with_meta(
        policies_root=str(tmp_path), policy_ref="teams/marketing/gate.yaml"
    )
    assert art.name == "teams/marketing/gate"
    assert used_legacy is False


@pytest.mark.asyncio
async def test_load_policy_extensionless_beats_yaml_when_both_exist(tmp_path) -> None:
    (tmp_path / "github-issue-comment").write_text('cel: "true"\n', encoding="utf-8")
    (tmp_path / "github-issue-comment.yaml").write_text('cel: "false"\n', encoding="utf-8")
    art, used_legacy = await load_policy_artifact_with_meta(
        policies_root=str(tmp_path), policy_ref="github-issue-comment"
    )
    assert art.cel_source == "true"
    assert used_legacy is False


@pytest.mark.asyncio
async def test_load_policy_missing_file_error_lists_candidates(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match=r" or .*missing\.yaml"):
        await load_policy_artifact_with_meta(policies_root=str(tmp_path), policy_ref="missing")
