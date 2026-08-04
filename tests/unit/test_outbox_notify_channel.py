"""Unit tests for outbox LISTEN/NOTIFY channel naming."""

from __future__ import annotations

import hashlib

from common.messaging.notify import NOTIFY_CHANNEL_MAX_BYTES, topic_to_notify_channel


def test_default_topics_map_to_warden_channels() -> None:
    assert topic_to_notify_channel("worker-commands") == "warden_worker_commands"
    assert topic_to_notify_channel("engine-events") == "warden_engine_events"


def test_channel_lowercases_and_sanitizes() -> None:
    assert topic_to_notify_channel("Worker.Commands") == "warden_worker_commands"


def test_long_topic_stays_within_namedatalen() -> None:
    topic = "x" * 200
    channel = topic_to_notify_channel(topic)
    assert len(channel.encode("utf-8")) <= NOTIFY_CHANNEL_MAX_BYTES
    full = "warden_" + ("x" * 200)
    expected = "warden_" + hashlib.md5(full.encode("utf-8")).hexdigest()
    assert channel == expected
