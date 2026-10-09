"""Tests for Append-Only Domain Event Sourcing and State Reconstruction."""

import json
from app.config import Settings
from app.providers import MockLLMProvider
from app.repository import SQLiteRepository
from app.schemas import CoachMessageRequest, RequestContext
from app.service import AICoachService

PLAN_20_PCT = json.dumps({
    "assistant_message": "Reducing intensity by 20%.",
    "intent": "modify_workout",
    "action": {
        "type": "reduce_workout_intensity",
        "workout_id": "workout_123",
        "reduction_percentage": 20,
    },
    "warnings": [],
})


def test_state_mutation_creates_immutable_domain_event():
    repo = SQLiteRepository()
    settings = Settings()
    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    service = AICoachService(provider=provider, repo=repo, settings=settings)

    req = CoachMessageRequest(
        request_id="req_event_sourcing_1",
        message="make workout easier",
        context=RequestContext(workout_id="workout_123"),
    )

    resp = service.process_message("user_123", req)
    assert resp.status == "success"

    # Fetch recorded events
    events = repo.get_workout_events("workout_123")
    assert len(events) == 1

    ev = events[0]
    assert ev.event_type == "WORKOUT_INTENSITY_REDUCED"
    assert ev.actor == "user_123"
    assert ev.request_id == "req_event_sourcing_1"
    assert ev.payload["previous_intensity"] == 100
    assert ev.payload["new_intensity"] == 80
    assert ev.payload["delta"] == 20


def test_reconstruct_state_from_event_replay():
    """Proves that aggregate intensity can be reconstructed entirely from event history."""
    repo = SQLiteRepository()
    settings = Settings()
    provider = MockLLMProvider()
    provider.raw_response = PLAN_20_PCT

    service = AICoachService(provider=provider, repo=repo, settings=settings)

    # 1st reduction: 100 -> 80
    req1 = CoachMessageRequest(
        request_id="req_ev_seq_1",
        message="reduce 1",
        context=RequestContext(workout_id="workout_123"),
    )
    service.process_message("user_123", req1)

    # Replay purely from events
    replayed_intensity = repo.replay_workout_intensity("workout_123", baseline_intensity=100)
    assert replayed_intensity == 80
    assert repo.get_workout("workout_123").intensity == replayed_intensity