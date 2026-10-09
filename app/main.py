"""HTTP layer: thin by design.

Responsibilities kept here (and only here):
- transport concerns (HTTP status codes, headers),
- fail-closed extraction of the server-derived identity,
- application composition (settings -> provider -> repository -> service).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings, load_settings
from app.errors import RequestInProgressError
from app.providers import LLMProvider, MockLLMProvider, OpenAICompatibleProvider
from app.repository import SQLiteRepository
from app.schemas import CoachMessageRequest, CoachResponse
from app.service import AICoachService

logger = logging.getLogger(__name__)


def build_provider(settings: Settings) -> LLMProvider:
    """Provider selection happens exactly once, at startup (fail fast)."""
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai requires OPENAI_API_KEY to be set")
        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_s=settings.llm_timeout_s,
        )
    if settings.llm_provider != "mock":
        raise RuntimeError(f"unknown LLM_PROVIDER: {settings.llm_provider}")
    logger.info("Using MockLLMProvider (default; no external calls)")
    return MockLLMProvider()


def create_app(
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
    repo: Optional[SQLiteRepository] = None,
) -> FastAPI:
    settings = settings or load_settings()
    provider = provider or build_provider(settings)
    repo = repo or SQLiteRepository(db_path=settings.sqlite_db_path)
    service = AICoachService(provider=provider, repo=repo, settings=settings)

    app = FastAPI(
        title="AI Coach Service",
        version="1.0.0",
        description="Defensive AI Coach integration: the model proposes, the server disposes.",
    )

    PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}

    @app.middleware("http")
    async def identity_gate(request: Request, call_next):
        """Simulates upstream auth middleware: fail-closed before routing & body parsing."""
        if request.url.path not in PUBLIC_PATHS:
            user_id = request.headers.get("x-user-id")
            if not user_id:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "missing authenticated identity"},
                )
            request.state.user_id = user_id
        return await call_next(request)

    def require_user(request: Request) -> str:
        user_id = getattr(request.state, "user_id", None)
        if not user_id:
            raise HTTPException(status_code=401, detail="missing authenticated identity")
        return user_id

    @app.post("/ai/coach/message", response_model=CoachResponse)
    def coach_message(
        payload: CoachMessageRequest,
        user_id: str = Depends(require_user),
    ) -> CoachResponse:
        try:
            return service.process_message(user_id, payload)
        except RequestInProgressError:
            raise HTTPException(
                status_code=409,
                detail="an identical request is already being processed; retry shortly",
            )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def unhandled_exception(request, exc):
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

app = create_app()