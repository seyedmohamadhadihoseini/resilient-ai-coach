"""Shared fixtures.

Every test builds its own app instance wired to a fresh, isolated SQLite database
and a scriptable mock provider.
"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers import MockLLMProvider
from app.repository import SQLiteRepository


@pytest.fixture
def provider(monkeypatch) -> MockLLMProvider:
    monkeypatch.delenv("MOCK_PLAN_JSON", raising=False)
    return MockLLMProvider()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Creates a temporary, unique database file path per test."""
    return str(tmp_path / "test_ai_coach.db")


@pytest.fixture
def repo(db_path: str) -> SQLiteRepository:
    return SQLiteRepository(db_path=db_path)


@pytest.fixture
def settings(db_path: str) -> Settings:
    return Settings(
        llm_provider="mock",
        max_attempts=3,
        retry_base_delay_s=0.0,
        sqlite_db_path=db_path,
    )


@pytest.fixture
def client(
    provider: MockLLMProvider, repo: SQLiteRepository, settings: Settings
) -> TestClient:
    return TestClient(create_app(settings=settings, provider=provider, repo=repo))


@pytest.fixture
def headers() -> dict:
    return {"X-User-Id": "user_123"}