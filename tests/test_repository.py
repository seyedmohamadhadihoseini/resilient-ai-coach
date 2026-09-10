"""Repository tests: the idempotency ledger's guarantees, under direct fire.

The ledger is what turns "hopefully not twice" into "provably once per
(user, request_id)" -- so it gets its own unit coverage, including a
multi-threaded race that mirrors real duplicate retries arriving in parallel.
"""

import threading

import pytest

from app.errors import RepositoryError
from app.repository import BeginOutcome, InMemoryRepository
from app.schemas import CoachResponse


def _response(request_id: str) -> CoachResponse:
    return CoachResponse(
        status="success", assistant_message="ok", action=None, warnings=[], request_id=request_id
    )


def test_ledger_lifecycle_acquired_in_progress_completed():
    repo = InMemoryRepository()

    assert repo.begin_request("user_123", "req_1").outcome is BeginOutcome.ACQUIRED
    assert repo.begin_request("user_123", "req_1").outcome is BeginOutcome.IN_PROGRESS

    repo.complete_request("user_123", "req_1", _response("req_1"))
    replay = repo.begin_request("user_123", "req_1")

    assert replay.outcome is BeginOutcome.COMPLETED
    assert replay.response is not None
    assert replay.response.request_id == "req_1"


def test_release_lets_a_failed_request_start_over():
    repo = InMemoryRepository()
    repo.begin_request("user_123", "req_1")
    repo.release_request("user_123", "req_1")  # e.g. provider failed; nothing executed
    assert repo.begin_request("user_123", "req_1").outcome is BeginOutcome.ACQUIRED


def test_stale_in_flight_reservation_can_be_stolen():
    """A crashed handler must not hold the key forever."""
    repo = InMemoryRepository()
    repo.begin_request("user_123", "req_1")
    import time

    time.sleep(0.02)
    result = repo.begin_request("user_123", "req_1", stale_after_s=0.01)
    assert result.outcome is BeginOutcome.ACQUIRED  # stolen


def test_concurrent_duplicates_exactly_one_wins():
    """16 threads race the same (user, request_id): exactly one may process it.
    This is the regression test for the classic check-then-act idempotency race."""
    repo = InMemoryRepository()
    results: list = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(repo.begin_request("user_123", "req_race").outcome)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(BeginOutcome.ACQUIRED) == 1
    assert results.count(BeginOutcome.IN_PROGRESS) == 15
    assert results.count(BeginOutcome.COMPLETED) == 0


def test_same_request_id_across_users_independent():
    repo = InMemoryRepository()
    assert repo.begin_request("user_123", "req_shared").outcome is BeginOutcome.ACQUIRED
    assert repo.begin_request("user_999", "req_shared").outcome is BeginOutcome.ACQUIRED


def test_cas_refuses_write_on_stale_snapshot():
    repo = InMemoryRepository()
    repo.record_intensity_change(
        "workout_123", 80, actor="user_123", request_id="req_cas", expected_current=100
    )
    assert repo.get_workout("workout_123").intensity == 80

    # Stale snapshot: the caller still thinks intensity is 100.
    with pytest.raises(RepositoryError):
        repo.record_intensity_change(
            "workout_123", 60, actor="user_123", request_id="req_cas2", expected_current=100
        )
    assert repo.get_workout("workout_123").intensity == 80  # untouched by refused write


def test_get_workout_returns_defensive_copy():
    repo = InMemoryRepository()
    snapshot = repo.get_workout("workout_123")
    snapshot.intensity = 0  # try to corrupt "the database" through the copy
    assert repo.get_workout("workout_123").intensity == 100


def test_audit_log_records_server_derived_actor():
    repo = InMemoryRepository()
    repo.record_intensity_change(
        "workout_123", 80, actor="user_123", request_id="req_audit"
    )
    entry = repo.audit_log[0]
    assert entry["actor"] == "user_123"
    assert entry["request_id"] == "req_audit"
    assert entry["new_intensity"] == 80