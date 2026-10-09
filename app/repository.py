"""Durable SQLite persistence: workouts, idempotency ledger, audit trail, and event sourcing.

Supports both on-disk persistent SQLite files (WAL mode) and in-memory isolated DBs.
Guarantees atomic, single-transaction commits across state mutations and idempotency logs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app.concurrency import execute_with_busy_retry
from app.database import DEFAULT_WORKOUTS, configure_connection, init_database
from app.errors import RepositoryError
from app.events import WorkoutEvent, reconstruct_workout_intensity
from app.schemas import CoachResponse

logger = logging.getLogger(__name__)

IdempotencyKey = Tuple[str, str]


@dataclass
class Workout:
    owner: str
    intensity: int


class LedgerStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    REJECTED = "rejected"


class BeginOutcome(Enum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class BeginResult:
    outcome: BeginOutcome
    response: Optional[CoachResponse] = None
    ledger_status: Optional[str] = None


@dataclass
class LedgerEntry:
    user_id: str
    request_id: str
    status: str
    response: Optional[CoachResponse]
    created_at: float
    updated_at: float
    lease_expires_at: float


class SQLiteRepository:
    def __init__(
        self,
        db_path: Optional[str] = None,
        workouts: Optional[Dict[str, dict]] = None,
    ) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()

        if not self._db_path:
            self._is_memory = True
            self._shared_conn = sqlite3.connect(
                ":memory:", check_same_thread=False, isolation_level=None
            )
            configure_connection(self._shared_conn, is_memory=True)
            init_database(self._shared_conn, seed_workouts=workouts)
        else:
            self._is_memory = False
            self._shared_conn = None
            conn = self._conn()
            try:
                init_database(conn, seed_workouts=workouts)
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        if self._is_memory:
            return self._shared_conn
        conn = sqlite3.connect(
            self._db_path, timeout=5.0, isolation_level=None
        )
        return configure_connection(conn, is_memory=False)

    # ---- idempotency ledger -------------------------------------------------

    def begin_request(
        self, user_id: str, request_id: str, *, stale_after_s: float = 120.0
    ) -> BeginResult:
        def _operation() -> BeginResult:
            now = time.time()
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cursor = conn.execute(
                        "SELECT status, response_json, updated_at, lease_expires_at FROM idempotency_ledger "
                        "WHERE user_id = ? AND request_id = ?;",
                        (user_id, request_id),
                    )
                    row = cursor.fetchone()

                    if row is not None:
                        status = row["status"]
                        response_json = row["response_json"]
                        updated_at = row["updated_at"]
                        lease_expires_at = row["lease_expires_at"] or 0.0

                        if status in (LedgerStatus.COMMITTED.value, LedgerStatus.REJECTED.value, "completed") and response_json:
                            conn.execute("COMMIT;")
                            cached_response = CoachResponse.model_validate_json(response_json)
                            return BeginResult(BeginOutcome.COMPLETED, cached_response, ledger_status=status)

                        if (now < lease_expires_at) or (now - updated_at <= stale_after_s):
                            conn.execute("COMMIT;")
                            return BeginResult(BeginOutcome.IN_PROGRESS, ledger_status=status)

                        logger.warning("Stale reservation reclaimed: user=%s req=%s", user_id, request_id)
                        conn.execute(
                            "UPDATE idempotency_ledger "
                            "SET status = ?, updated_at = ?, lease_expires_at = 0 "
                            "WHERE user_id = ? AND request_id = ?;",
                            (LedgerStatus.PENDING.value, now, user_id, request_id),
                        )
                        conn.execute("COMMIT;")
                        return BeginResult(BeginOutcome.ACQUIRED, ledger_status=LedgerStatus.PENDING.value)

                    conn.execute(
                        "INSERT INTO idempotency_ledger "
                        "(user_id, request_id, status, created_at, updated_at, lease_expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?);",
                        (user_id, request_id, LedgerStatus.PENDING.value, now, now, 0.0),
                    )
                    conn.execute("COMMIT;")
                    return BeginResult(BeginOutcome.ACQUIRED, ledger_status=LedgerStatus.PENDING.value)
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        return execute_with_busy_retry(_operation)

    def heartbeat(self, user_id: str, request_id: str, extension_s: float = 60.0) -> bool:
        def _operation() -> bool:
            now = time.time()
            new_lease = now + extension_s
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cursor = conn.execute(
                        "UPDATE idempotency_ledger "
                        "SET updated_at = ?, lease_expires_at = ? "
                        "WHERE user_id = ? AND request_id = ? AND status = ?;",
                        (now, new_lease, user_id, request_id, LedgerStatus.PENDING.value),
                    )
                    updated = cursor.rowcount > 0
                    conn.execute("COMMIT;")
                    return updated
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        return execute_with_busy_retry(_operation)

    def complete_request(
        self, user_id: str, request_id: str, response: CoachResponse, *, status: LedgerStatus = LedgerStatus.REJECTED
    ) -> None:
        def _operation() -> None:
            now = time.time()
            serialized = response.model_dump_json()
            target_status = status.value if isinstance(status, LedgerStatus) else str(status)
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        "UPDATE idempotency_ledger "
                        "SET status = ?, response_json = ?, updated_at = ?, lease_expires_at = 0 "
                        "WHERE user_id = ? AND request_id = ?;",
                        (target_status, serialized, now, user_id, request_id),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        execute_with_busy_retry(_operation)

    def release_request(self, user_id: str, request_id: str) -> None:
        def _operation() -> None:
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute(
                        "DELETE FROM idempotency_ledger "
                        "WHERE user_id = ? AND request_id = ? AND status NOT IN (?, ?);",
                        (user_id, request_id, LedgerStatus.COMMITTED.value, "completed"),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        execute_with_busy_retry(_operation)

    def get_ledger_entry(self, user_id: str, request_id: str) -> Optional[LedgerEntry]:
        with self._lock:
            conn = self._conn()
            should_close = not self._is_memory
            try:
                cursor = conn.execute(
                    "SELECT user_id, request_id, status, response_json, created_at, updated_at, lease_expires_at "
                    "FROM idempotency_ledger WHERE user_id = ? AND request_id = ?;",
                    (user_id, request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                resp = CoachResponse.model_validate_json(row["response_json"]) if row["response_json"] else None
                return LedgerEntry(
                    user_id=row["user_id"],
                    request_id=row["request_id"],
                    status=row["status"],
                    response=resp,
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    lease_expires_at=row["lease_expires_at"],
                )
            finally:
                if should_close:
                    conn.close()

    # ---- atomic state mutation, audit & event sourcing ----------------------

    def commit_workout_action_and_response(
        self,
        user_id: str,
        request_id: str,
        response: CoachResponse,
        *,
        workout_id: str,
        new_intensity: int,
        expected_current: Optional[int] = None,
    ) -> Workout:
        def _operation() -> Workout:
            now = time.time()
            serialized = response.model_dump_json()
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cursor = conn.execute(
                        "SELECT owner, intensity FROM workouts WHERE id = ?;", (workout_id,)
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RepositoryError(f"workout disappeared during processing: {workout_id}")

                    current_intensity = row["intensity"]
                    owner = row["owner"]

                    if expected_current is not None and current_intensity != expected_current:
                        raise RepositoryError(
                            f"concurrent modification on {workout_id}: expected intensity "
                            f"{expected_current}, found {current_intensity}; refusing blind overwrite"
                        )

                    # 1. Update current workout state
                    conn.execute(
                        "UPDATE workouts SET intensity = ? WHERE id = ?;",
                        (new_intensity, workout_id),
                    )
                    # 2. Append legacy audit trail
                    conn.execute(
                        "INSERT INTO audit_log (ts, request_id, actor, workout_id, new_intensity) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (now, request_id, user_id, workout_id, new_intensity),
                    )
                    # 3. Append immutable Domain Event (Event Sourcing)
                    event_payload = json.dumps({
                        "previous_intensity": current_intensity,
                        "new_intensity": new_intensity,
                        "delta": current_intensity - new_intensity,
                    })
                    conn.execute(
                        "INSERT INTO workout_events (event_type, workout_id, actor, request_id, payload_json, ts) "
                        "VALUES (?, ?, ?, ?, ?, ?);",
                        ("WORKOUT_INTENSITY_REDUCED", workout_id, user_id, request_id, event_payload, now),
                    )
                    # 4. Complete idempotency ledger atomically
                    conn.execute(
                        "UPDATE idempotency_ledger "
                        "SET status = ?, response_json = ?, updated_at = ?, lease_expires_at = 0 "
                        "WHERE user_id = ? AND request_id = ?;",
                        (LedgerStatus.COMMITTED.value, serialized, now, user_id, request_id),
                    )
                    conn.execute("COMMIT;")

                    logger.info(
                        "Atomic Commit with Event Sourced log: %s intensity=%s (actor=%s req=%s)",
                        workout_id,
                        new_intensity,
                        user_id,
                        request_id,
                    )
                    return Workout(owner=owner, intensity=new_intensity)
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        return execute_with_busy_retry(_operation)

    # ---- workouts & event replay --------------------------------------------

    def get_workout(self, workout_id: str) -> Optional[Workout]:
        with self._lock:
            conn = self._conn()
            should_close = not self._is_memory
            try:
                cursor = conn.execute(
                    "SELECT owner, intensity FROM workouts WHERE id = ?;", (workout_id,)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return Workout(owner=row["owner"], intensity=row["intensity"])
            finally:
                if should_close:
                    conn.close()

    def get_workout_events(self, workout_id: str) -> List[WorkoutEvent]:
        """Fetch all immutable historical domain events for a given workout in chronological order."""
        with self._lock:
            conn = self._conn()
            should_close = not self._is_memory
            try:
                cursor = conn.execute(
                    "SELECT id, event_type, workout_id, actor, request_id, payload_json, ts "
                    "FROM workout_events WHERE workout_id = ? ORDER BY id ASC;",
                    (workout_id,),
                )
                return [
                    WorkoutEvent(
                        event_id=row["id"],
                        event_type=row["event_type"],
                        workout_id=row["workout_id"],
                        actor=row["actor"],
                        request_id=row["request_id"],
                        payload=json.loads(row["payload_json"]),
                        ts=row["ts"],
                    )
                    for row in cursor.fetchall()
                ]
            finally:
                if should_close:
                    conn.close()

    def replay_workout_intensity(self, workout_id: str, baseline_intensity: int = 100) -> int:
        """Reconstruct workout intensity purely from immutable event logs."""
        events = self.get_workout_events(workout_id)
        return reconstruct_workout_intensity(events, initial_intensity=baseline_intensity)

    def record_intensity_change(
        self,
        workout_id: str,
        new_intensity: int,
        *,
        actor: str,
        request_id: str,
        expected_current: Optional[int] = None,
    ) -> Workout:
        def _operation() -> Workout:
            now = time.time()
            with self._lock:
                conn = self._conn()
                should_close = not self._is_memory
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    cursor = conn.execute(
                        "SELECT owner, intensity FROM workouts WHERE id = ?;", (workout_id,)
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RepositoryError(f"workout disappeared during processing: {workout_id}")

                    current_intensity = row["intensity"]
                    owner = row["owner"]

                    if expected_current is not None and current_intensity != expected_current:
                        raise RepositoryError(
                            f"concurrent modification on {workout_id}: expected intensity "
                            f"{expected_current}, found {current_intensity}; refusing blind overwrite"
                        )

                    conn.execute(
                        "UPDATE workouts SET intensity = ? WHERE id = ?;",
                        (new_intensity, workout_id),
                    )
                    conn.execute(
                        "INSERT INTO audit_log (ts, request_id, actor, workout_id, new_intensity) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (now, request_id, actor, workout_id, new_intensity),
                    )
                    conn.execute("COMMIT;")
                    return Workout(owner=owner, intensity=new_intensity)
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise
                finally:
                    if should_close:
                        conn.close()

        return execute_with_busy_retry(_operation)

    # ---- audit --------------------------------------------------------------

    @property
    def audit_log(self) -> List[dict]:
        with self._lock:
            conn = self._conn()
            should_close = not self._is_memory
            try:
                cursor = conn.execute(
                    "SELECT ts, request_id, actor, workout_id, new_intensity "
                    "FROM audit_log ORDER BY id ASC;"
                )
                return [
                    {
                        "ts": row["ts"],
                        "request_id": row["request_id"],
                        "actor": row["actor"],
                        "workout_id": row["workout_id"],
                        "new_intensity": row["new_intensity"],
                    }
                    for row in cursor.fetchall()
                ]
            finally:
                if should_close:
                    conn.close()


InMemoryRepository = SQLiteRepository