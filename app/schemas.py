"""Pydantic contracts -- one section per trust boundary.

1. Request models   transport contract for the HTTP API (still validated server-side).
2. LLM plan models  the shape we ASK the model to produce. Treated as UNTRUSTED:
                    reaching these types proves only that raw bytes parsed into the
                    expected types. Semantics are enforced separately in the service.
3. Response models  the server's own trusted contract for clients.

Note what the plan schema deliberately does NOT contain: any user identity field.
The model cannot say "act as user X" because the type system has no place to put
it -- structural impossibility beats runtime rejection.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# 1. Client -> Backend
# --------------------------------------------------------------------------

class RequestContext(BaseModel):
    """Server-side session facts supplied by the app (never by the LLM)."""

    sleep_hours: Optional[float] = Field(default=None, ge=0, le=24)
    energy_level: Optional[int] = Field(default=None, ge=1, le=5)
    workout_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class CoachMessageRequest(BaseModel):
    request_id: str = Field(
        min_length=6,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-.]+$",
        description="Client-generated idempotency key; retries of the same "
                    "logical request reuse it.",
    )
    message: str = Field(min_length=1, max_length=2000)
    context: RequestContext = Field(default_factory=RequestContext)


# --------------------------------------------------------------------------
# 2. LLM -> Backend  (UNTRUSTED)
# --------------------------------------------------------------------------

class ProposedAction(BaseModel):
    """Structural contract only.

    `reduction_percentage` is deliberately unbounded here: proving the model sent
    an integer is a PARSING concern; deciding whether the value makes business
    sense is a SEMANTIC concern enforced in the service layer. Keeping those two
    apart is the whole point of having two validation layers.

    strict=True: at an untrusted boundary we want the type the model actually
    sent, not a coercion guess ("20" stays invalid instead of becoming 20).
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    type: Literal["reduce_workout_intensity", "no_action"]
    workout_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    reduction_percentage: Optional[int] = Field(default=None)


class CoachPlan(BaseModel):
    """What the model is asked for. Reaching this type proves the JSON parsed.

    Text fields are bounded so a runaway model cannot blow up memory or logs.
    extra="ignore": harmless extra keys from the model are dropped, not fatal.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    assistant_message: str = Field(min_length=1, max_length=2000)
    intent: Literal["answer_question", "modify_workout", "unknown"] = "unknown"
    action: Optional[ProposedAction] = None
    warnings: List[str] = Field(default_factory=list, max_length=5)


# --------------------------------------------------------------------------
# 3. Backend -> Client  (trusted, server-authored)
# --------------------------------------------------------------------------

class ExecutedAction(BaseModel):
    """Server-verified record of what ACTUALLY changed. These are facts."""

    type: str
    workout_id: str
    previous_intensity: int
    new_intensity: int


class CoachResponse(BaseModel):
    status: Literal["success", "rejected", "fallback"]
    assistant_message: str
    action: Optional[ExecutedAction] = None
    warnings: List[str] = Field(default_factory=list)
    request_id: str