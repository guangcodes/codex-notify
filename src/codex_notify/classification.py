"""Independent Turn classification states.

Prompt text is deliberately not a classification input.  Public Codex events do
not currently bridge a subagent ``agent_id`` to a child Turn identity, so
``CONFIRMED_CHILD`` is reserved for future direct structured evidence.
"""

from __future__ import annotations

from enum import StrEnum


class TaskClassification(StrEnum):
    PENDING_ROOT_CANDIDATE = "PENDING_ROOT_CANDIDATE"
    NOTIFIABLE_ROOT = "NOTIFIABLE_ROOT"
    CONFIRMED_CHILD = "CONFIRMED_CHILD"
    UNVERIFIED = "UNVERIFIED"
    CONFLICT = "CONFLICT"


class TurnLifecycle(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
