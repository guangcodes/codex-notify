"""Independent Turn classification states.

Prompt text is deliberately not a classification input. A child is confirmed
only from a unique active parent Turn plus exact Hook agent and Turn identity.
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
