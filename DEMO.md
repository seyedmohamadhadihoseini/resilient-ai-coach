# Live Demo Recipes

The default mock answers with a harmless default plan. Set `MOCK_PLAN_JSON` to
script any scenario live — including the brief's exact attack vectors.

> **Timing note:** after every `--force-recreate`, wait ~2s for uvicorn to start
> listening (or poll `/health`) before sending requests.

> **Windows PowerShell note:** PS 5.1 mangles JSON quotes passed to `curl.exe`.
> Save the body to a file and use `--data "@$env:TEMP\body.json"` (see the
> PowerShell variant at the bottom), or use `Invoke-RestMethod -Body`.
> Examples below use POSIX `curl` (macOS / Linux / Git Bash).

## Demo 1 — normal 20% reduction + live idempotency (scenarios A + D)

```bash
export MOCK_PLAN_JSON='{"assistant_message":"Reducing by 20% given your low sleep.","intent":"modify_workout","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":20},"warnings":[]}'
docker compose up -d --force-recreate   # then wait ~2s

curl -s -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" -H "X-User-Id: user_123" \
  -d '{"request_id":"req_demo_1","message":"make it easier","context":{"sleep_hours":4.5,"energy_level":3,"workout_id":"workout_123"}}'
# → status=success, action: previous_intensity=100, new_intensity=80

# Send the exact same command again → identical cached reply; intensity stays 80.
docker compose logs ai-coach | grep POST   # two lines: ... 200 OK
```

## Demo 2 — the brief's 900% attack (scenario B)

```bash
export MOCK_PLAN_JSON='{"assistant_message":"Nuking your session.","intent":"modify_workout","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":900},"warnings":[]}'
docker compose up -d --force-recreate   # then wait ~2s

curl -s -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" -H "X-User-Id: user_123" \
  -d '{"request_id":"req_demo_2","message":"destroy my workout","context":{"sleep_hours":4.5,"energy_level":3,"workout_id":"workout_123"}}'
# → status=rejected, action=null. Send again → identical cached verdict (rejections are final).
```

## Demo 3 — the model claims a different identity (the brief's exact example)

```bash
export MOCK_PLAN_JSON='{"assistant_message":"Sure.","intent":"modify_workout","user_id":"user_999","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":20},"warnings":[]}'
docker compose up -d --force-recreate   # then wait ~2s

curl -s -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" -H "X-User-Id: user_123" \
  -d '{"request_id":"req_demo_3","message":"do it","context":{"sleep_hours":4.5,"energy_level":3,"workout_id":"workout_123"}}'
# → status=success. The model's user_id claim was dropped at the parse boundary;
#   the action executed for the authenticated caller only.
```

## Demo 4 — fail-closed identity

Any POST without `X-User-Id` → **401**, even with a malformed body:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/ai/coach/message \
  -H "Content-Type: application/json" -d '{broken json'
# → 401
```

## Reset

```bash
unset MOCK_PLAN_JSON
docker compose up -d --force-recreate
```

## PowerShell variant (Demo 1, copy-paste-safe)

```powershell
$env:MOCK_PLAN_JSON = '{"assistant_message":"Reducing by 20%.","intent":"modify_workout","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":20},"warnings":[]}'
docker compose up -d --force-recreate

'{"request_id":"req_demo_1","message":"make it easier","context":{"sleep_hours":4.5,"energy_level":3,"workout_id":"workout_123"}}' |
  Out-File -Encoding ascii "$env:TEMP\demo1.json"
# wait for /health, then:
curl.exe -sS -X POST http://localhost:8000/ai/coach/message `
  -H "Content-Type: application/json" -H "X-User-Id: user_123" `
  --data "@$env:TEMP\demo1.json"

Remove-Item Env:MOCK_PLAN_JSON
```