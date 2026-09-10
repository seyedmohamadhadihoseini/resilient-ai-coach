"""Shared fixtures.

Every test builds its own app instance wired to a fresh in-memory repository
and a scriptable mock provider. No global singletons, no dependency_override
hacks -- create_app() takes its collaborators as arguments, so isolation is a
constructor call away.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers import MockLLMProvider
from app.repository import InMemoryRepository


@pytest.fixture
def provider(monkeypatch) -> MockLLMProvider:
    monkeypatch.delenv("MOCK_PLAN_JSON", raising=False)
    return MockLLMProvider()


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def settings() -> Settings:
    # Zero retry delay: tests must be fast; bounded-retry behaviour is still
    # fully exercised (attempt counting is what matters, not wall-clock time).
    return Settings(llm_provider="mock", max_attempts=3, retry_base_delay_s=0.0)


@pytest.fixture
def client(provider: MockLLMProvider, repo: InMemoryRepository, settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings, provider=provider, repo=repo))


@pytest.fixture
def headers() -> dict:
    return {"X-User-Id": "user_123"}