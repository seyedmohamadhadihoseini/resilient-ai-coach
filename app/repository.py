"""In-memory persistence: workouts, the idempotency ledger, and an audit trail.

This is the ONLY module allowed to mutate application state, and every mutation
happens under a re-entrant lock. It explicitly simulates a database; its limits
(durability, multi-process, crash atomicity) are documented in the README.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app.errors import RepositoryError
from app.schemas import CoachResponse

logger = logging.getLogger(__name__)

# The idempotency key is ALWAYS (user_id, request_id) -- never request_id alone.
# request_id is client-generated; keying on it alone would leak one user's cached
# response to another user on collision (there is a test proving isolation).
IdempotencyKey = Tuple[str, str]

DEFAULT_WORKOUTS: Dict[str, dict] = {
    "workout_123": {"owner": "user_123", "intensity": 100},
    "workout_456": {"owner": "user_999", "intensity": 100},
    "workout_789": {"owner": "user_123", "intensity": 15},  # near the business floor
}


@dataclass
class Workout:
    owner: str
    intensity: int  # 0..100 session-load scalar


class BeginOutcome(Enum):
    ACQUIRED = "acquired"        # this caller now owns processing of the request
    IN_PROGRESS = "in_progress"  # identical request is being processed right now
    COMPLETED = "completed"      # finished before; cached response is attached


@dataclass
class BeginResult:
    outcome: BeginOutcome
    response: Optional[CoachResponse] = None


@dataclass
class _Reservation:
    created_at: float
    response: Optional[CoachResponse] = None  # None => still in progress


class InMemoryRepository:
    def __init__(self, workouts: Optional[Dict[str, dict]] = None) -> None:
        self._lock = threading.RLock()
        self._workouts: Dict[str, Workout] = {
            workout_id: Workout(**spec) for workout_id, spec in (workouts or DEFAULT_WORKOUTS).items()
        }
        self._idempotency: Dict[IdempotencyKey, _Reservation] = {}
        self._audit: List[dict] = []

    # ---- idempotency ledger -------------------------------------------------

    def begin_request(
        self, user_id: str, request_id: str, *, stale_after_s: float = 120.0
    ) -> BeginResult:
        """Atomically reserve the right to process (user_id, request_id).

        Exactly one concurrent caller gets ACQUIRED; everyone else either gets
        the cached response (COMPLETED) or IN_PROGRESS (mapped to HTTP 409).
        This removes the classic check-then-act race between "has it run?" and
        "run it". `stale_after_s` bounds how long a crashed handler can hold
        the reservation before another caller may steal it.
        """
        key: IdempotencyKey = (user_id, request_id)
        now = time.monotonic()
        with self._lock:
            record = self._idempotency.get(key)
            if record is not None:
                if record.response is not None:
                    return BeginResult(BeginOutcome.COMPLETED, record.response)
                if now - record.created_at <= stale_after_s:
                    return BeginResult(BeginOutcome.IN_PROGRESS)
                logger.warning("Stale in-progress reservation stolen: %s", key)
            self._idempotency[key] = _Reservation(created_at=now)
            return BeginResult(BeginOutcome.ACQUIRED)

    def complete_request(self, user_id: str, request_id: str, response: CoachResponse) -> None:
        """Publish the final response for a reservation we own."""
        with self._lock:
            record = self._idempotency.get((user_id, request_id))
            if record is None:
                logger.error("Completing a reservation that does not exist: %s/%s", user_id, request_id)
                return
            record.response = response

    def release_request(self, user_id: str, request_id: str) -> None:
        """Drop the reservation entirely.

        Used when processing FAILED (provider down / infrastructure error): no
        state changed, so a client retrying the same request_id should be able
        to start over from scratch rather than being pinned to a fallback.
        """
        with self._lock:
            self._idempotency.pop((user_id, request_id), None)

    # ---- workouts -----------------------------------------------------------

    def get_workout(self, workout_id: str) -> Optional[Workout]:
        with self._lock:
            workout = self._workouts.get(workout_id)
            return replace(workout) if workout else None  # copy: callers can't mutate state

    
    def record_intensity_change(
        self,
        workout_id: str,
        new_intensity: int,
        *,
        actor: str,
        request_id: str,
        expected_current: Optional[int] = None,   # optimistic concurrency control
    ) -> Workout:
        """The single state-changing write, with an audit entry. Atomic under the lock.

        `expected_current` is a compare-and-swap guard: if the intensity changed
        between the service's validation snapshot and this write (another request
        racing on the same workout), the write is REFUSED instead of silently
        landing on top of unvalidated state.
        """
        with self._lock:
            workout = self._workouts.get(workout_id)
            if workout is None:
                raise RepositoryError(f"workout disappeared during processing: {workout_id}")
            if expected_current is not None and workout.intensity != expected_current:
                raise RepositoryError(
                    f"concurrent modification on {workout_id}: expected intensity "
                    f"{expected_current}, found {workout.intensity}; refusing blind overwrite"
                )
            workout.intensity = new_intensity
            self._audit.append(
                {
                    "ts": time.time(),
                    "request_id": request_id,
                    "actor": actor,
                    "workout_id": workout_id,
                    "new_intensity": new_intensity,
                }
            )
            logger.info(
                "State change: %s intensity=%s (actor=%s request=%s)",
                workout_id, new_intensity, actor, request_id,
            )
            return replace(workout)
    
    # ---- audit --------------------------------------------------------------

    @property
    def audit_log(self) -> List[dict]:
        with self._lock:
            return list(self._audit)