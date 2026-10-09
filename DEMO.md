# Resilience & Defensive AI Demo Recipes

This repository implements a **durable, crash-resilient AI integration** designed to safely execute state-changing actions even under catastrophic process termination, lost network acknowledgements, and prompt injection attacks.

---

## 1. Automated Visual Chaos Demo (Recommended)

Run the end-to-end resilience suite in a single command:

```bash
python demo_resilience.py
```

This interactive demo deterministically walks through:
1. **Atomic Commit & Idempotency:** Side-effects and ledger updates committed in an indivisible SQLite WAL transaction.
2. **The Lost Acknowledgement Window:** Simulates a hard process crash immediately after the database commit, followed by a client retry on a fresh worker process (proving zero double-executions).
3. **Canary Leak Defense:** Proves that model attempts to leak system instructions trigger immediate fail-closed rejections.
4. **Event Sourcing Audit:** Replays immutable domain event logs to reconstruct state consistency from scratch.

---

## 2. Running Automated Tests

Run the full defensive test suite (56 automated tests):

```bash
pytest -v
```

All 56 tests execute in seconds without external network dependencies or API keys.

---

## 3. Docker Production Multi-Worker Run

Run the service inside containerized multi-worker Uvicorn (4 parallel workers):

```bash
docker compose up --build
```

Test endpoint live:
```bash
curl -s -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user_123" \
  -d '{
        "request_id": "req_demo_001",
        "message": "I am exhausted today. Can you reduce today'\''s session?",
        "context": {"sleep_hours": 4.5, "energy_level": 3, "workout_id": "workout_123"}
      }'
```