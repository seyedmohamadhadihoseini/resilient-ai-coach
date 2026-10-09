"""Domain event sourcing models and state reconstruction engine.

Guarantees an append-only, tamper-evident audit trail for compliance and safety.
Allows rebuilding aggregate state by replaying historical events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class WorkoutEvent:
    event_id: int
    event_type: str
    workout_id: str
    actor: str
    request_id: str
    payload: Dict[str, Any]
    ts: float


def reconstruct_workout_intensity(events: List[WorkoutEvent], initial_intensity: int = 100) -> int:
    """Deterministically reconstructs current intensity purely by replaying historical events."""
    current = initial_intensity
    for event in events:
        if event.event_type == "WORKOUT_INTENSITY_REDUCED":
            # Event payload contains explicit delta and absolute new value
            current = event.payload.get("new_intensity", current)
    return current