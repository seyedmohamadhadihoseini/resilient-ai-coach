"""Tests verifying Circuit Breaker behavior and fast-failing resilience."""

import time
import pytest

from app.circuit_breaker import CircuitBreaker, CircuitState
from app.config import Settings
from app.errors import CircuitBreakerOpenError, ProviderTransientError
from app.providers import MockLLMProvider
from app.repository import SQLiteRepository
from app.schemas import CoachMessageRequest, RequestContext
from app.service import AICoachService


def test_circuit_breaker_trips_to_open_after_threshold():
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_s=0.1)

    def failing_call():
        raise ProviderTransientError("503 Provider Down")

    # Failures 1 & 2: still CLOSED
    with pytest.raises(ProviderTransientError):
        breaker.call(failing_call)
    assert breaker.state == CircuitState.CLOSED

    with pytest.raises(ProviderTransientError):
        breaker.call(failing_call)
    assert breaker.state == CircuitState.CLOSED

    # Failure 3: Trips to OPEN
    with pytest.raises(ProviderTransientError):
        breaker.call(failing_call)
    assert breaker.state == CircuitState.OPEN

    # Call 4: Fast-fails immediately without calling operation
    with pytest.raises(CircuitBreakerOpenError):
        breaker.call(failing_call)


def test_circuit_breaker_recovers_via_half_open():
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.04)

    def failing_call():
        raise ProviderTransientError("504 Timeout")

    with pytest.raises(ProviderTransientError):
        breaker.call(failing_call)
    with pytest.raises(ProviderTransientError):
        breaker.call(failing_call)
    assert breaker.state == CircuitState.OPEN

    # Wait past cooldown timeout
    time.sleep(0.05)

    # Canary call succeeds -> resets to CLOSED
    result = breaker.call(lambda: "recovered_output")
    assert result == "recovered_output"
    assert breaker.state == CircuitState.CLOSED


def test_service_returns_safe_fallback_when_circuit_breaker_is_open():
    """Proves the service returns a safe fallback without crashing or calling provider when circuit is open."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_s=10.0)
    provider = MockLLMProvider()
    provider.script = [ProviderTransientError("500 Outage")]

    repo = SQLiteRepository()
    settings = Settings(max_attempts=1)
    service = AICoachService(
        provider=provider, repo=repo, settings=settings, circuit_breaker=breaker
    )

    req = CoachMessageRequest(
        request_id="req_breaker_fallback",
        message="make workout easier",
        context=RequestContext(workout_id="workout_123"),
    )

    # First attempt: trips breaker and falls back safely
    resp1 = service.process_message("user_123", req)
    assert resp1.status == "fallback"
    assert breaker.state == CircuitState.OPEN
    assert provider.calls == 1

    # Second attempt: circuit is OPEN -> fast fallback without consulting provider
    req2 = CoachMessageRequest(
        request_id="req_breaker_fallback_2",
        message="make workout easier",
        context=RequestContext(workout_id="workout_123"),
    )
    resp2 = service.process_message("user_123", req2)
    assert resp2.status == "fallback"
    # Calls stayed at 1 (provider was NOT hit again)
    assert provider.calls == 1