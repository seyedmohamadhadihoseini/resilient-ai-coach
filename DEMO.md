Live Demo Recipes
The default mock answers with a harmless default plan. Set MOCK_PLAN_JSON toscript any scenario live -- including the brief's exact attack vectors.

Demo 1 — normal 20% reduction (scenario A, live):

 $env:MOCK_PLAN_JSON = '{"assistant_message":"Reducing by 20% given your low sleep.","intent":"modify_workout","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":20},"warnings":[]}'docker compose up -d --force-recreateInvoke-RestMethod -Uri http://localhost:8000/ai/coach/message -Method Post `  -ContentType "application/json" -Headers @{ "X-User-Id" = "user_123" } `  -Body '{"request_id":"req_demo_1","message":"make it easier","context":{"sleep_hours":4.5,"energy_level":3,"workout_id":"workout_123"}}'# → status=success, action: previous_intensity=100, new_intensity=80# Send the exact same command again → identical cached replay; intensity stays 80 (idempotency, live).
Demo 2 — the brief's 900% attack (scenario B, live):

 $env:MOCK_PLAN_JSON = '{"assistant_message":"Nuking your session.","intent":"modify_workout","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":900},"warnings":[]}'docker compose up -d --force-recreate# POST with a fresh request_id → status=rejected; send again → same cached verdict.

After every --force-recreate, wait ~2s for uvicorn to start listening(or poll /health) before sending requests.
Demo 3 — the model claims a different identity (the brief's exact example):

 $env:MOCK_PLAN_JSON = '{"assistant_message":"Sure.","intent":"modify_workout","user_id":"user_999","action":{"type":"reduce_workout_intensity","workout_id":"workout_123","reduction_percentage":20},"warnings":[]}'# POST as user_123 → status=success. The user_id claim was dropped at the parse# boundary; the action executed for the authenticated caller only.
Demo 4 — fail-closed identity: any POST without X-User-Id → 401, even with a malformed body.