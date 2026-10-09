"""Tests proving Atomic Single-Transaction Commit guarantees.

Ensures that state-changing actions and idempotency records either:
1. Both succeed and persist together.
2. Both roll back completely on failure or concurrent collision.
"""

import pytest

from app.errors import RepositoryError
from app.repository import BeginOutcome, SQLiteRepository
from app.schemas import CoachResponse, ExecutedAction


def make_dummy_response(request_id: str, new_intensity: int) -> CoachResponse:
    return CoachResponse(
        status="success",
        assistant_message="Intensity reduced safely.",
        action=ExecutedAction(
            type="reduce_workout_intensity",
            workout_id="workout_123",
            previous_intensity=100,
            new_intensity=new_intensity,
        ),
        warnings=[],
        request_id=request_id,
    )


def test_atomic_commit_updates_workout_and_ledger_simultaneously():
    repo = SQLiteRepository()
    user_id = "user_123"
    request_id = "req_atomic_1"

    # Step 1: acquire reservation
    res = repo.begin_request(user_id, request_id)
    assert res.outcome is BeginOutcome.ACQUIRED

    # Step 2: commit state change + ledger atomically
    response = make_dummy_response(request_id, 80)
    repo.commit_workout_action_and_response(
        user_id,
        request_id,
        response,
        workout_id="workout_123",
        new_intensity=80,
        expected_current=100,
    )

    # Verify both workout and ledger are simultaneously committed
    workout = repo.get_workout("workout_123")
    assert workout is not None and workout.intensity == 80

    replay = repo.begin_request(user_id, request_id)
    assert replay.outcome is BeginOutcome.COMPLETED
    assert replay.response is not None
    assert replay.response.action is not None
    assert replay.response.action.new_intensity == 80


def test_atomic_commit_cas_mismatch_rolls_back_both():
    repo = SQLiteRepository()
    user_id = "user_123"
    request_id = "req_atomic_cas_fail"

    repo.begin_request(user_id, request_id)
    response = make_dummy_response(request_id, 70)

    # Expected current is wrong (90 instead of 100) -> CAS failure
    with pytest.raises(RepositoryError):
        repo.commit_workout_action_and_response(
            user_id,
            request_id,
            response,
            workout_id="workout_123",
            new_intensity=70,
            expected_current=90,
        )

    # Workout MUST be untouched
    workout = repo.get_workout("workout_123")
    assert workout is not None and workout.intensity == 100

    # Ledger must NOT be completed
    replay = repo.begin_request(user_id, request_id)
    assert replay.outcome is BeginOutcome.IN_PROGRESS