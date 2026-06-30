"""
Centralized topic names for the outbox/messaging layer.

Read from app config (ENGINE_EVENTS_TOPIC, WORKER_COMMANDS_TOPIC).
"""

from common.config import get_settings

_settings = get_settings()
TOPIC_ORCHESTRATOR_EVENTS: str = _settings.topic_orchestrator_events
TOPIC_WORKER_COMMANDS: str = _settings.topic_worker_commands
