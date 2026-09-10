"""End-to-end scenarios.

The naming maps each test to the evaluation brief (A-E are mandatory; F+ are
extra proof). Every security claim in the README is backed by at least one
failing-without-the-fix test here.
"""

import json

from fastapi.testclient import TestClient

from app.errors import ProviderTerminalError, ProviderTransientError

BASE_CTX = {"sleep_hours": 4.5, "energy_level": 3}


# --- helpers -----------------------------------------------------------------

def plan_json(action=None, message="Here's my take.", intent="modify_workout", warnings=None, **extra) -> str:
    """Build a raw model completion exactly as a real provider would emit it."""
    body = {
        "assistant_message": message,
        "intent": intent,
        "action": action,
        "warnings": warnings or [],
    }
    body.update(extra)
    return json.dumps(body)


def reduce_action(workout_id="workout_123", pct=20, **extra) -> dict:
    action = {"type": "reduce_workout_intensity", "workout_id": workout_id, "reduction_percentage": pct}
    action.update(extra)
    return action


def post_message(client: TestClient, request_id: str, user: str = "user_123",
                 message: str = "make today easier", ctx: dict | None = None) -> dict:
    payload = {"request_id": request_id, "message": message, "context": ctx or BASE_CTX}
    response = client.post(
        "/ai/coach/message", json=payload, headers={"X-User-Id": user} if user else {}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- A. Normal request --------------------------------------------------------

def test_a_normal_request_executes_validated_action(client, provider, repo):
    provider.raw_response = plan_json(action=reduce_action("workout_123", 20))
    body = post_message(client, "req_a1")

    assert body["status"] == "success"
    # The client sees server-verified FACTS, not the model's claim.
    assert body["action"] == {
        "type": "reduce_workout_intensity",
        "workout_id": "workout_123",
        "previous_intensity": 100,
        "new_intensity": 80,
    }
    assert repo.get_workout("workout_123").intensity == 80
    assert repo.audit_log[0]["actor"] == "user_123"


def test_a2_question_without_action_changes_nothing(client, provider, repo):
    provider.raw_response = plan_json(intent="answer_question", action=None,
                                      message="You need about 40g more protein today.")
    body = post_message(client, "req_a2", message="how much protein do I still need?")

    assert body["status"] == "success" and body["action"] is None
    assert repo.get_workout("workout_123").intensity == 100


# --- B. Semantic failures ------------------------------------------------------

def test_b_semantic_failure_900pct_rejected(client, provider, repo):
    provider.raw_response = plan_json(action=reduce_action("workout_123", 900))
    body = post_message(client, "req_b1")

    assert body["status"] == "rejected"
    assert body["action"] is None
    assert repo.get_workout("workout_123").intensity == 100  # untouched


def test_b2_rejections_are_final_decisions_and_cached(client, provider, repo):
    """A rejection IS an answer. Replaying the same request_id must return the
    same verdict without re-consulting the (non-deterministic) model."""
    provider.raw_response = plan_json(action=reduce_action("workout_123", 900))
    post_message(client, "req_b2")
    calls_after_first = provider.calls

    body = post_message(client, "req_b2")  # replay
    assert provider.calls == calls_after_first  # model NOT consulted again
    assert body["status"] == "rejected"


def test_b3_reduction_below_safe_floor_rejected(client, provider, repo):
    # workout_789 sits at intensity 15; 40% off leaves 9, below the floor of 10.
    ctx = {**BASE_CTX, "workout_id": "workout_789"}
    provider.raw_response = plan_json(action=reduce_action("workout_789", 40))
    body = post_message(client, "req_b3", ctx=ctx)

    assert body["status"] == "rejected"
    assert repo.get_workout("workout_789").intensity == 15


def test_b4_rounding_to_noop_rejected(client, provider, repo):
    # 3% of 15 floors to 0 points: a "success" that changed nothing is a lie.
    ctx = {**BASE_CTX, "workout_id": "workout_789"}
    provider.raw_response = plan_json(action=reduce_action("workout_789", 3))
    assert post_message(client, "req_b4", ctx=ctx)["status"] == "rejected"


def test_b5_valid_small_reduction_still_works(client, provider, repo):
    ctx = {**BASE_CTX, "workout_id": "workout_789"}
    provider.raw_response = plan_json(action=reduce_action("workout_789", 20))  # -3 -> 12
    body = post_message(client, "req_b5", ctx=ctx)

    assert body["status"] == "success"
    assert body["action"]["new_intensity"] == 12


# --- C. Authorization failures ---------------------------------------------------

def test_c_cross_user_action_blocked(client, provider, repo):
    """Model tries to modify a workout owned by someone else."""
    provider.raw_response = plan_json(action=reduce_action("workout_456", 20))  # owner: user_999
    body = post_message(client, "req_c1")  # caller is user_123

    assert body["status"] == "rejected"
    assert repo.get_workout("workout_456").intensity == 100


def test_c2_context_pinning_blocks_even_same_user_targets(client, provider, repo):
    """Defense in depth beyond ownership: the app pinned the session to
    workout_123, so the model may not redirect to workout_789 -- even though
    user_123 owns BOTH. The app decides which resource is on the table."""
    ctx = {**BASE_CTX, "workout_id": "workout_123"}
    provider.raw_response = plan_json(action=reduce_action("workout_789", 20))
    body = post_message(client, "req_c2", ctx=ctx)

    assert body["status"] == "rejected"
    assert repo.get_workout("workout_789").intensity == 15  # untouched


def test_c3_model_claimed_identity_is_structurally_ignored(client, provider, repo):
    """The brief's exact attack: model outputs user_id=user_999. The plan schema
    has no identity field, so the claim is DROPPED at the parse boundary and the
    action still runs as the authenticated caller."""
    provider.raw_response = plan_json(
        action=reduce_action("workout_123", 20), user_id="user_999"
    )
    body = post_message(client, "req_c3")

    assert body["status"] == "success"
    assert repo.audit_log[0]["actor"] == "user_123"  # executed FOR THE CALLER


# --- D. Duplicate requests ---------------------------------------------------------

def test_d_duplicate_request_id_executes_once(client, provider, repo):
    provider.raw_response = plan_json(action=reduce_action("workout_123", 20))
    first = post_message(client, "req_d1")
    assert first["action"]["new_intensity"] == 80

    # Make any second LLM call detectable: default plan would still succeed, so
    # assert on call count instead of outcome.
    calls_after_first = provider.calls
    second = post_message(client, "req_d1")

    assert provider.calls == calls_after_first       # provider not consulted again
    assert second["action"]["new_intensity"] == 80   # replayed facts, not recomputed
    assert repo.get_workout("workout_123").intensity == 80  # reduced exactly once


def test_d2_same_request_id_across_users_is_isolated(client, provider, repo):
    """Idempotency keys are (user, request_id). user_999's retry must never be
    served user_123's cached response -- and vice versa."""
    provider.raw_response = plan_json(action=reduce_action("workout_123", 20))
    post_message(client, "req_shared_1", user="user_123")

    # user_999 reuses the same request_id for THEIR OWN workout.
    provider.raw_response = plan_json(action=reduce_action("workout_456", 20))
    body = post_message(client, "req_shared_1", user="user_999")

    assert provider.calls == 2                                  # processed independently
    assert body["action"]["workout_id"] == "workout_456"
    assert repo.get_workout("workout_123").intensity == 80
    assert repo.get_workout("workout_456").intensity == 80


# --- E. Provider failures & safe fallback ---------------------------------------------

def test_e_transient_failure_exhausts_bounded_retries_then_falls_back(client, provider, repo):
    provider.script = [
        ProviderTransientError("504 timeout"),
        ProviderTransientError("429 rate limited"),
        ProviderTransientError("503 unavailable"),
    ]
    body = post_message(client, "req_e1")

    assert body["status"] == "fallback"
    assert body["action"] is None
    assert provider.calls == 3                          # bounded: exactly max_attempts
    assert repo.get_workout("workout_123").intensity == 100  # nothing executed


def test_e2_fallback_is_not_pinned_client_may_retry(client, provider, repo):
    """Fallback releases the reservation: once the provider recovers, the SAME
    request_id can be processed successfully. (Caching the fallback would pin
    the user to 'couldn't process' forever -- see README trade-off.)"""
    provider.script = [ProviderTransientError("504"), ProviderTransientError("504"),
                       ProviderTransientError("504")]
    assert post_message(client, "req_e2")["status"] == "fallback"

    provider.raw_response = plan_json(action=reduce_action("workout_123", 20))
    body = post_message(client, "req_e2")  # same request_id, provider recovered

    assert body["status"] == "success"
    assert repo.get_workout("workout_123").intensity == 80


def test_e3_terminal_error_never_retried(client, provider, repo):
    provider.script = [ProviderTerminalError("401 invalid api key")]
    body = post_message(client, "req_e3")

    assert body["status"] == "fallback"
    assert provider.calls == 1  # retrying 4xx/auth errors cannot help


# --- F. Malformed model output (structural boundary) ------------------------------------

def test_f_valid_json_wrong_shape_is_retryable_then_succeeds(client, provider, repo):
    """Models are non-deterministic: two malformed samples, then a good one.
    Malformed output must be treated like a transient failure -- retryable,
    because nothing has executed yet."""
    provider.script = ['{"assistant_message": ', "not json at all"]
    provider.raw_response = plan_json(action=reduce_action("workout_123", 20))
    body = post_message(client, "req_f1")

    assert body["status"] == "success"
    assert provider.calls == 3
    assert repo.get_workout("workout_123").intensity == 80


def test_f2_permanently_malformed_output_falls_back(client, provider, repo):
    provider.raw_response = "[1, 2, 3]"   # every re-sample returns the same junk
    provider.script = ["[1, 2, 3]"]
    body = post_message(client, "req_f2")

    assert body["status"] == "fallback"
    assert provider.calls == 3
    assert repo.get_workout("workout_123").intensity == 100


def test_f3_type_coercion_is_not_trusted(client, provider, repo):
    """Strict parsing: the model sends reduction_percentage as a STRING "20".
    A lenient boundary would coerce and execute. Ours refuses: at an untrusted
    boundary we want the type the model actually sent."""
    provider.raw_response = plan_json(action=reduce_action("workout_123", "20"))
    body = post_message(client, "req_f3")

    assert body["status"] == "fallback"   # never parses -> never executes
    assert repo.get_workout("workout_123").intensity == 100


def test_f4_model_warnings_are_bounded_before_reaching_clients(client, provider, repo):
    provider.raw_response = plan_json(
        action=reduce_action("workout_123", 20),
        warnings=["x" * 5000, "sleep is low"],
    )
    body = post_message(client, "req_f4")

    assert body["status"] == "success"
    assert len(body["warnings"][0]) == 200
    assert body["warnings"][1] == "sleep is low"


# --- G. API-level security & validation ---------------------------------------------------
# --- G. API-level security & validation -------------------------------------

def test_g_missing_identity_header_is_rejected_fail_closed(client):
    response = client.post(
        "/ai/coach/message",
        json={"request_id": "req_g1", "message": "hi", "context": BASE_CTX},
    )
    assert response.status_code == 401  # no default user, ever


def test_g2_identity_gate_runs_before_body_parsing(client):
    """Even a MALFORMED body gets 401 (not 422) without the identity header:
    the auth boundary lives in middleware, upstream of body validation --
    exactly where a real upstream auth layer would sit."""
    response = client.post(
        "/ai/coach/message",
        content="{ definitely not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_g3_invalid_request_shapes_are_422(client, headers):
    bad_payloads = [
        {"request_id": "short", "message": "hi", "context": BASE_CTX},
        {"request_id": "has spaces!", "message": "hi", "context": BASE_CTX},
        {"request_id": "req_ok_123", "message": "x" * 2001, "context": BASE_CTX},
        {"request_id": "req_ok_123", "message": "hi", "context": {"energy_level": 99}},
    ]
    for payload in bad_payloads:
        assert client.post("/ai/coach/message", json=payload, headers=headers).status_code == 422


def test_g4_health(client):
    assert client.get("/health").json() == {"status": "ok"}