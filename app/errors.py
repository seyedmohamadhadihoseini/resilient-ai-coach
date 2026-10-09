"""Exception hierarchy.

The taxonomy *is* the retry/fallback policy, so it is worth reading:

Provider layer (raised by LLMProvider implementations):
- ProviderTransientError        timeout / connection error / 429 / 5xx.
                                A bounded retry with backoff may succeed.
- CircuitBreakerOpenError       fast-fail when the provider is known to be failing;
                                avoids burning tokens or blocking workers.
- MalformedModelOutputError     HTTP-level success, but the raw completion could not
                                be parsed into the CoachPlan contract.
- ProviderTerminalError         bad request / auth / policy errors: retrying cannot
                                help and only burns money. Never retried.

Validation layer (raised by AICoachService):
- SemanticValidationError       structurally valid plan, business-invalid values.
- AuthorizationError            plan targets a resource outside the caller's
                                ownership. Logged as a security event. Never retried.

Infrastructure:
- RepositoryError               the local state layer failed.
- RequestInProgressError        same (user, request_id) is being processed right now;
                                mapped to HTTP 409 by the API layer.
"""


class AppError(Exception):
    """Base class for all domain errors."""


# ---- provider layer ----

class ProviderError(AppError):
    """Base class for LLM provider failures."""


class ProviderTransientError(ProviderError):
    """Temporary failure; a bounded retry with backoff may succeed."""


class CircuitBreakerOpenError(ProviderTransientError):
    """Circuit breaker is OPEN; fail-fast without hitting external provider."""


class ProviderTerminalError(ProviderError):
    """Permanent failure; retrying is pointless (and expensive)."""


class MalformedModelOutputError(ProviderError):
    """Raw completion could not be parsed into the structured contract."""


# ---- validation layer ----

class SemanticValidationError(AppError):
    """Plan is structurally valid but violates business constraints."""


class AuthorizationError(AppError):
    """Plan targets a resource outside the authenticated caller's ownership."""


# ---- infrastructure ----

class RepositoryError(AppError):
    """The state layer failed to fetch or persist data."""


class RequestInProgressError(AppError):
    """An identical logical request is currently in flight."""