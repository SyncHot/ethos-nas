---
name: isolated-dev-instance
description: "Boot a throwaway instance on :9002 for debug, curl probes, or tests. Use to reproduce bugs without touching production backend/data on :9000."
argument-hint: "What do you need to reproduce or probe?"
---

# Run an isolated instance

## Why
A second instance sharing `backend/data/` will overwrite production stores. **Always** set `ETHOS_DATA_DIR` and a non-9000 `ETHOS_PORT`. (`./start_server.sh` is the interactive :9001 dev server; use this skill for scripted/parallel instances.)

## Procedure

### 1. Start
```bash
rm -rf /tmp/ethos-test-data
ETHOS_PORT=9002 \
ETHOS_DATA_DIR=/tmp/ethos-test-data \
ETHOS_JWT_SECRET=dev-only-secret \
nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9002 \
  > /tmp/ethos-nas-test.log 2>&1 & disown
```

### 2. Wait for readiness
```bash
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9002/api/system/status)
  [ "$code" = "401" ] && echo "up" && break
  sleep 0.5
done
```
`401` means the server is up and auth is required. `000` means not listening — read the log.

### 3. Log in and capture the token
```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:9002/api/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$ETHOS_TEST_PASSWORD\"}" \
  | .venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["token"])')
```
Credentials come from the environment (`ETHOS_TEST_PASSWORD`); never hardcode them.

### 4. Call the API
```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9002/api/<domain>/...
```

### 5. Observe
- Server log: `tail -f /tmp/ethos-nas-test.log`
- All failures: `/tmp/ethos-test-data/app_events.jsonl`
- Stores: `/tmp/ethos-test-data/*.json` (safe to inspect/delete)
- Swagger: http://127.0.0.1:9002/docs

### 6. Point tests at it
```bash
ETHOS_TEST_BASE=http://127.0.0.1:9002 ETHOS_TEST_PASSWORD=... \
  .venv/bin/python tests/test_<area>.py
```
`run_tests.py` boots its own instance; use it for the whole suite:
```bash
ETHOS_TEST_PASSWORD=... .venv/bin/python run_tests.py --filter <area>
```

### 7. Tear down
```bash
pkill -f "uvicorn backend.main:app.*--port 9002" || true
rm -rf /tmp/ethos-test-data
```

## Hard rules
- Never start a second instance without `ETHOS_DATA_DIR`.
- Never run destructive probes (`/api/files/delete`, `/api/disk-clean`, USB format) against :9000.
- Do not restart the production service for debugging — use this instance.
- Do not commit `/tmp` paths or test credentials.

## Done when
The probe/test ran against `127.0.0.1:9002` with a throwaway data dir, and the process is stopped.
