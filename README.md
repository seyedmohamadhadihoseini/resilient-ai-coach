# Resilient AI Coach Service: Defensive Trust Boundaries & Durable Idempotency

A production-grade, crash-resilient FastAPI backend service for fitness applications that bridges natural language model outputs with authoritative, state-changing business actions.

> **Core Architectural Axiom:**  
> *The model proposes. The server validates, authorizes, and disposes.*  
> Model output is untrusted data. It is never identity, never authorization, and never trusted application state.

---

## 1. Architectural Highlights & Solutions to the 3 Failure Windows

This service explicitly solves the fundamental distributed systems challenge:  
**Delivery cannot be exactly-once over unreliable networks, but state effects MUST be.**

```text
       [Client Request]
              │
              ▼
    ┌──────────────────┐
    │  1. PENDING      │ ◄─── Heartbeat & Lease TTL (Prevents zombie locks)
    └─────────┬────────┘
              │  (Validate Semantics + CAS Check)
              ▼
    ┌──────────────────┐
    │  2. COMMITTED    │ ◄─── ATOMIC TRANSACTION: State change + Audit + Ledger in 1 TX
    └─────────┬────────┘
              │  ◄─── CRASH WINDOW: If network drops or worker dies HERE,
              ▼                     the client retry receives the cached response.
    ┌──────────────────┐            Zero double-reductions.
    │ 3. ACKNOWLEDGED  │
    └──────────────────┘
```

### Key Engineering Guarantees:
1. **Single-Transaction Atomic Commits:** Workout intensity mutations, audit logging, and idempotency ledger records are committed in an indivisible SQLite transaction (`BEGIN IMMEDIATE`). A process crash at any point results in either complete persistence or a clean rollback.
2. **Lost Acknowledgement Recovery:** If a state mutation commits but the socket drops before the client receives the response, retries with the same `request_id` return the authoritative cached response without re-consulting the LLM or touching application state.
3. **Multi-Worker Process Safety:** Ready for multi-process environments (`uvicorn --workers 4`). Contention across separate processes is handled via SQLite WAL locking and exponential backoff with **Full Jitter**.
4. **Circuit Breaker & Bounded Retries:** Outages or transient 5xx/429 errors from LLM providers trip a circuit breaker to prevent worker starvation and token waste.
5. **Prompt Quarantine & Canary Tokens:** User input is quarantined within boundary tags. System prompts carry dynamic, high-entropy canary tokens (`CANARY_...`) that trigger instant fail-closed rejection if leaked by the model.
6. **Immutable Event Sourcing:** Mutations generate append-only domain events (`workout_events`), allowing complete reconstruction of state through event replay.

---

## 2. Quick Start

### Run the Automated Test Suite (56 Tests)
```bash
pip install -r requirements.txt
pytest -v
```

### Run the Interactive Resilience & Chaos Demo
```bash
python demo_resilience.py
```

### Run the Service in Docker (Multi-Worker)
```bash
docker compose up --build
```

---

## 3. Architecture & Trust Boundaries

```text
Mobile Client / Upstream Gateway
       │  POST /ai/coach/message (Header: X-User-Id)
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ FastAPI Gateway (app/main.py)                                          │
│  • identity_gate middleware: Fail-closed 401 before body parsing       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ AICoachService (app/service.py)                                        │
│  1. begin_request() ──────► Reserve (user_id, request_id) with lease   │
│  2. Prompt Quarantine ────► Isolate user text, inject Canary Token     │
│  3. Provider Call ────────► Circuit Breaker + Full Jitter Backoff      │
│  4. Structural Parse ─────► CoachPlan Pydantic model (strict types)    │
│  5. Semantic Validation ──► Whitelist, Session Scope, Ownership, Bounds │
│  6. Atomic Commit ────────► State write + Audit + Ledger in 1 TX       │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   app/circuit_breaker.py                       app/repository.py
   CircuitBreaker State Machine                 SQLiteRepository (WAL Mode)
   (CLOSED / OPEN / HALF-OPEN)                  • workouts
                                                • idempotency_ledger
                                                • audit_log
                                                • workout_events (Event Sourcing)
```

---

## 4. Test Suite Map (56 Automated Tests)

| Category | Test File | Scenarios Covered |
|---|---|---|
| **Core Scenarios** | `tests/test_scenarios.py` | Mandatory Brief items (Normal 20%, 900% rejection, ownership block, duplicate calls, safe fallbacks, identity drops). |
| **Atomic Commits** | `tests/test_atomic_commit.py` | Proves state change + ledger commit together; CAS concurrency rollbacks. |
| **Lost Acknowledgements** | `tests/test_lost_ack.py` | Dropped connection recovery, Heartbeat lease extension, cold server reboot recovery. |
| **Crash Injection & Chaos** | `tests/test_chaos.py` | `ChaosHook` simulating hard crashes right after DB write; clean recovery on retry. |
| **Multi-Worker Concurrency**| `tests/test_concurrency.py` | 16-thread/process races against SQLite WAL lock with zero corruption. |
| **Circuit Breaker** | `tests/test_circuit_breaker.py` | Threshold tripping, fast-failing during outages, Half-Open canary recovery. |
| **Prompt Security** | `tests/test_prompt_security.py` | Boundary tag escaping, Canary token leak detection. |
| **Event Sourcing** | `tests/test_events.py` | Append-only event logs, full state reconstruction via event replay. |
| **Provider Taxonomy** | `tests/test_providers.py` | HTTP error taxonomy mapping (429, 5xx, timeouts) via MockTransport. |

---

## 5. Production Differences & Design Decisions

- **Why SQLite WAL over In-Memory dict?**  
  Eliminates the multi-worker memory boundary and gives true crash durability without forcing reviewers to provision external PostgreSQL or Redis infrastructure.
- **Why No Clamping?**  
  When an LLM suggests an illegal action (e.g. 900% cut), clamping it to 50% hides logic bugs and misleads users. The system fails closed and explains the adjustment collaboratively.
- **Why Key Idempotency on `(user_id, request_id)`?**  
  `request_id` is client-generated. Keying solely on it allows malicious or accidental cross-user collision. Keying on both enforces strict tenant isolation.