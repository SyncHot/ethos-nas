# AGENTS.md — EthOS NAS

Self-hosted NAS UI: FastAPI (Python 3.14, `.venv`) + vanilla ES modules, no build step. One process
serves the API (`/api/*`), WebSockets and the SPA.

## Commands (always use `.venv`)

| What | Command |
|---|---|
| Setup | `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` |
| Dev server | `./start_server.sh` — binds **:9001**, `ETHOS_DATA_DIR=/tmp/ethos-dev-data`, never production :9000 |
| Daemon | systemd `ethos-nas` — **no auto-reload**. Restart only via UI: Settings → Restart (`POST /api/system/restart`). Fallback: ask the user to run `sudo systemctl restart ethos-nas`. Logs: `journalctl -u ethos-nas -f` |
| Isolated instance | `ETHOS_PORT=9002 ETHOS_DATA_DIR=/tmp/ethos-test-data nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9002 &` — **always** set `ETHOS_DATA_DIR` |
| Tests | `.venv/bin/python run_tests.py` — needs `ETHOS_TEST_PASSWORD` |
| Health | `curl -sf http://localhost:9000/api/system/status` (HTTP 401 = up) |
| Swagger | http://localhost:9000/docs |

## Architecture

- `backend/main.py` — app init, JWT middleware, router mounting, SPA. Public paths are an **exact-path
  set**; never add a prefix (see `TODO.md` P0-02).
- `backend/config.py` — `settings` singleton. Read env vars **only here**.
- `backend/routers/` — one file per domain; most logic lives here today. Put **new** logic in
  `backend/services/` (services must not import routers — `scripts/check_boundaries.py`).
- State: JSON under `settings.DATA_DIR` via `services/json_store.py`.
- Frontend: `index.html` (Tailwind CDN + CSS variables) · `static/app.js` (bootstrap/wiring) ·
  `static/apps/<app>.js` (app code) · `static/core/` (`window-manager`, `api`, `ws`, `registry`,
  `upload-engine`, `modal`, `notification-center`, `utils`). New app code never goes in `app.js`.
- Auth: PAM → JWT → `Authorization: Bearer` (also cookie, `?token=` for media and WebSockets).
- API changes follow `docs/api-conventions.md`.
- Docker compose projects must be named `ethos-<name>` so labels in `.env` match.

## Hard rules

**AuthZ.** Every `/api/*` requires a JWT except the exact `public_paths` set. `user` ≠ admin:
privileged routers use `require_admin` / `require_role` (`routers/auth.py`).

**Paths.** Filesystem endpoints mount `path_scope.scope_request` and pass every path through
`path_scope.sanitize()`; non-admins are limited to `/home`, `/mnt`, `/media`. `scope_request` must stay
`async def`. File ops run as the `ethos-nas` service user (see `docs/service-user.md`) — surface the
403 `detail`, not a bare status. Subprocess: argument lists only, never `shell=True`. docker-py is
synchronous → always `asyncio.to_thread()`.

**Logging.** Every module gets a child logger: `logging.getLogger("ethos-nas.routers.<name>")` or
`"ethos-nas.services.<name>"`. Every failure path logs (`logger.error(..., exc_info=True)`); never
`except Exception: pass` without a comment. INFO+ records land in `<DATA_DIR>/logs/events.jsonl`
(ring 5000, rotated 5 × 1 MiB) through `services/log_service.py` — no second log store or file
(spec: `docs/logs.md`; rule: `.github/instructions/logging.instructions.md`; guard:
`tests/test_logging_ui.py`). Verify with `GET /api/logs?limit=20` (admin; the legacy
`/api/logs/app` alias still works). Frontend apps report through `core/logger.js` and read through
`core/log-stream.js`; native packages use `ctx.log` / `ctx.logger`. Never log secrets, tokens or
request bodies with credentials.

**Stores.** Use `json_store.get(path, default, version=N, migrate=...)` and `.load()` / `.save()` /
`.update()`. No `json.dump(` in routers, no plain `write_text` for state, no unbounded lists.

**Core vs packages.** Heavy, feature-only dependencies never enter `requirements.txt` and are never
imported by `backend/**` — a package owns them in its private venv and the core talks to it over
loopback (`runtime: "venv"`, spec `docs/packages.md`, boundary `docs/app-store.md`). Shell out to
packages, never `import` them. Guard: `scripts/check_core_deps.py`.

**Host dependencies.** Only `system_requires` (registry `services/system_deps.py`) may install OS
software, and only through the root-owned helpers (`ethos-docker-install`, `ethos-system-deps`) with
their exact sudoers rules. Install records ownership; uninstall removes a requirement only when no
installed package needs it and EthOS installed it. Never add a broad `apt` sudoers rule, never run
`apt` from a package, never auto-remove `docker`. Verify with `tests/test_system_deps*.py`.

**Notifications.** One global system (spec: `docs/notifications.md`; rules:
`.github/instructions/notifications.instructions.md`). Backend publishes with
`emit_event(kind, title, message, source=..., action=..., dedupe_key=...)`; the frontend calls
`notify({kind, title, message, action})` (legacy `addNotification(msg, type)` still works).
`core/notification-center.js` owns the toasts, the right-edge sheet and the bell badge —
**apps and packages must not build their own toast, panel, badge or event history**
(`tests/test_notification_ui.py`). Native packages use `ctx.notifications.{toast,emit,open}`.

**Progress.** Long-running actions (uploads, copy/move, delete/trash, package/engine installs) report
through `core/progress.js` — `startTask` → `update`/`finish`. No bespoke progress cards or panels in
apps; finished rows persist until cleared. See `.github/instructions/frontend.instructions.md` and
`docs/progress-ui.md`.

## Skills — load before working

| Task | Skill |
|---|---|
| New/changed endpoint or `/api` domain | `add-api-endpoint` |
| WebSocket (`/ws/*`, codes, reconnect) | `ws-endpoint` |
| JSON persistence, schema, migration | `state-store` |
| Desktop app / window / icons / XSS | `frontend-app` |
| Notifications (toast, sheet, package contract) | `notification-ui` |
| Logs (source, viewer, `ctx.log`, retention) | `logging-ui` |
| Packages package, manifest, compose, signing | `add-package` |
| Tests (API, security, Playwright) | `add-test` |
| Pre-commit verification | `verify-changes` |
| TODO.md item | `todo-item` |
| New feature plan doc | `feature-doc` |
| Repro / debug without touching prod | `isolated-dev-instance` |
| Android APK release | `release-android-apk` |
| Backend security audit | `security-invariant-review` |

## Workflow

- **Server restarts:** never restart the service yourself — ask the user (UI or terminal).
- **Git:** after each finished task, commit and push: `feat(<scope>): …` / `fix(<scope>): …`.
- **Backlog:** `TODO.md` is the only backlog. Read only the section you need; full text of completed
  items is in `docs/archive/todo-done.md`.
- **Features:** create `docs/<feature>.md` with plan + file map + status checkboxes, update as work lands.
- **Android:** any change in `ethos-moments` / `ethos-music` / `ethos-video` requires bumping
  `versionCode`, running `./build.sh`, verifying `/api/app/version?app=<app>` and mirroring the feature
  on the web — use `release-android-apk`.
- **Parity:** every user-facing Android feature must exist in the matching `frontend/static/apps/*.js`;
  report the parity check.
- **Done:** `verify-changes` green, server up on :9000 (401 health check), report changed files.
- **Scope:** small focused changes; no unrelated refactors; ask when ambiguous.
- **Autonomy:** don't ask the user to run commands you can run yourself; ask only when ambiguous, risky,
  or inherently manual (sudo, UI restart).
