# App store model — slim core vs packages

Status: **phases 1-2 and 4 implemented**, phase 3 (desktop surface) not started.
Verified against the code on 2026-11-15.

## Why

`requirements.txt` is a single lock for the whole product: every heavy dependency is a hard startup
requirement because `backend/main.py` imports all 39 routers at module level. A Raspberry Pi 4
install therefore carries OpenCV + numpy (~130 MB) and yt-dlp (~17 MB) even when the admin never
opens Music or face-cropped video thumbnails.

Two separate problems hide behind "slim it down":

1. **Dependency weight** — only OpenCV (+numpy) and yt-dlp can actually leave the core venv.
   Everything else is load-bearing (see the table below).
2. **App surface** — 26 built-in apps on one desktop grid, all shipped and versioned together.

The fix for both is the same primitive: a package that carries its own Python environment.

## Boundary

| Plane | What lives there | Why |
|---|---|---|
| **Core — APIs and system apps** | The API for every app (files, gallery, music, videos, surveillance, editor/git, shares, disk-clean, kanban-as-package, …) plus the apps that *are* the system: Files, trash, search, Settings/users/appearance, Logs, Packages, Docker Manager, Overview, Processes, Storage, Network, Firewall, Services/systemd, SMART, Cron, Shared Links, Terminal | Needs the service identity and its invariants: narrow sudoers rules, `docker` group, `pamela` login, `psutil` host metrics, `cryptography` signature verification, `path_scope` on every path, and the public exact-path set in `main.py`. Keeping the **API** here is what lets a package ship a UI without breaking any of that (and keeps the Android apps working). |
| **Packages — desktop apps** | `ethos-kanban` (own service), `ethos-photos`, `ethos-music` (+ engine), `ethos-video`, `ethos-surveillance`, `ethos-editor`, `ethos-git-diff`, `ethos-shares`, `ethos-cleaner`, `ethos-media-enhance` (engine), plus third-party apps | Pure presentation or dependency-heavy engines. A UI-only package calls the core API with the caller's own JWT, so permissions, path scope and sudoers are unchanged. |

Every moved UI keeps a core router; that is deliberate, not a leftover. Moving a router would move
its security decisions too, and the invariants above are what make the core trustworthy.

## Dependency triage

| Dependency | Verdict | Reason |
|---|---|---|
| `fastapi`, `uvicorn[standard]`, `python-multipart` | core | framework + `File()`/`Form()` endpoints |
| `PyYAML` | core | compose policy; already pulled in by `uvicorn[standard]` |
| `Pillow` | core | Files thumbnails, wallpapers, share previews, surveillance |
| `psutil` | core | dashboard, processes, storage, network, health monitor |
| `docker` (SDK) | core | Docker Manager (built-in by design) |
| `pamela` | core | PAM login |
| `cryptography` | core | package signature verification (security invariant) + Photos private |
| `aiofiles` | removed | uploads write through `asyncio.to_thread` |
| `opencv-python-headless` (+`numpy`) | **package** | `ethos-media-enhance`, face-aware crops only |
| `yt-dlp` | **package** | `ethos-music-engine`, the only dep that wants daily updates |

After extraction the core `requirements.txt` is: fastapi, uvicorn[standard], python-multipart, PyYAML,
Pillow, psutil, docker, pamela, cryptography. `scripts/check_core_deps.py` fails if any of the
removed distributions reappears — there is no pending allowlist any more.

## Mechanism

- Manifest: `runtime: "compose" | "venv"`, `python: {entry, port, requirements[], wheels[{url, sha256,
  arch, filename}], requires_python}` — spec in `docs/packages.md`.
- Install: verified wheels → `<pkg>/.wheelhouse/` → `<pkg>/venv` via
  `pip --no-index --find-links --only-binary :all:`. **No live PyPI, no core-venv mutation.**
- Run: `<venv>/bin/python <entry>` as the service user, one loopback port, stdout/stderr into the
  unified log as `pkg:<id>`, bounded restart-on-crash, autostart at boot.
- Talk: `services/package_client.py` (sync, used from `asyncio.to_thread`). A missing or stopped
  package raises `PackageUnavailable`, which the router turns into a 503 with an install hint.
- Dependencies between packages: `requires` is installed depth-first before the target, `conflicts`
  blocks the install, and uninstalling a package that others require is refused.
- Uninstall: stop the process, delete `<pkg>/` (venv included), keep `<DATA_DIR>/packages/data/<id>`.
- Guard: `scripts/check_core_deps.py` + `tests/test_core_deps_guard.py`.

## Published packages

| id | port | owns | consumed by |
|---|---|---|---|
| `ethos-music-engine` | 8096 | yt-dlp search/resolve/download | `routers/music.py` (`/api/music/*`, Android `ethos-music`) |
| `ethos-media-enhance` | 8097 | OpenCV face detection | `routers/files.py` video thumbnails |
| `ethos-kanban` | 8098 | boards + native UI | its own window (`user_access: true`) |
| `ethos-sticky-notes` | 8099 | per-account notes + native UI | its own window (`user_access: true`) |
| `ethos-books` | 8089 | per-account library metadata, covers, progress + native UI | its own window (`user_access: true`); book bytes come from `/api/files/raw` |
| `ethos-photos` | — | Photos desktop UI | core `/api/gallery` + `/api/moments` |
| `ethos-music` | — | Music desktop UI | core `/api/music` (+ `ethos-music-engine`) |
| `ethos-video` | — | Videos desktop UI | core `/api/videos` |
| `ethos-surveillance` | — | Cameras desktop UI | core `/api/surveillance` + camera service |
| `ethos-editor` | — | Code Editor workbench | core `/api/editor` |
| `ethos-git-diff` | — | Git diff / staging UI | core `/api/editor/git/*` |
| `ethos-shares` | — | Samba Shares UI | core `/api/shares` (sudoers stays core) |
| `ethos-docker` | — | Docker Manager UI | core `/api/docker` (engine helper stays core) |
| `ethos-cleaner` | — | Disk Cleaner UI | core `/api/disk-clean` (`path_scope`) |

The first two are **engine** packages: no `ui` block, no desktop icon. `music.py` keeps every
route Android expects and only the yt-dlp work moved; `files.py` falls back to the first valid
frame when the package is absent, so thumbnails never fail.

`ethos-kanban` is the reference **user-space app**: a native UI plus a stdlib-only service (no
wheels at all), reached through the authenticated package proxy. It is the pattern for moving any
further app — the core keeps no Kanban code, the desktop layout is remapped to `pkg-ethos-kanban`,
and the old boards are imported on first start. `ethos-sticky-notes` follows the same pattern
(port 8099): the core router, ambient widget and its test are gone, and the old
`sticky_notes.json` is imported by the package service.

### UI-only packages

Photos, Music and Videos have **no service at all**: they are files-only packages with a
native UI module that calls the core API with the caller's own JWT through `ctx.apiFetch`. That is
why they need neither `user_access` nor a loopback port — and why the Android apps (which use
`/api/gallery`, `/api/music`, `/api/videos`) are unaffected. A files-only package carries no
runtime, so `status` reports `not_configured` and Packages shows no start/stop buttons.

Internal launchers keep working: Files still calls `openApp('gallery')`, and `app.js` maps
legacy ids (`gallery`, `music`, `videos`, `kanban`) to their package.

### Apps that stay in core, and why

Nothing user-facing is left except the system apps in the table above. The four hard cases are now
**packaged UIs over core APIs**:

| App | What stayed in core |
|---|---|
| Samba Shares | `/etc/samba/smb.conf` writes through the narrow sudoers rule, `testparm`, reload of `samba`/`smbd`, passdb operations through `ethos-samba-accounts` |
| Docker Manager | the engine helper `ethos-docker-install`, compose policy/runtime and `/api/docker/*` — every container package depends on them |
| Code Editor / Git Diff | file reads/writes through `path_scope`, git and PTY/agent handling |
| Disk Cleaner | every scan and delete through the scoped file service |
| Surveillance | camera threads, ffmpeg recordings, motion events |

`/api/packages/{id}/proxy/*` is admin-only unless the manifest sets `user_access: true` (native UI
only, pinned source only). The proxy never forwards client headers: it derives `X-Ethos-User` and
`X-Ethos-Role` from the verified JWT and stamps them on the loopback request, so a package can do
its own per-user separation without trusting anything the browser sent.

**Host software is never installed by a package.** A package that needs OS binaries declares
`system_requires` (registry in `backend/services/system_deps.py`); EthOS probes for them before
deploying and, when they are missing, reports the exact `apt` command instead of running it
(`docker` is the only provisionable id, through the privileged helper). Requirements flagged
`optional` (e.g. `archive-tools` for the Download Manager) never block an install — EthOS tries the
helper and otherwise leaves them to the operator. Samba Shares is the
reference case: `ethos-shares` declares `system_requires: ["samba"]`, Packages disables
Install with the missing-binary list, and the app itself shows the same warning banner and disables
“+ Add Share” — both paths read the same probe. See `docs/packages.md` → System requirements.

## File map

| What | Where |
|---|---|
| Manifest schema (runtime, python, wheels, min version) | `backend/services/package_schema.py` |
| Wheelhouse download/verify | `backend/services/package_wheels.py` |
| venv build + process supervisor | `backend/services/package_runtime.py` |
| Installer dispatch + rollback | `backend/services/package_installer.py` |
| Job step plans | `backend/services/package_jobs.py` |
| Boot/shutdown hooks | `backend/main.py` (`lifespan`) |
| Repo tooling (wheel URLs + hashes) | `scripts/ethos-pack` |
| Core-dependency guard | `scripts/check_core_deps.py` |
| Tests | `tests/test_package_runtime.py`, `tests/test_core_deps_guard.py` |

## Data model

| Path | Content |
|---|---|
| `<DATA_DIR>/packages/installed.json` | record now carries `runtime`, normalized `python`, `ui.port` |
| `<DATA_DIR>/packages/apps/<id>/` | payload: compose **or** `python.entry` + `.wheelhouse/` + `venv/` |
| `<DATA_DIR>/packages/data/<id>/` | service state (`HOME`, `TMPDIR`), kept across uninstall |

## API

No new routes. `GET /api/packages/{id}/status` returns `runtime`; `POST /api/packages/{id}/action`
handles `start`/`stop`/`restart` for both runtimes; job step names gained `wheels` and `venv`.
`GET /api/packages/catalog/{id}` now also returns `system_status` (host-binary preflight for the
Install button) and `GET /api/shares/status` returns `samba_installed`, `missing_tools` and
`install_hint`.

## Status

- [x] Schema: `runtime`, `python` block, wheel validation, `min_ethos_version` enforcement
- [x] Offline wheelhouse (hash-pinned, arch-checked, sdists rejected)
- [x] Private venv build + `--no-index` install
- [x] Process supervisor (health probe, bounded restarts, `pkg:<id>` logs, autostart, shutdown)
- [x] Installer/update/uninstall/status/action dispatch + rollback for venv packages
- [x] `ethos-pack` builds wheel URLs and verifies wheel hashes
- [x] Core-dependency guard + tests
- [x] **Phase 2a** — yt-dlp extracted into `ethos-music-engine`; `/api/music` calls it over loopback
- [x] **Phase 2b** — OpenCV extracted into `ethos-media-enhance`; `files.py` falls back to the first frame
- [x] **Phase 2c** — `aiofiles` dropped, guard allowlist emptied
- [x] **Phase 4** — `requires` resolver, `conflicts`, dependent-uninstall guard
- [x] **Phase 3** — Kanban migrated: `ethos-kanban` (native UI + stdlib service, `user_access`),
  core router/app/registry entries removed, desktop layouts remapped to `pkg-ethos-kanban`, boards
  imported from `<DATA_DIR>/kanban_boards.json` on first start
- [x] **Phase 3b** — Photos, Music, Videos and Cameras UIs moved to files-only packages
  (`ethos-photos`, `ethos-music`, `ethos-video`, `ethos-surveillance`); their core APIs stay for
  Android parity (or for the headless camera service), legacy app ids are aliased, and `ethos-music`
  declares `requires: ["ethos-music-engine"]` so Packages installs the engine first
- [x] **Phase 3c** — `user_access` proxy policy: signed-in users reach a native package UI, the
  server derives `X-Ethos-User`/`X-Ethos-Role` from the JWT and never trusts client headers
- [x] **Phase 3d** — every remaining desktop app moved: `ethos-shares`, `ethos-cleaner`,
  `ethos-git-diff`, `ethos-editor` (UI-only; their APIs stay core). Opening a file from Files
  still routes through `window.openEditorForPath`, which now calls back into the loaded package and
  falls back to a path handshake when it is not loaded
- [x] **Phase 3e** — Sticky Notes migrated: `ethos-sticky-notes` (native UI + stdlib service,
  `user_access`), core router/widget/bootstrap/test removed, legacy `sticky_notes.json` imported
  on first start; the package proxy now forwards PUT/PATCH/DELETE as well as GET/POST

## Verifying a moved UI

A package UI is a blob module, so it cannot be imported by the app's own tests. The working recipe
(the one used for Kanban, Photos, Music, Video and Surveillance) is a jsdom harness outside the repo:

1. Copy the package's `frontend/app.js` into a scratch directory, copy `index.html`/`shim` from the
   previous harness if you need a browser, and provide a `ctx` stub:
   `apiFetch` → `fetch`, `escape` → the same helper, `notifications.toast`, `modal.confirm/form`,
   `log`.
2. Stub the global listeners and observers a browser provides (`addEventListener`,
   `IntersectionObserver`, `ResizeObserver`, `CSS.escape`, `HTMLMediaElement.play`).
3. Import the module, call `render(container, ctx)`, then assert DOM (`[data-testid=…]`), drive clicks,
   and check `no error toast`.
4. For API-backed checks, point the stub at a real isolated instance with a token minted by
   `_create_token(...)`, or at the package service directly.

Do not skip this for a UI move: a shim gets the module to mount, but only a few interactions prove the
handlers still resolve.

## Open questions

1. Wheel transport: signed-repo download (current) vs. bundling wheels in the package archive.
   Download keeps packages small; the artifact cache makes retries offline.
2. Supervisor: in-process asyncio (current) vs. systemd transient units (survives core restarts).
3. `ethos-media-enhance` interface: persistent loopback HTTP with batching (planned) vs. a
   subprocess per thumbnail.
