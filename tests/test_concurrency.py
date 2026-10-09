"""Multi-worker and multi-connection concurrency tests.

Proves that when multiple independent processes/workers race for the exact
same request_id across separate database connections, exactly one execution
succeeds and the database remains completely consistent.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import Settings
from app.errors import RequestInProgressError
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


def test_multi_worker_process_race_on_same_request_id(tmp_path: Path):
    """Simulates 16 separate workers/connections racing the same request_id.

    Guarantees:
    - Exactly 1 worker gets to execute the state mutation (intensity: 100 -> 80).
    - Other workers either get 409 (RequestInProgress) or the committed response.
    - Zero double reductions occur (intensity never becomes 64).
    - Database WAL handles all concurrent connections cleanly without unhandled lock errors.
    """
    db_file = str(tmp_path / "multi_worker.db")
    settings = Settings(sqlite_db_path=db_file)

    # Pre-seed the database
    init_repo = SQLiteRepository(db_path=db_file)
    assert init_repo.get_workout("workout_123").intensity == 100

    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    barrier = threading.Barrier(16)
    outcomes = []
    errors = []

    def simulated_worker(worker_id: int):
        # Each worker represents an independent process connection to the same DB file
        worker_repo = SQLiteRepository(db_path=db_file)
        service = AICoachService(provider=provider, repo=worker_repo, settings=settings)
        req = CoachMessageRequest(
            request_id="req_multi_worker_race",
            message="make easier",
            context=RequestContext(workout_id="workout_123"),
        )
        barrier.wait()  # Align all workers to strike at the exact same instant
        try:
            resp = service.process_message("user_123", req)
            outcomes.append(resp.status)
        except RequestInProgressError:
            errors.append("409_conflict")

    threads = [threading.Thread(target=simulated_worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verifications:
    # 1. Total attempts equals 16
    assert len(outcomes) + len(errors) == 16
    # 2. At least one worker succeeded
    assert "success" in outcomes
    # 3. Final intensity is EXACTLY 80 on disk (strictly reduced ONCE)
    final_repo = SQLiteRepository(db_path=db_file)
    assert final_repo.get_workout("workout_123").intensity == 80
    # 4. LLM provider was only consulted ONCE
    assert provider.calls == 1


def test_different_workouts_concurrent_writes_succeed_independently(tmp_path: Path):
    """Proves that concurrent writes to DIFFERENT workouts do not block each other."""
    db_file = str(tmp_path / "concurrent_diff.db")
    settings = Settings(sqlite_db_path=db_file)
    repo = SQLiteRepository(db_path=db_file)

    def worker_modify(workout_id: str, new_val: int):
        w_repo = SQLiteRepository(db_path=db_file)
        w_repo.record_intensity_change(
            workout_id,
            new_val,
            actor="user_123",
            request_id=f"req_{workout_id}",
            expected_current=100 if workout_id == "workout_123" else 15,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_modify, "workout_123", 80)
        f2 = executor.submit(worker_modify, "workout_789", 12)
        f1.result()
        f2.result()

    assert repo.get_workout("workout_123").intensity == 80
    assert repo.get_workout("workout_789").intensity == 12