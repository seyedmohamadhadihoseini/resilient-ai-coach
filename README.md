# AI Coach Service

A standalone FastAPI backend that lets a fitness app's users send natural-language
messages to an AI coach and get back a **structured, validated, safe** response —
including authorized, audited, idempotent execution of state-changing actions.

Core design principle, enforced structurally rather than by convention:

> **The model proposes. The server disposes.**

Model output is *data*. It is never identity, never authorization, and never
trusted application state.

## Quick Start

```bash
# 1. Run the test suite (40 tests, no network or credentials needed)
pip install -r requirements.txt
pytest -v

# 2. Run the service
docker compose up --build
# API:  http://localhost:8000/ai/coach/message
# Docs: http://localhost:8000/docs

# 3. Talk to it (upstream auth middleware is simulated by the X-User-Id header)
curl -s -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user_123" \
  -d '{
        "request_id": "req_abc123",
        "message": "I am exhausted today. Can you make my workout easier?",
        "context": {"sleep_hours": 4.5, "energy_level": 3, "workout_id": "workout_123"}
      }'
```

- Port 8000 busy? `HOST_PORT=8001 docker compose up -d` (bash) or
  `$env:HOST_PORT="8001"; docker compose up -d` (PowerShell).
- Windows PowerShell users: `curl` there is an alias for `Invoke-WebRequest`; use
  `curl.exe`, and prefer the copy-paste-safe recipes in **DEMO.md**.

## Architecture

```text
Mobile client
   │  POST /ai/coach/message      X-User-Id header (injected by upstream auth middleware)
   ▼
┌─────────────────────────── FastAPI (app/main.py) ───────────────────────────┐
│  identity_gate middleware: fail-closed 401 BEFORE routing / body parsing    │
│  └▶ AICoachService (app/service.py)                                         │
│       1. idempotency ledger   → atomic reserve / replay / 409-in-flight     │
│       2. LLMProvider.generate() → returns RAW text, nothing more            │
│       3. parse → CoachPlan     → the single untrusted-input boundary        │
│       4. semantic + authz      → bounds, ownership, session scope           │
│       5. repository write      → compare-and-swap + audit trail             │
│       6. cache final verdict   → replays never re-consult the model         │
└──────────────────────────────────────────────────────────────────────────────┘
        │                                    │
   app/providers.py                     app/repository.py
   LLMProvider (ABC)                    InMemoryRepository
   ├─ MockLLMProvider (default)         ├─ workouts (+ defensive copies)
   └─ OpenAICompatibleProvider          ├─ idempotency ledger (atomic)
      (real API, error taxonomy)        └─ audit log
```

Business code depends only on the `LLMProvider` interface — never on a vendor SDK.
Mock and real providers cross the **exact same trust boundary**: both return raw
text; both are parsed and validated in one place.

See **DEMO.md** for 2-minute live demos of every security scenario (idempotency,
900% rejection, identity-claim drop, fail-closed auth).

## Three Trust Boundaries, Three Contracts (app/schemas.py)

1. **Client → Backend** (`CoachMessageRequest`): bounded `request_id`, bounded
   `message` (max 2000 chars — cost/log protection), context fields with ranges.
2. **LLM → Backend** (`CoachPlan`, UNTRUSTED): structural contract only.
   `strict=True` so `"20"` (string) is refused rather than coerced to `20`.
   Deliberately contains **no identity field**: the model cannot claim "act as
   user X" because the type system has no place to put it. Structural
   impossibility beats runtime rejection — see `test_c3` for the brief's exact
   attack (`user_id: user_999` in model output) being dropped at parse time.
3. **Backend → Client** (`CoachResponse`): server-authored facts only.
   `action.previous_intensity / new_intensity` are what *actually* happened,
   verified server-side — never the model's claims. The rejection/fallback
   wording is written by the server; we never echo the reasoning of a proposal
   we just overruled.

## Structured Output Strategy

- Provider returns **raw text**; parsing happens in exactly one place
  (`AICoachService._parse_plan`). JSON mode (`response_format=json_object`) is
  requested from the real provider to make valid JSON *likely* — but it is
  hygiene, never security: output is still parsed defensively downstream.
- Parse/schema failures raise `MalformedModelOutputError`, treated as
  **retryable** (bounded): model output is non-deterministic, a re-sample can
  genuinely succeed, and retrying is safe because nothing executes until a plan
  validates.
- Tests prove it: garbage JSON (`test_f1/f2`), wrong JSON shape, and type
  coercion attempts (`test_f3`) all end safely.

## Semantic / Business Validation

Pydantic proves *shape*; the service decides *sense*. Rules enforced after parse:

| Rule | Value | Rationale |
|---|---|---|
| Max single reduction | 50% | A coach may cut at most half a session's load in one step; bigger cuts (e.g. the brief's `900%`) are rescheduling conversations, not automated math. |
| Safe intensity floor | 10 | Automated reduction never drops below it; the right answer there is "take a rest day", not silent clamping. |
| No-op reduction | rejected | A "success" that changed nothing is a lie (`3% of 15` floors to 0 points → rejected, `test_b4`). |
| **No clamping, ever** | — | An invalid proposal is *rejected*, not corrected to the nearest legal value. Silent correction hides bugs and surprises users. Fail-closed. |

All values live in one place (`app/service.py` constants) with the reasoning
above; changing policy is a one-line, well-lit change.

## Authorization Boundary

- Identity is enforced by an HTTP **middleware** (`identity_gate`) — the same
  layer real upstream auth occupies — and is fail-closed: no header → 401
  *before* body parsing. Even a malformed body with no header gets 401, never
  422 or a default user (`test_g2`).
- Public surface is an explicit allowlist (`/health` for orchestrator liveness
  probes, which send no credentials, plus API docs). All other routes require
  identity by default — new routes are protected unless explicitly made public.
- The model never sees, receives, or influences identity. Identity never comes
  from the request body or model output — only from the server-derived header.
- Three independent gates before any write:
  1. **Action whitelist** — service-level allowlist on top of the schema Literal.
  2. **Session scope** — if the app pinned the conversation to `workout_123`,
     the model cannot redirect to another workout *even one the same user owns*
     (`test_c2`). The app decides which resource is on the table.
  3. **Ownership** — resource owner must equal the authenticated caller
     (`test_c`). "Not found" and "not yours" are indistinguishable to clients
     (no resource-id enumeration); the detailed reason is logged server-side.
- Every executed write is audited with the **server-derived** actor
  (`audit_log[0]["actor"] == "user_123"`, asserted in tests).

## Idempotency Strategy

Key = `(user_id, request_id)` — **never `request_id` alone**: request ids are
client-generated, so keying on them alone would leak one user's cached response
to another on collision (`test_d2` proves isolation).

The ledger is a state machine per key, mutated only under a lock:

- `begin_request()` — atomically returns `ACQUIRED` (exactly one concurrent
  caller may process), `IN_PROGRESS` (duplicate in flight → mapped to HTTP 409
  so the client retries shortly instead of double-firing), or `COMPLETED`
  (replay → cached response, provider not re-consulted). This removes the
  classic check-then-act race (`test_repository.py` race test: 16 threads →
  exactly 1 winner).
- `complete_request()` — publishes the final verdict (success **or** rejection —
  a rejection *is* an answer; replays must not re-roll the model dice).
- `release_request()` — used only when *nothing* was executed (provider failure):
  the client may retry the same `request_id` later instead of being pinned to a
  fallback forever (`test_e2`).
- Stale in-flight reservations (`reservation_ttl_s`, default 120s) can be
  stolen, so a crashed handler cannot hold a key forever.

**Limitations (Stage 1, by design):** single-process, in-memory, lost on
restart. **In production:** Redis `SET key value NX EX ttl` for the atomic
reservation + a stored-response record; plus a DB unique constraint on
`(user_id, request_id)` so the ledger survives process crashes, with the action
and ledger entry committed in one transaction.

## Provider Error Handling

The exception taxonomy **is** the retry policy (see `app/errors.py` docstring):

| Symptom | Class | Policy |
|---|---|---|
| timeout / connection error | `ProviderTransientError` | bounded retry + exponential backoff |
| 429 rate limit (retry-after surfaced in logs) | `ProviderTransientError` | bounded retry |
| 5xx | `ProviderTransientError` | bounded retry |
| malformed model output | `MalformedModelOutputError` | bounded retry (nothing executed yet → safe to re-sample) |
| other 4xx / auth / policy | `ProviderTerminalError` | **never retried** — cannot help, costs money |

All retries are bounded (`LLM_MAX_ATTEMPTS`, default 3). Real-provider behaviour
is proven without network access via `httpx.MockTransport`
(`tests/test_providers.py`).

## Safe Fallback Behavior

On total provider failure the service returns a typed fallback
(`status="fallback"`, `action=null`), never crashes, never executes anything,
and **releases** the idempotency reservation so a later retry with the same
`request_id` can succeed once the provider recovers (`test_e2`). Trade-off
discussed: caching the fallback would permanently pin a user to "I couldn't
process this" after a transient outage; releasing means a duplicate retry during
an outage calls the provider again — acceptable for an idempotent,
read-then-maybe-write coach flow. In production I would cache fallbacks with a
short TTL (e.g. 30s) as a middle ground.

## Running With a Real Provider

```bash
LLM_PROVIDER=openai OPENAI_API_KEY=sk-... OPENAI_MODEL=gpt-4o-mini \
  uvicorn app.main:app --port 8000
```

No credentials are shipped with this evaluation. The default is the mock
provider; `OpenAICompatibleProvider` is the reference real integration and is
exercised end-to-end in tests via a scripted transport.

## Test Map (tests/test_scenarios.py unless noted)

| Brief requirement | Test(s) |
|---|---|
| A. Normal request | `test_a_*` |
| B. Semantic failure (900%) | `test_b_*` (+ no-op, floor, caching of verdicts) |
| C. Authorization failure | `test_c_*` (cross-user, session-scope, `user_id` claim dropped) |
| D. Duplicate request | `test_d_*` (single execution, cross-user isolation) |
| E. Provider failure | `test_e_*` (bounded retries, retry-after-fallback, terminal no-retry) |
| Malformed structured output | `test_f_*` |
| API hardening (fail-closed 401, 422 bounds) | `test_g_*` |
| Real provider taxonomy + full stack | `tests/test_providers.py` |
| Ledger concurrency (16-thread race, CAS) | `tests/test_repository.py` |

Every security claim in this README has a test that fails if the claim breaks.

## Assumptions

- Upstream middleware owns real authentication and injects `X-User-Id`.
- Mock workouts model a 0–100 "session load" scalar; business bounds above are
  product policy placeholders, deliberately centralized and documented.
- Conversational memory is intentionally out of scope (per brief); each request
  carries its own session facts.
- Stage 1 persistence is in-memory by design; the interface it exposes
  (begin/complete/release, CAS write) is the shape a Redis/Postgres
  implementation would drop into.

## Known Limitations

- In-memory state: single-process only; no durability across restarts.
- Backoff is sleep-based without jitter; a thundering herd of retries against
  one provider would want jitter + a circuit breaker.
- `assistant_message` from the model is passed through bounded but unmoderated;
  production would add content-safety filtering (`_sanitize_warnings` marks the
  hook).

## What I Would Change for Production

1. **Redis-backed idempotency** (`SET NX EX` + stored responses) and a DB unique
   constraint; action + ledger committed transactionally.
2. **PostgreSQL** with row-level locking / `UPDATE ... WHERE intensity = expected`
   to make CAS durable.
3. **Tenacity** retries with exponential backoff + jitter, a circuit breaker, and
   per-user rate limiting in front of the endpoint.
4. **Observability**: structured logs, token/latency metrics, alerting on
   rejection and fallback rates (a spike = model drift or an attack).
5. Prompt-injection hardening for user free-text (delimiting, canary tokens),
   since it is concatenated into the user turn today.