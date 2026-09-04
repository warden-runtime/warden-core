"""Materialize freezes tools.bind onto tools_bind."""

from types import SimpleNamespace

from engine.step_materialize import _step_create_fields


def test_reason_step_materialize_writes_tools_bind() -> None:
    saga = SimpleNamespace(trace_id="trace1", namespace="default")
    fields = _step_create_fields(
        saga=saga,  # type: ignore[arg-type]
        step_spec={
            "id": "run",
            "name": "Run",
            "kind": "reason",
            "worker": "w",
            "worker_version": "1.0.0",
            "prompt": "p.j2",
            "with": {
                "container_id": {"from": "$.steps.init.output.data.container_id"},
                "problem": {"from": "$.input.problem"},
            },
            "tools": {
                "bind": ["container_id"],
                "allow": [{"name": "sandbox_exec"}],
            },
        },
        forward_seq=0,
        order_index=0,
    )
    assert fields["tools_bind"] == ["container_id"]
    assert fields["tools_allow"][0]["name"] == "sandbox_exec"


def test_commit_step_materialize_tools_bind_empty() -> None:
    saga = SimpleNamespace(trace_id="trace1", namespace="default")
    fields = _step_create_fields(
        saga=saga,  # type: ignore[arg-type]
        step_spec={
            "id": "post",
            "name": "Post",
            "kind": "commit",
            "worker": "w",
            "worker_version": "1.0.0",
            "with": {"body": {"value": "hi"}},
            "tools": {"allow": [{"name": "add_comment"}]},
        },
        forward_seq=1,
        order_index=1,
    )
    assert fields["tools_bind"] == []
