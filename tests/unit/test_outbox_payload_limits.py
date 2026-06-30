"""Outbox payload size guard for worker-commands."""

import pytest
from common.config import get_settings
from common.contracts import DoStepCommand
from common.outbox import emit_saga_event
from common.topics import TOPIC_WORKER_COMMANDS


@pytest.mark.asyncio
async def test_emit_rejects_oversized_worker_command(monkeypatch):
    monkeypatch.setenv("OUTBOX_MAX_PAYLOAD_BYTES", "200")
    get_settings.cache_clear()
    try:
        command = DoStepCommand(
            type="DO_STEP",
            namespace="default",
            saga_trace_id="a" * 32,
            step_span_id="b" * 16,
            worker_name="test-worker",
            worker_version="1.0.0",
            idempotency_key="idem-oversize",
            prompt_ref="p.j2",
            arguments={"blob": "x" * 500},
            tool_specs=[],
        )
        with pytest.raises(ValueError) as exc_info:
            await emit_saga_event(
                topic=TOPIC_WORKER_COMMANDS,
                event_type="DO_STEP",
                payload_schema=command,
            )
        assert "OUTBOX_MAX_PAYLOAD_BYTES" in str(exc_info.value)
    finally:
        monkeypatch.delenv("OUTBOX_MAX_PAYLOAD_BYTES", raising=False)
        get_settings.cache_clear()
