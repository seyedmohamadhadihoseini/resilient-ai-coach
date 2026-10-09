"""Chaos tests proving resilience when a process crashes immediately after DB commit."""

import json
import time
from pathlib import Path
import pytest

from app.chaos import ChaosHook, ChaosPoint, SimulatedCrashError
from app.config import Settings
from app.providers import MockLLMProvider
from app.repository import SQLiteRepository
from app.schemas import CoachMessageRequest, RequestContext
from app.service import AICoachService

PLAN_20_PCT = json.dumps({
    "assistant_message": "Session reduced by 20%.",
    "intent": "modify_workout",
    "action": {
        "type": "reduce_workout_intensity",
        "workout_id": "workout_123",
        "reduction_percentage": 20,
    },
    "warnings": [],
})


def test_crash_after_commit_recovers_safely_on_next_attempt(tmp_path: Path):
    """THE CRITICAL TEST FOR AXEL:
    1. Transaction commits to SQLite WAL.
    2. Process crashes before sending the response to the caller (SimulatedCrashError).
    3. Caller receives a dropped connection / timeout and retries with the same request_id.
    4. Fresh service recovers the committed response from disk without double-reducing.
    """
    db_file = str(tmp_path / "chaos_test.db")
    settings = Settings(sqlite_db_path=db_file)
    repo = SQLiteRepository(db_path=db_file)
    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    chaos = ChaosHook()
    # Arm the hook to simulate crash immediately after the atomic commit
    chaos.arm(ChaosPoint.AFTER_COMMIT_BEFORE_RESPONSE)

    service = AICoachService(
        provider=provider, repo=repo, settings=settings, chaos_hook=chaos
    )

    req = CoachMessageRequest(
        request_id="req_chaos_post_commit",
        message="reduce workout",
        context=RequestContext(workout_id="workout_123"),
    )

    # First attempt: Crash happens
    with pytest.raises(SimulatedCrashError):
        service.process_message("user_123", req)

    # The commit succeeded before the crash:
    assert repo.get_workout("workout_123").intensity == 80

    # RECOVERY PHASE: New process/service instance comes up (new worker or restarted process)
    recovered_repo = SQLiteRepository(db_path=db_file)
    recovered_service = AICoachService(
        provider=provider, repo=recovered_repo, settings=settings
    )

    # Client retries with the same request_id
    recovered_resp = recovered_service.process_message("user_123", req)

    # Verifications:
    # 1. Successful response returned
    assert recovered_resp.status == "success"
    assert recovered_resp.action.new_intensity == 80
    # 2. Workout intensity was NOT decreased again (MUST NOT BE 64)
    assert recovered_repo.get_workout("workout_123").intensity == 80
    # 3. Provider was only called during the first attempt, never on recovery replay
    assert provider.calls == 1


def test_crash_before_commit_rolls_back_and_allows_clean_retry(tmp_path: Path):
    """Crash occurs before the commit:
    1. DB stays clean.
    2. Reservation is held until lease expires (preventing premature duplicates).
    3. Once lease expires, client retry safely reclaims the slot and succeeds.
    """
    db_file = str(tmp_path / "chaos_pre_commit.db")
    settings = Settings(sqlite_db_path=db_file, reservation_ttl_s=0.02)
    repo = SQLiteRepository(db_path=db_file)
    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    chaos = ChaosHook()
    chaos.arm(ChaosPoint.BEFORE_COMMIT)

    service = AICoachService(
        provider=provider, repo=repo, settings=settings, chaos_hook=chaos
    )

    req = CoachMessageRequest(
        request_id="req_chaos_pre_commit",
        message="reduce workout",
        context=RequestContext(workout_id="workout_123"),
    )

    with pytest.raises(SimulatedCrashError):
        service.process_message("user_123", req)

    # Database was untouched
    assert repo.get_workout("workout_123").intensity == 100

    # Wait for the crashed worker's lease to expire
    time.sleep(0.03)

    # Disarm chaos and retry
    chaos.disarm(ChaosPoint.BEFORE_COMMIT)
    retry_resp = service.process_message("user_123", req)
    assert retry_resp.status == "success"
    assert repo.get_workout("workout_123").intensity == 80