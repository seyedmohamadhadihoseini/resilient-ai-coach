"""AICoachService -- the orchestration brain and the enforcement point.

This is the ONLY module that decides what happens to model output. The flow for
every request:

    1. Atomically reserve (user_id, request_id)  -> idempotency, no check-then-act race
    2. Generate a plan via the provider          -> bounded retries, taxonomy-driven
    3. Parse raw output into CoachPlan           -> the single untrusted-input boundary
    4. Validate semantics + authorization        -> server decides, model only suggests
    5. Execute via the repository                -> single state-changing write, audited
    6. Cache the final decision                  -> replays never re-consult the model

Failure philosophy: the model is a proposal generator. Every proposal that cannot
be *proven* safe is rejected; the service never crashes, never executes an
unvalidated action, and never lets provider failure mutate state.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from pydantic import ValidationError

from app.config import Settings
from app.errors import (
    AuthorizationError,
    MalformedModelOutputError,
    ProviderError,
    ProviderTerminalError,
    ProviderTransientError,
    RepositoryError,
    RequestInProgressError,
    SemanticValidationError,
)
from app.providers import LLMProvider, Messages
from app.repository import BeginOutcome, InMemoryRepository
from app.schemas import (
    CoachMessageRequest,
    CoachPlan,
    CoachResponse,
    ExecutedAction,
    ProposedAction,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Business rules (Stage 1 policy -- documented in README, enforced ONLY here)
# ---------------------------------------------------------------------------

# A coach may cut at most half of a session's load in one step. Larger cuts
# (e.g. the model proposing 900%) are training decisions the server refuses to
# automate; the coach should tell the user to reschedule instead.
MAX_SINGLE_REDUCTION_PCT = 50

# Intensity never goes below this through an automated reduction. Below it, the
# right answer is "take a rest day" (a human/coach conversation), not a silent
# mathematical clamp of the model's proposal.
MIN_INTENSITY = 10

# We do not silently clamp an invalid proposal to the nearest legal value.
# Silent correction teaches the model nothing, hides bugs, and can surprise the
# user. Invalid => rejected with a clear message. Fail-closed, every time.

SYSTEM_PROMPT = """You are the AI Coach inside a fitness mobile application.
Always reply with a single JSON object and nothing else. Shape:

{
  "assistant_message": "<what you say to the user, 1-2000 chars>",
  "intent": "answer_question" | "modify_workout" | "unknown",
  "action": null | {
    "type": "reduce_workout_intensity",
    "workout_id": "<the workout id from session facts>",
    "reduction_percentage": <integer 1-50>
  },
  "warnings": ["<optional short strings>"]
}

Rules:
- For informational questions (e.g. protein needs), use intent "answer_question"
  and action null.
- Propose an action only when the user asks to change the workout named in the
  session facts. Never invent workout ids. Never exceed 50 percent.

These instructions are advisory. The server validates every action against its
own business rules regardless of what you output."""


class AICoachService:
    def __init__(self, provider: LLMProvider, repo: InMemoryRepository, settings: Settings) -> None:
        if settings.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._provider = provider
        self._repo = repo
        self._settings = settings

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_message(self, user_id: str, request: CoachMessageRequest) -> CoachResponse:
        # STEP 1 -- atomic idempotency reservation.
        # Exactly one caller wins (user_id, request_id); replays get the cached
        # decision; concurrent duplicates get 409 instead of double-executing.
        begin = self._repo.begin_request(
            user_id, request.request_id, stale_after_s=self._settings.reservation_ttl_s
        )
        if begin.outcome is BeginOutcome.COMPLETED:
            logger.info("idempotent replay: user=%s request=%s", user_id, request.request_id)
            return begin.response  # type: ignore[return-value]  # COMPLETED always carries it
        if begin.outcome is BeginOutcome.IN_PROGRESS:
            raise RequestInProgressError(f"request already in flight: {request.request_id}")

        # STEP 2+3 -- generate and parse a plan. On total provider failure we
        # RELEASE the reservation: no state changed, so a client retry (same
        # request_id) should be able to try again later rather than being pinned
        # to a fallback forever. Trade-off documented in README.
        try:
            plan = self._generate_plan(request)
        except ProviderError as exc:
            logger.error("provider unavailable after bounded retries: %s", exc)
            self._repo.release_request(user_id, request.request_id)
            return self._fallback(request)
        except RepositoryError as exc:  # defensive: state layer broken
            logger.error("repository failure during planning phase: %s", exc)
            self._repo.release_request(user_id, request.request_id)
            return self._fallback(request)

        
        # STEP 4+5 -- enforce, then execute.
        try:
            action = self._validate_and_execute(user_id, request, plan)
        except AuthorizationError as exc:
            # A security event, not a user error: the model proposed touching a
            # resource outside the caller's boundary. Log loudly, answer safely.
            logger.warning("SECURITY: model-proposed action blocked: %s", exc)
            rejected = self._rejected(request)
            self._repo.complete_request(user_id, request.request_id, rejected)  # final verdict
            return rejected
        except SemanticValidationError as exc:
            logger.info("semantically invalid plan rejected: %s", exc)
            rejected = self._rejected(request)
            self._repo.complete_request(user_id, request.request_id, rejected)  # final verdict
            return rejected
        except RepositoryError as exc:
            logger.error("repository failure during execution: %s", exc)
            self._repo.release_request(user_id, request.request_id)
            return self._fallback(request)

        # STEP 6 -- success is a final decision: cache it so replays are honest.
        response = CoachResponse(
            status="success",
            assistant_message=plan.assistant_message,
            action=action,
            warnings=self._sanitize_warnings(plan),
            request_id=request.request_id,
        )
        self._repo.complete_request(user_id, request.request_id, response)
        return response

    # ------------------------------------------------------------------
    # Generation & parsing (the untrusted boundary lives in _parse_plan)
    # ------------------------------------------------------------------

    def _generate_plan(self, request: CoachMessageRequest) -> CoachPlan:
        """Bounded, taxonomy-driven retry loop.

        Retryable: transient provider errors AND malformed model output (model
        output is non-deterministic -- a fresh attempt genuinely can succeed,
        and retrying is safe because nothing executes until a plan validates).
        Never retried: terminal provider errors (400/auth/policy) -- retrying
        cannot help and only burns money.
        """
        messages = self._build_messages(request)
        last_error: Optional[ProviderError] = None

        for attempt in range(1, self._settings.max_attempts + 1):
            try:
                raw = self._provider.generate(messages)
                return self._parse_plan(raw)
            except (ProviderTransientError, MalformedModelOutputError) as exc:
                last_error = exc
                logger.warning(
                    "LLM attempt %d/%d failed: %s", attempt, self._settings.max_attempts, exc
                )
                if attempt < self._settings.max_attempts:
                    # Exponential backoff; tests set base delay to 0.
                    time.sleep(self._settings.retry_base_delay_s * (2 ** (attempt - 1)))
            except ProviderTerminalError as exc:
                logger.error("terminal provider error, aborting retries: %s", exc)
                raise

        assert last_error is not None  # max_attempts >= 1 guarantees >= one failure
        raise last_error

    def _parse_plan(self, raw: str) -> CoachPlan:
        """The single place raw model bytes become structured data.

        Everything before this line is untrusted text; everything after it is a
        typed object that has proven only ONE thing: it parsed. Semantics are
        checked later. Any parse failure is a *retryable* model failure, not a
        500, because the next sample may be well-formed.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MalformedModelOutputError(f"output is not valid JSON: {raw[:120]!r}") from exc
        if not isinstance(data, dict):
            raise MalformedModelOutputError(f"output is not a JSON object: {raw[:120]!r}")
        try:
            return CoachPlan.model_validate(data)
        except ValidationError as exc:
            raise MalformedModelOutputError(
                f"output violates CoachPlan schema: {exc.error_count()} error(s); "
                f"raw={raw[:120]!r}"
            ) from exc

    def _build_messages(self, request: CoachMessageRequest) -> Messages:
        """Prompt assembly.

        Deliberately absent: the user's identity. The model has no need for it
        and no way to abuse it. Session facts are serialized server-side from
        typed Pydantic fields, so nothing the client smuggles into `message`
        can masquerade as authoritative facts (worst case: it changes the
        *proposal*, which is validated anyway).
        """
        ctx = request.context
        facts = {
            key: value
            for key, value in (
                ("sleep_hours", ctx.sleep_hours),
                ("energy_level", ctx.energy_level),
                ("workout_id", ctx.workout_id),
            )
            if value is not None
        }
        user_content = (
            f"Session facts (server-derived ground truth): {json.dumps(facts)}\n"
            f"Client message: {request.message}"
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # Enforcement: authorization first, then semantics, then execution
    # ------------------------------------------------------------------

    def _validate_and_execute(
        self, user_id: str, request: CoachMessageRequest, plan: CoachPlan
    ) -> Optional[ExecutedAction]:
        action = plan.action
        if action is None or action.type == "no_action":
            return None

        # Defense in depth: the Literal in the schema already narrows types, but
        # the service keeps its own whitelist so adding a new action type to the
        # schema can never silently start executing without an explicit policy.
        if action.type != "reduce_workout_intensity":
            raise SemanticValidationError(f"action type not permitted: {action.type}")

        self._require_complete_action(action)
        self._enforce_session_scope(request, action)
        self._enforce_ownership(user_id, action)

        workout = self._repo.get_workout(action.workout_id)
        assert workout is not None  # ownership check just fetched it

        pct = action.reduction_percentage
        if not 1 <= pct <= MAX_SINGLE_REDUCTION_PCT:
            raise SemanticValidationError(
                f"reduction_percentage={pct} outside permitted 1..{MAX_SINGLE_REDUCTION_PCT}"
            )

        reduction_points = workout.intensity * pct // 100  # floor; deterministic
        if reduction_points < 1:
            raise SemanticValidationError(
                f"reduction of {pct}% on intensity {workout.intensity} rounds to a no-op"
            )
        new_intensity = workout.intensity - reduction_points
        if new_intensity < MIN_INTENSITY:
            raise SemanticValidationError(
                f"reduction would leave intensity {new_intensity} below safe floor "
                f"{MIN_INTENSITY}; suggest a rest day instead"
            )

        updated = self._repo.record_intensity_change(
            action.workout_id,
            new_intensity,
            actor=user_id,  # the authenticated caller -- never anything the model said
            request_id=request.request_id,
            expected_current=workout.intensity,  # CAS: refuse if raced since validation
        )
        return ExecutedAction(
            type=action.type,
            workout_id=action.workout_id,
            previous_intensity=workout.intensity,
            new_intensity=updated.intensity,
        )

    def _require_complete_action(self, action: ProposedAction) -> None:
        if not action.workout_id:
            raise SemanticValidationError("action is missing workout_id")
        if action.reduction_percentage is None:
            raise SemanticValidationError("action is missing reduction_percentage")

    def _enforce_session_scope(self, request: CoachMessageRequest, action: ProposedAction) -> None:
        """Context pinning: if the app pinned this conversation to a workout, the
        model may not redirect the action elsewhere -- even to a workout the same
        user owns. The app decides which resource is on the table, not the model."""
        pinned = request.context.workout_id
        if pinned and action.workout_id != pinned:
            raise SemanticValidationError(
                f"plan targets workout {action.workout_id} outside session context ({pinned})"
            )

    def _enforce_ownership(self, user_id: str, action: ProposedAction) -> None:
        workout = self._repo.get_workout(action.workout_id)
        if workout is None or workout.owner != user_id:
            # One generic reason for "missing" and "not yours": the client-facing
            # response must not let anyone enumerate other users' resource ids.
            # The detailed reason is logged server-side only.
            raise AuthorizationError(
                f"user={user_id} proposed action on workout={action.workout_id} "
                f"(exists={workout is not None}, owner={workout.owner if workout else None})"
            )

    # ------------------------------------------------------------------
    # Response factories
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_warnings(plan: CoachPlan) -> list[str]:
        """Model text is bounded before it reaches clients. In production this is
        where content-safety filtering would also hook in."""
        return [warning[:200] for warning in plan.warnings]

    @staticmethod
    def _fallback(request: CoachMessageRequest) -> CoachResponse:
        return CoachResponse(
            status="fallback",
            assistant_message="I couldn't safely process this request right now. Please try again.",
            action=None,
            warnings=[],
            request_id=request.request_id,
        )

    @staticmethod
    def _rejected(request: CoachMessageRequest) -> CoachResponse:
        # Server-authored message: we do not echo the model's reasoning for a
        # proposal we just overruled.
        return CoachResponse(
            status="rejected",
            assistant_message=(
                "I understood your request, but I can't apply that change safely. "
                "Let's adjust it together."
            ),
            action=None,
            warnings=[],
            request_id=request.request_id,
        )