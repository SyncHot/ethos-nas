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
  `upload-engine`, `modal`, `notification-center`, `package-ribbon`, `utils`) ·
  `static/{notes,videos,music}/` are the standalone PWA modules (`/notes`, `/videos`, `/music`) ·
  `static/css/{tokens,ribbon-host}.css` hold shared tokens and the right-edge ribbon layer. New app
  code never goes in `app.js`.
- Auth: PAM → JWT → `Authorization: Bearer` (also cookie, `?token=` for media and WebSockets).
- API changes follow `docs/api-conventions.md`.
- Docker compose projects must be named `ethos-<name>` so labels in `.env` match.

## Hard rules

**AuthZ.** Every `/api/*` requires a JWT except the exact `public_paths` set. `user` ≠ admin:
privileged routers use `require_admin` / `require_role` (`routers/auth.py`).

**Paths.** Filesystem endpoints mount `path_scope.scope_request` and pass every path through
`path_scope.sanitize()`; admins keep full access, every other account is limited to its **own home**
plus `/mnt` and `/media`, and `<DATA_DIR>` is never user-facing. `scope_request` must stay
`async def`. Background workers use `path_scope.system_scope()` (or `sanitize_for`). File ops run as
the `ethos-nas` service user (see `docs/service-user.md`) — surface the
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
`emit_event(kind, title, message, source=..., action=..., dedupe_key=...)`; pass `users=[…]` to
notify named accounts, omit it for a broadcast. The frontend calls
`notify({kind, title, message, action})` (legacy `addNotification(msg, type)` still works).
`core/notification-center.js` owns the toasts, the right-edge sheet and the bell badge —
**apps and packages must not build their own toast, panel, badge or event history**
(`tests/test_notification_ui.py`). Native packages use `ctx.notifications.{toast,emit,open}`.
Web Push permission (for example Sticky Notes reminders) goes through
`requestNotificationPermission()` in `core/notification-center.js`; the service worker owns the
`push` / `notificationclick` handlers, and no app or package touches the `Notification` API.

**Sharing.** Cross-account access exists only as an explicit view-only grant in
`<DATA_DIR>/sharing.json`, managed by `services/sharing_service.py` and exposed at `/api/sharing`.
Read-only endpoints use `path_scope.sanitize_read`; every write uses `sanitize`, so a received
share can never become write access. Never add a per-app share store, never share `<DATA_DIR>`, and
never expose Photos Private/encrypted items. Spec `docs/sharing.md`; guard
`tests/test_sharing_api.py` + `tests/test_path_scope_sharing.py`.

**Progress.** Long-running actions (uploads, copy/move, delete/trash, package/engine installs) report
through `core/progress.js` — `startTask` → `update`/`finish`. No bespoke progress cards or panels in
apps; finished rows persist until cleared. See `.github/instructions/frontend.instructions.md` and
`docs/progress-ui.md`.

**App windows.** Every core app uses the shared shell — `mountShell()` from `core/ui-shell.js` plus
the `.ui-*` classes in `frontend/static/css/ethos-ui.css` — with the same metrics as the native
package windows (218 px text-only rail, 24/28/32 main padding, 15 px panels, 11 px tables, tags,
status pill, both themes). No `<style>` injection, no hardcoded colours, no `.glass-card`/`.app-tab`
in apps. Files, Terminal and Login are the only documented layout exceptions. Spec
`docs/app-ui.md`; rule `.github/instructions/app-ui.instructions.md`; skill `app-ui`; guard
`tests/test_app_ui.py`.

**Package ribbon.** A pinned native package may declare `ui.ribbon = {entry, title}`. The core
mounts exactly one ribbon in `#ethos-ribbon-host` through `core/package-ribbon.js` (z 950, hidden
below 900 px) and calls the entry's `mount(host, ctx)`; the package owns all visuals and must not
build its own fixed right-edge host. `ctx.setDesktopGutter(44)` shifts the desktop icon grid
(`--desktop-right-gutter`). Spec `docs/sticky-notes.md`, `docs/packages.md`; guard
`tests/test_package_ribbon.py`.

**PWA.** The desktop shell and the three standalone phone apps — `/notes`, `/videos`, `/music` —
share `frontend/sw.js`. `/manifest*.webmanifest`, `/sw.js`, `/notes`, `/videos`, `/music` (each also
with a trailing slash) are **exact** public paths; the worker is network-first, never caches `/api/*`,
owns `push`/`notificationclick` for package reminders and precaches each standalone page's whole
module graph (core modules included) so an installed app can open offline. API calls still require a
JWT. Specs `docs/notes-pwa.md`, `docs/videos-pwa.md`, `docs/music-pwa.md`; guard
`tests/test_pwa_assets.py`, `tests/test_notes_pwa.py`, `tests/test_music_pwa.py`.

**Updates.** Core releases ship a **signed** `releases/index.json` + `.sig`, produced only by
`scripts/ethos-release publish` (offline Ed25519 key at `~/.ethos-pack/ethos-packages/repo.key`, public
half pinned in `backend/config.py` and `setup_scripts/setup_service.sh`). Bump `APP_VERSION` for any
user-visible change; publish **after** the code commit is pushed; commit the index separately and push.
Servers apply it through the argument-free root helper `deploy/ethos-update` (verifies the index itself,
`git reset --hard` + pip as the repo owner, restart, 90 s health probe, auto-rollback). Never hand-edit
the index, never commit the key, never let the API pass the helper a URL/ref/command. Spec
`docs/updates.md`; skill `release-core-update`; guard `tests/test_update_helper.py`.

## Skills — load before working

| Task | Skill |
|---|---|
| New/changed endpoint or `/api` domain | `add-api-endpoint` |
| WebSocket (`/ws/*`, codes, reconnect) | `ws-endpoint` |
| JSON persistence, schema, migration | `state-store` |
| Desktop app / window / icons / XSS | `frontend-app` |
| App window layout / styling / design system | `app-ui` |
| Notifications (toast, sheet, package contract) | `notification-ui` |
| Logs (source, viewer, `ctx.log`, retention) | `logging-ui` |
| Packages package, manifest, compose, signing | `add-package` |
| Tests (API, security, Playwright) | `add-test` |
| Pre-commit verification | `verify-changes` |
| TODO.md item | `todo-item` |
| New feature plan doc | `feature-doc` |
| Repro / debug without touching prod | `isolated-dev-instance` |
| Android APK release | `release-android-apk` |
| Cut/publish a core release now (bump + signed index) | `release` |
| Every push → version bump, CHANGELOG, tag `vX.Y.Z` | `release-tag-changelog` |
| Core version release / updater change | `release-core-update` |
| Backend security audit | `security-invariant-review` |

## Workflow

- **Server restarts:** never restart the service yourself — ask the user (UI or terminal).
- **Git:** after each finished task, commit and push: `feat(<scope>): …` / `fix(<scope>): …`.
  Every push of runtime changes then goes through `release-tag-changelog`: bump
  `APP_VERSION`, add the `CHANGELOG.md` section, publish the signed index, tag `vX.Y.Z`.
  Docs-only pushes keep the version.
- **Backlog:** `TODO.md` is the only backlog. Read only the section you need; full text of completed
  items is in the private archive repo (`git log private-archive/master`), not in this checkout.
- **Features:** create `docs/<feature>.md` with plan + file map + status checkboxes, update as work lands.
- **Android:** any change in `ethos-moments` / `ethos-video` requires bumping
  `versionCode`, running `./build.sh`, verifying `/api/app/version?app=<app>` and mirroring the feature
  on the web — use `release-android-apk`. `ethos-music` is **retired**: the phone client is the
  `/music` PWA, and `backend/routers/updates.py` answers 404 for `app=music` (spec
  `docs/music-pwa.md`). Never resurrect the Music APK.
- **Parity:** every user-facing feature of a phone client must exist in its counterpart — the
  Android APKs (`ethos-moments`, `ethos-video`) and the core PWAs (`/notes`, `/videos`, `/music`);
  report the parity check.
- **Done:** `verify-changes` green, server up on :9000 (401 health check), report changed files.
- **Scope:** small focused changes; no unrelated refactors; ask when ambiguous.
- **Autonomy:** don't ask the user to run commands you can run yourself; ask only when ambiguous, risky,
  or inherently manual (sudo, UI restart).
