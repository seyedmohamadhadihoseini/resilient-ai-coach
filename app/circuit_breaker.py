"""Circuit Breaker pattern for external LLM provider calls.

Prevents worker exhaustion, token waste, and downstream cascading failures:
- CLOSED: Normal operation. All calls pass through.
- OPEN: Threshold failures exceeded. All calls immediately fail-fast with CircuitBreakerOpenError.
- HALF_OPEN: Cooldown elapsed. Allows a single canary call to test provider recovery.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Callable, TypeVar

from app.errors import CircuitBreakerOpenError, ProviderTransientError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.last_failure_time = 0.0

    def call(self, operation: Callable[[], T]) -> T:
        """Executes an LLM operation protected by the circuit breaker state machine."""
        now = time.time()

        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout_s:
                logger.info("Circuit breaker transitioning from OPEN to HALF_OPEN (probing provider)")
                self.state = CircuitState.HALF_OPEN
            else:
                remaining = self.recovery_timeout_s - (now - self.last_failure_time)
                logger.warning(
                    "Circuit breaker is OPEN. Fast-failing request (cooldown remaining: %.1fs)",
                    remaining,
                )
                raise CircuitBreakerOpenError(
                    f"LLM Provider circuit is OPEN; fast-failing call. Retry in {remaining:.1f}s."
                )

        try:
            result = operation()
            self._on_success()
            return result
        except ProviderTransientError:
            self._on_transient_failure()
            raise

    def _on_success(self) -> None:
        if self.state != CircuitState.CLOSED:
            logger.info("Provider recovered! Circuit breaker reset to CLOSED.")
        self.consecutive_failures = 0
        self.state = CircuitState.CLOSED

    def _on_transient_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        if self.consecutive_failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.error(
                "Circuit breaker tripped to OPEN after %d consecutive transient failures",
                self.consecutive_failures,
            )