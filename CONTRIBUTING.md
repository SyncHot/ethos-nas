# Contributing to EthOS NAS

Thanks for helping. This project is a FastAPI backend plus vanilla ES modules —
no build step, no bundler, no npm.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
./start_server.sh          # http://127.0.0.1:9001, isolated data dir
```

`start_server.sh` binds **:9001** and points `ETHOS_DATA_DIR` at
`/tmp/ethos-dev-data`, so it can never touch an installed instance on :9000.

Testing against a throwaway instance instead of the dev server:

```bash
ETHOS_PORT=9002 ETHOS_DATA_DIR=/tmp/ethos-test-data ETHOS_JWT_SECRET=dev-secret \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9002 &
```

## Tests

```bash
export ETHOS_TEST_PASSWORD='<password of your Linux account>'
export ETHOS_TEST_USER="$USER"
.venv/bin/python run_tests.py            # everything
.venv/bin/python tests/test_network.py   # one file
```

Rules:

- Never commit credentials — tests read `ETHOS_TEST_PASSWORD`.
- Every test that boots a server sets its own `ETHOS_DATA_DIR` and
  `ETHOS_JWT_SECRET`, and cleans up afterwards.
- One file per process; `run_tests.py` discovers `tests/test_*.py`.

Before opening a PR, run the same gate as CI:

```bash
.venv/bin/python -m compileall -q backend
.venv/bin/python -c "import backend.main"
.venv/bin/ruff check .
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/check_logging.py
.venv/bin/python scripts/check_core_deps.py
node --check frontend/static/app.js
```

## Code conventions

**Backend**

- New logic goes into `backend/services/`; routers stay thin. Services must not
  import routers (enforced by `scripts/check_boundaries.py`).
- Read environment variables only in `backend/config.py`.
- JSON state goes through `backend/services/json_store.py` (atomic writes,
  schema versions, migrations). No `json.dump` in routers.
- Every module gets a child logger `ethos-nas.routers.<name>` /
  `ethos-nas.services.<name>`; every failure path logs with `exc_info=True`.
- Subprocesses: argument lists only, never `shell=True`; wrap blocking calls in
  `asyncio.to_thread()`.

**Frontend**

- No new globals; import from `frontend/static/core/*`.
- Escape untrusted text with `escapeHtml` / `escapeAttr` from `core/utils.js`.
- Progress uses `core/progress.js`; toasts/notifications use
  `core/notification-center.js` (never a local panel).
- User-facing strings are English; dates/bytes use `formatDate`/`formatBytes`.

**Security invariants** (see `SECURITY.md`)

- Every `/api/*` route requires a JWT unless it is in the exact `public_paths`
  set in `backend/main.py`.
- Privileged routers use `require_admin`; "logged in" is not "admin".
- Filesystem endpoints mount `path_scope.scope_request` and pass paths through
  `path_scope.sanitize()`.

## Pull requests

- Keep changes focused; no drive-by refactors.
- Describe the user-visible effect and how you tested it.
- CI must be green (`ci` workflow runs the fast suite).

## License

By contributing you agree that your contribution is licensed under the MIT
License (see `LICENSE`).
