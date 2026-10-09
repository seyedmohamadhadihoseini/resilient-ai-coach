"""Tests proving recovery from Lost Acknowledgements and In-Flight Failures.

Answers Axel's primary critique:
'What happens if the side effect succeeds, but the process crashes before
the client receives the response?'
"""

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers import MockLLMProvider
from app.repository import BeginOutcome, LedgerStatus, SQLiteRepository
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


def test_lost_acknowledgement_replays_exact_response_without_reapplying(tmp_path: Path):
    """Scenario:
    1. Client issues request 'req_lost_ack'.
    2. Server executes DB write (100 -> 80) and commits ledger.
    3. Network drops before client reads response.
    4. Client retries with identical request_id.
    5. Server returns cached response; DB stays 80; LLM is NOT consulted again.
    """
    db_file = str(tmp_path / "lost_ack.db")
    settings = Settings(sqlite_db_path=db_file)
    repo = SQLiteRepository(db_path=db_file)
    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    service = AICoachService(provider=provider, repo=repo, settings=settings)

    req = CoachMessageRequest(
        request_id="req_lost_ack",
        message="please make it easier",
        context=RequestContext(workout_id="workout_123", energy_level=2),
    )

    # First attempt: completes and commits
    first_resp = service.process_message("user_123", req)
    assert first_resp.status == "success"
    assert first_resp.action.new_intensity == 80
    assert repo.get_workout("workout_123").intensity == 80
    assert provider.calls == 1

    # Simulate client never got the response (dropped connection) and retries
    retry_resp = service.process_message("user_123", req)

    # Assertions:
    # 1. Returned payload is identical
    assert retry_resp.status == first_resp.status
    assert retry_resp.action == first_resp.action
    # 2. Database mutation was NOT applied a second time (80 -> 64 must NOT happen!)
    assert repo.get_workout("workout_123").intensity == 80
    # 3. Model was NOT consulted on retry
    assert provider.calls == 1

    # Verify ledger state in DB
    entry = repo.get_ledger_entry("user_123", "req_lost_ack")
    assert entry is not None
    assert entry.status == LedgerStatus.COMMITTED.value


def test_heartbeat_extends_active_lease():
    repo = SQLiteRepository()
    user_id = "user_123"
    req_id = "req_slow_worker"

    # Acquire reservation with a very short lease
    res = repo.begin_request(user_id, req_id, stale_after_s=0.05)
    assert res.outcome is BeginOutcome.ACQUIRED

    # Heartbeat extends lease by 1.0 second
    success = repo.heartbeat(user_id, req_id, extension_s=1.0)
    assert success is True

    # Sleep past the original 0.05s timeout
    time.sleep(0.06)

    # Re-checking within the new lease must still return IN_PROGRESS
    check = repo.begin_request(user_id, req_id, stale_after_s=0.05)
    assert check.outcome is BeginOutcome.IN_PROGRESS


def test_lost_ack_survives_complete_server_restart(tmp_path: Path):
    """Proves durable recovery across process restarts on disk."""
    db_file = str(tmp_path / "restart.db")
    settings = Settings(sqlite_db_path=db_file)

    # Process 1: Starts, processes request, commits, and exits
    repo1 = SQLiteRepository(db_path=db_file)
    provider1 = MockLLMProvider()
    provider1.raw_response = PLAN_20_PCT
    service1 = AICoachService(provider=provider1, repo=repo1, settings=settings)

    req = CoachMessageRequest(
        request_id="req_restart_1",
        message="reduce load",
        context=RequestContext(workout_id="workout_123"),
    )
    resp1 = service1.process_message("user_123", req)
    assert resp1.action.new_intensity == 80

    # Process 1 dies. Process 2 spins up from cold disk state:
    repo2 = SQLiteRepository(db_path=db_file)
    provider2 = MockLLMProvider()
    service2 = AICoachService(provider=provider2, repo=repo2, settings=settings)

    # Client retries on Process 2
    resp2 = service2.process_message("user_123", req)

    assert resp2.status == "success"
    assert resp2.action.new_intensity == 80
    assert repo2.get_workout("workout_123").intensity == 80
    assert provider2.calls == 0  # Provider in new process never even touched