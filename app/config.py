"""Runtime configuration, loaded from environment variables.

Kept deliberately tiny: this is a Stage 1 service, not a platform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # "mock" (default -- no credentials needed) or "openai" (any OpenAI-compatible API).
    llm_provider: str = "mock"

    # Real-provider settings; only used when llm_provider == "openai".
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    llm_timeout_s: float = 8.0
    reservation_ttl_s: float = 120.0  # max time an in-flight idempotency reservation is held

    # Bounded retry policy for transient provider failures.
    max_attempts: int = 3
    retry_base_delay_s: float = 0.25  # exponential backoff base; tests set 0.0

    # Persistence settings
    sqlite_db_path: str = "ai_coach.db"

    # Circuit breaker settings
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout_s: float = 30.0


def load_settings() -> Settings:
    return Settings(
        llm_provider=_env("LLM_PROVIDER", "mock").strip().lower(),
        openai_api_key=_env("OPENAI_API_KEY", ""),
        openai_model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        llm_timeout_s=float(_env("LLM_TIMEOUT_S", "8")),
        reservation_ttl_s=float(_env("RESERVATION_TTL_S", "120.0")),
        max_attempts=int(_env("LLM_MAX_ATTEMPTS", "3")),
        retry_base_delay_s=float(_env("LLM_RETRY_BASE_DELAY_S", "0.25")),
        sqlite_db_path=_env("SQLITE_DB_PATH", "ai_coach.db"),
        circuit_breaker_failure_threshold=int(
            _env("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
        ),
        circuit_breaker_recovery_timeout_s=float(
            _env("CIRCUIT_BREAKER_RECOVERY_TIMEOUT_S", "30.0")
        ),
    )