"""Topic → Postgres LISTEN/NOTIFY channel helpers (must match DB trigger SQL)."""

from __future__ import annotations

import hashlib
import re

# Postgres NAMEDATALEN default: identifiers (incl. NOTIFY channels) max 63 bytes.
NOTIFY_CHANNEL_MAX_BYTES = 63
_CHANNEL_PREFIX = "warden_"
_NON_ALNUM = re.compile(r"[^a-z0-9_]")


def topic_to_notify_channel(topic: str) -> str:
    """Derive a LISTEN/NOTIFY channel from an outbox destination topic.

    Must stay byte-identical to ``notify_outbox_pending`` in
    ``db/migrations/005_outbox_notify_trigger.sql``.
    """
    sanitized = _NON_ALNUM.sub("_", topic.lower())
    channel = f"{_CHANNEL_PREFIX}{sanitized}"
    encoded = channel.encode("utf-8")
    if len(encoded) <= NOTIFY_CHANNEL_MAX_BYTES:
        return channel
    digest = hashlib.md5(encoded).hexdigest()
    hashed = f"{_CHANNEL_PREFIX}{digest}"
    # md5 hex is 32 chars; with prefix always well under 63.
    return hashed[:NOTIFY_CHANNEL_MAX_BYTES]
