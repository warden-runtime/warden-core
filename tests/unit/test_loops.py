"""Unit tests for loop materialization helpers and until evaluation."""

from __future__ import annotations

from common.loops import (
    build_loop_definitions,
    evaluate_until,
    initial_loops_context,
    take_initial_materialization_segment,
    take_next_materialization_segment,
)


def test_take_initial_segment_stops_after_first_loop() -> None:
    frozen = [
        {
            "id": "prep",
            "kind": "reason",
            "name": "P",
            "worker": "w",
            "worker_version": "1",
            "prompt": "p.j2",
        },
        {
            "id": "refine",
            "kind": "loop",
            "max_iterations": 2,
            "until": {"cel": "true"},
            "steps": [
                {
                    "id": "attempt",
                    "kind": "reason",
                    "name": "A",
                    "worker": "w",
                    "worker_version": "1",
                    "prompt": "p.j2",
                }
            ],
        },
        {
            "id": "done",
            "kind": "commit",
            "name": "D",
            "worker": "w",
            "worker_version": "1",
            "tools": {"allow": [{"name": "t"}]},
        },
    ]
    segment, state = take_initial_materialization_segment(frozen)
    assert [s["id"] for s in segment] == ["prep", "attempt"]
    assert segment[1]["_loop_id"] == "refine"
    assert segment[1]["_loop_iteration"] == 1
    assert state["active_loop_id"] == "refine"
    assert state["next_blueprint_index"] == 2


def test_take_next_segment_materializes_tail_after_loop() -> None:
    frozen = [
        {
            "id": "refine",
            "kind": "loop",
            "max_iterations": 1,
            "until": {"cel": "true"},
            "steps": [
                {
                    "id": "attempt",
                    "kind": "reason",
                    "name": "A",
                    "worker": "w",
                    "worker_version": "1",
                    "prompt": "p.j2",
                }
            ],
        },
        {
            "id": "done",
            "kind": "commit",
            "name": "D",
            "worker": "w",
            "worker_version": "1",
            "tools": {"allow": [{"name": "t"}]},
        },
    ]
    segment, state = take_next_materialization_segment(frozen, from_blueprint_index=1)
    assert [s["id"] for s in segment] == ["done"]
    assert state["active_loop_id"] is None
    assert state["next_blueprint_index"] == 2


def test_initial_loops_context_and_definitions() -> None:
    frozen = [
        {
            "id": "refine",
            "kind": "loop",
            "max_iterations": 3,
            "until": {"cel": "true"},
            "steps": [
                {
                    "id": "attempt",
                    "kind": "reason",
                    "name": "A",
                    "worker": "w",
                    "worker_version": "1",
                    "prompt": "p.j2",
                }
            ],
        }
    ]
    loops = initial_loops_context(frozen)
    assert loops["refine"]["iteration"] == 1
    assert loops["refine"]["max_iterations"] == 3
    defs = build_loop_definitions(frozen)
    assert defs["refine"]["until_cel"] == "true"
    assert len(defs["refine"]["body"]) == 1


def test_evaluate_until_true_literal() -> None:
    assert (
        evaluate_until(
            cel_source="true",
            binding={"input": {}, "steps": {}, "loops": {}, "saga": {}, "loop": {}},
        )
        is True
    )
