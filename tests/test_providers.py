"""Provider-layer tests using httpx.MockTransport.

These prove the REAL provider class (OpenAI-compatible -- what actually runs
when LLM_PROVIDER=openai) behaves exactly as the error taxonomy promises,
without network access or credentials. The final test wires that same class
through the entire service stack: raw completion -> parse -> validate -> execute.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import (
    MalformedModelOutputError,
    ProviderTerminalError,
    ProviderTransientError,
)
from app.main import create_app
from app.providers import OpenAICompatibleProvider
from app.repository import InMemoryRepository

PLAN = {
    "assistant_message": "Reducing today's session.",
    "intent": "modify_workout",
    "action": {
        "type": "reduce_workout_intensity",
        "workout_id": "workout_123",
        "reduction_percentage": 20,
    },
    "warnings": [],
}


def make_provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test-key",
        model="gpt-test",
        base_url="https://api.test/v1",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )


def openai_envelope(content) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --- taxonomy mapping ---------------------------------------------------------

def test_happy_path_returns_raw_content_and_requests_json_mode():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["payload"] = json.loads(request.read())
        return openai_envelope(json.dumps(PLAN))

    raw = make_provider(handler).generate([{"role": "user", "content": "hi"}])

    # The provider returns RAW text and nothing else: parsing is NOT its job.
    assert raw == json.dumps(PLAN)
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["model"] == "gpt-test"


def test_429_is_transient_and_surfaces_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "rate limited"})

    with pytest.raises(ProviderTransientError) as excinfo:
        make_provider(handler).generate([{"role": "user", "content": "hi"}])
    assert "retry-after=7" in str(excinfo.value)


def test_5xx_is_transient():
    def handler(request):
        return httpx.Response(503, json={"error": "overloaded"})

    with pytest.raises(ProviderTransientError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


def test_other_4xx_is_terminal():
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(ProviderTerminalError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ProviderTransientError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


def test_connection_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(ProviderTransientError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


def test_unrecognised_envelope_is_flagged():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(MalformedModelOutputError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


def test_non_string_content_is_flagged():
    def handler(request):
        return openai_envelope({"oops": "not a string"})

    with pytest.raises(MalformedModelOutputError):
        make_provider(handler).generate([{"role": "user", "content": "hi"}])


# --- the real provider class through the FULL stack ----------------------------

def test_full_stack_with_real_provider_class():
    """Production integration path demonstrated end to end: OpenAICompatibleProvider
    fed by a scripted HTTP transport, through service validation to state change."""

    def handler(request):
        return openai_envelope(json.dumps(PLAN))

    settings = Settings(llm_provider="openai", max_attempts=2, retry_base_delay_s=0.0)
    client = TestClient(
        create_app(settings=settings, provider=make_provider(handler), repo=InMemoryRepository())
    )
    response = client.post(
        "/ai/coach/message",
        json={"request_id": "req_prov_1", "message": "easier please",
              "context": {"sleep_hours": 5, "energy_level": 2}},
        headers={"X-User-Id": "user_123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["action"]["new_intensity"] == 80