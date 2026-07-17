"""Unit tests for golden-ratio ReAct memory compression."""

from __future__ import annotations

import json
import math

import pytest
from common.agent_adapter import ExecutionStepError
from common.llm import ChatMessage, ToolCall
from workers.adapters.react_memory import (
    DIGEST_PREFIX,
    PHI_INV,
    CalibratedEstimator,
    _compute_budgets,
    compress_if_needed,
    effective_context_limit,
    format_tool_keys_peek,
    partition_messages,
    phi_split,
    serialize_for_estimate,
)


def _asst(content: str = "", *, tools: list[str] | None = None) -> ChatMessage:
    tcs = None
    if tools:
        tcs = [ToolCall(name=n, args={}, id=f"id-{n}") for n in tools]
    return ChatMessage(role="assistant", content=content, tool_calls=tcs)


def _tool(name: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, name=name, tool_call_id=f"id-{name}")


def _transcript_with_groups(n_groups: int, *, tool_payload: str = "ok") -> list[ChatMessage]:
    msgs: list[ChatMessage] = [
        ChatMessage(role="system", content="You are a worker."),
        ChatMessage(role="human", content="Do the task."),
    ]
    for i in range(n_groups):
        name = f"tool_{i}"
        msgs.append(_asst(f"thought {i}", tools=[name]))
        msgs.append(_tool(name, tool_payload))
    return msgs


def test_compression_stats_when_triggered():
    msgs = _transcript_with_groups(5, tool_payload="result-ok")
    _out, stats = compress_if_needed(
        msgs,
        max_turns=3,
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=None,
    )
    assert stats.compressed is True
    assert stats.deepest_tier >= 2
    assert stats.groups_evicted > 0
    assert stats.groups_after < stats.groups_before
    attrs = stats.to_otel_attrs()
    assert attrs["warden.memory.compressed"] is True
    assert attrs["warden.memory.trigger_tier"] == stats.deepest_tier
    mem = stats.to_usage_memory()
    assert mem["compressions"] == 1
    assert mem["groups_evicted"] == stats.groups_evicted


def test_memory_compression_enabled_from_env(monkeypatch):
    from workers.adapters.react_memory import memory_compression_enabled_from_env

    monkeypatch.delenv("WARDEN_REACT_MEMORY_COMPRESSION", raising=False)
    assert memory_compression_enabled_from_env() is True
    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "0")
    assert memory_compression_enabled_from_env() is False
    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "false")
    assert memory_compression_enabled_from_env() is False
    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "off")
    assert memory_compression_enabled_from_env() is False
    monkeypatch.setenv("WARDEN_REACT_MEMORY_COMPRESSION", "1")
    assert memory_compression_enabled_from_env() is True


def test_phi_split_sums_to_budget():
    for budget in (1, 2, 10, 20, 200):
        wm, anchor = phi_split(budget)
        assert wm + anchor == budget
        assert wm == math.floor(budget * PHI_INV)


def test_estimator_ema_and_noop():
    est = CalibratedEstimator(default_ratio=3.5, alpha=0.3)
    before = est.ratio
    est.calibrate("", 100)
    assert est.ratio == before
    est.calibrate("hello", 0)
    assert est.ratio == before
    # 100 chars / 25 tokens = 4.0
    est.calibrate("x" * 100, 25)
    assert est.ratio == pytest.approx(0.7 * 3.5 + 0.3 * 4.0)


def test_estimate_messages_does_not_crash_on_tool_calls():
    est = CalibratedEstimator()
    msgs = [
        ChatMessage(
            role="assistant",
            content="call",
            tool_calls=[ToolCall(name="echo", args={"x": "y"}, id="1")],
        )
    ]
    assert est.estimate_messages(msgs) > 0
    assert len(serialize_for_estimate(msgs)) > 0


def test_partition_parallel_tools_one_group():
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="human", content="goal"),
        _asst("think", tools=["a", "b"]),
        _tool("a", "ra"),
        _tool("b", "rb"),
        _asst("next", tools=["c"]),
        _tool("c", "rc"),
    ]
    anchors, groups = partition_messages(msgs)
    assert len(anchors) == 2
    assert len(groups) == 2
    assert len(groups[0].tool_msgs) == 2
    assert len(groups[1].tool_msgs) == 1


def test_format_tool_keys_empty_and_redacted():
    assert format_tool_keys_peek("{}") == "keys=<empty>"
    assert format_tool_keys_peek('{"secret_api_key": "x", "password": "y"}') == "keys=<redacted>"
    assert format_tool_keys_peek('{"id": 1, "secret_token": "x"}') == "keys=id"
    assert format_tool_keys_peek("not-json") == "keys=unknown"
    assert format_tool_keys_peek("[1,2]") == "keys=list"


def test_tier1_redacts_old_tools_and_encodes_status():
    big = json.dumps({"id": 1, "rows": list(range(200))})
    assert len(big) > 50
    msgs = _transcript_with_groups(3, tool_payload=big)
    # Append a hot group with smaller payload so Tier 1 can finish under turn budget
    msgs.extend([_asst("hot", tools=["hot"]), _tool("hot", "small")])

    out, stats = compress_if_needed(
        msgs,
        max_turns=4,  # wm_turn_target = floor(4*0.618)=2 → over with 4 groups
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=50,
    )
    # Oldest tool messages should be redacted; digest or fewer groups present
    tool_contents = [m.content for m in out if m.role == "tool"]
    assert any("Tool output redacted" in c and "keys=id" in c for c in tool_contents)
    assert any("status=ok" in c for c in tool_contents if "Tool output redacted" in c)


def test_tier1_empty_object_and_all_sensitive_keys():
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="human", content="goal"),
        _asst("t0", tools=["e"]),
        _tool("e", "{}"),
        _asst("t1", tools=["s"]),
        _tool("s", json.dumps({"api_key": "x", "db_password": "y"})),
        _asst("hot", tools=["h"]),
        _tool("h", "ok"),
    ]
    out, stats = compress_if_needed(
        msgs,
        max_turns=2,
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=1,
    )
    blob = "\n".join(m.content for m in out)
    assert "keys=<empty>" in blob
    assert "keys=<redacted>" in blob


def test_digest_is_human_with_untrusted_boundary():
    msgs = _transcript_with_groups(5, tool_payload="result-ok")
    out, stats = compress_if_needed(
        msgs,
        max_turns=3,  # wm_target = 1
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=None,
    )
    digests = [m for m in out if m.role == "human" and (m.content or "").startswith("[Warden")]
    assert digests, "expected a memory digest human message"
    assert digests[0].content.startswith(DIGEST_PREFIX.strip()[:20]) or digests[
        0
    ].content.startswith("[Warden memory digest]")
    assert "untrusted" in digests[0].content.lower()
    assert digests[0].role == "human"
    assert not any(
        m.role == "system" and (m.content or "").startswith("[Warden memory digest]") for m in out
    )


def test_digest_marks_failures_differently():
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="human", content="goal"),
        _asst("t0", tools=["fail"]),
        _tool("fail", "Error: boom"),
        _asst("t1", tools=["ok"]),
        _tool("ok", "all good"),
        _asst("hot", tools=["h"]),
        _tool("h", "now"),
    ]
    out, stats = compress_if_needed(
        msgs,
        max_turns=2,
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=None,
    )
    digest = next(m for m in out if m.role == "human" and "[Warden memory digest]" in m.content)
    assert "status=error" in digest.content
    assert "status=ok" in digest.content


def test_soft_borrow_shrinks_turn_target():
    """Heavy anchors soft-borrow WM tokens and tighten the turn cap."""
    heavy = "A" * 5000
    anchors = [
        ChatMessage(role="system", content=heavy),
        ChatMessage(role="human", content=heavy),
    ]
    # Light WM groups
    light_groups_msgs: list[ChatMessage] = []
    for i in range(6):
        light_groups_msgs.append(_asst(f"t{i}", tools=[f"t{i}"]))
        light_groups_msgs.append(_tool(f"t{i}", "ok"))

    est = CalibratedEstimator(default_ratio=3.5)
    effective = effective_context_limit(2000, 0.9)
    assert effective is not None

    light_budgets = _compute_budgets(
        max_turns=10,
        anchors=[
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="human", content="goal"),
        ],
        estimator=est,
        effective_limit=effective,
    )
    heavy_budgets = _compute_budgets(
        max_turns=10,
        anchors=anchors,
        estimator=est,
        effective_limit=effective,
    )
    assert heavy_budgets.wm_token_target is not None
    assert light_budgets.wm_token_target is not None
    assert heavy_budgets.wm_token_target < light_budgets.wm_token_target
    assert heavy_budgets.wm_turn_target < light_budgets.wm_turn_target


def test_headroom_reduces_effective_limit():
    assert effective_context_limit(1000, 0.9) == 900
    assert effective_context_limit(1000, 1.0) == 1000
    assert effective_context_limit(None, 0.9) is None
    assert effective_context_limit(0, 0.9) is None


def test_context_overflow_when_hot_group_still_too_large():
    huge = "X" * 50_000
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="human", content="goal"),
        _asst("only", tools=["big"]),
        _tool("big", huge),
    ]
    with pytest.raises(ExecutionStepError) as exc:
        compress_if_needed(
            msgs,
            max_turns=10,
            context_limit=500,
            estimator=CalibratedEstimator(default_ratio=3.5),
            tool_redact_limit=None,  # cannot redact hot-only group via tier1 cold path
            headroom=0.9,
        )
    details = exc.value.error_details or {}
    assert details.get("code") == "CONTEXT_OVERFLOW"


def test_sensitive_keys_scrubbed_in_placeholder():
    payload = json.dumps({"id": 1, "api_token": "shh", "name": "n" + ("!" * 40)})
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="human", content="goal"),
        _asst("t0", tools=["q"]),
        _tool("q", payload),
        _asst("hot", tools=["h"]),
        _tool("h", "ok"),
    ]
    out, stats = compress_if_needed(
        msgs,
        max_turns=2,
        context_limit=None,
        estimator=CalibratedEstimator(),
        tool_redact_limit=10,
    )
    blob = "\n".join(m.content for m in out)
    assert "keys=id, name" in blob
    # Sensitive key name must not appear in the keys= peek token
    assert "api_token" not in blob.split("keys=")[1].split(".")[0]
