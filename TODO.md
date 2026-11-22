# EthOS NAS — Master TODO

> **Open work only.** Completed items are one-line entries below; full detail (problem / fix /
> evidence) lives in the private archive repository (`private-archive` remote, `SyncHot/ethosnas`),
> together with the historical 2026-11-11 audit scorecard and execution plan.

## How to use

1. Work top-down: **P0 before P1 before P2**. Never start an architecture item while a P0 is open.
2. Reference the item **ID** in commits: `fix(security): require admin on system endpoints (P0-01)`.
3. When done: flip `☐` → `☑`, append `— <sha>` on the heading, move the body to the archive, keep the
   one-line heading here.
4. If an item is wrong or already fixed, **delete it** and note it in "Closed / not-an-issue".
5. Re-verify `path:line` anchors before acting — this file is a snapshot, code moves.

## Legend

| Symbol | Meaning |
|---|---|
| P0–P4 | security/data loss · correctness · architecture · UX · tests & CI |
| P5–P7 | ops · docs · feature roadmap |
| `S` / `M` / `L` / `XL` | <2 h / 1–2 days / 3–5 days / >1 week |

## Status

**Open: 16 tracked items** (plus the legacy checklist in §9). All P0 security/data-loss items are
closed; CI, lint, the test runner and atomic stores are in place.

---

# §1 · P0 — Security & data loss

### ☑ P0-01 · Require admin role on privileged endpoints — `M`

### ☑ P0-13 · Extend admin scope to the remaining privileged routers — `M`

### ☑ P0-02 · Replace prefix-matching `public_paths` with an exact allowlist — `S`

### ☑ P0-03 · Stop logging credentials — `S`

### ☑ P0-04 · Unify the user store (two divergent files, one wrong) — `M`

### ☑ P0-05 · Make CORS and JWT secret actually enforced — `S`

### ☑ P0-06 · Route `sync.py` and `file_ops.py` through the file role scope — `S`

### ☑ P0-07 · Remove the hardcoded password from tests and the test token — `S`

### ☑ P0-08 · Neutralize the three deprecated sudoers installers — `S`

### ☑ P0-09 · Stop leaking server paths from the public APK endpoint — `S`
> Re-audited for the public release: `/api/app/version` still returned an absolute path in `url`.
> It now returns an app-scoped `/api/app/download?app=…` URL and the APK is streamed by the router.

### ☑ P0-10 · Introduce `services/json_store.py` and migrate unsafe writers — `L`

### ☑ P0-11 · Bound unbounded stores and caches (disk-fill risk) — `M`

### ☑ P0-12 · Pin dependencies and add a lockfile — `S`

# §2 · P1 — Correctness & reliability

### ☑ P1-01 · Replace silent `except: pass` with logged failures — `M`

### ☑ P1-02 · Fix logging coverage and naming — `S`

### ☑ P1-03 · Remove the dead `ss | grep` port probe — `S`

### ☑ P1-04 · Stop blocking the event loop in the Samba router — `S`

### ☑ P1-05 · Move kanban store I/O off the event loop — `S`

### ☑ P1-06 · Register every background task so shutdown can cancel it — `M`

### ☑ P1-07 · Quarantine corrupt stores instead of silently resetting them — `S`

### ☑ P1-08 · Add pagination to unbounded list endpoints — `M`

### ◐ P1-09 · Give request bodies and responses real models — `M`
**Problem.** `response_model=` is effectively unused, so `/docs` is inaccurate and nothing validates request
shape; ~20 endpoints took a raw `dict` with ad-hoc checks.
**Landed so far.** `file_ops` bodies are typed. `downloads` now has `AddTaskRequest`
(`url` 1…8192 chars, optional `name` ≤255, `folder` ≤4096, `extra="forbid"`), `SelectFilesRequest`
(`file_ids: [int|str]`, 1…2000) and `VerifyDebridRequest` (`provider` ≤32, `key` ≤512) — a typo'd field is
now a 422 instead of a silently ignored key.
**Verified live.** `""` url → 422 · unknown key → 422 · 9000-char url → 422 · valid url → 200 · empty
`file_ids` → 422 · unknown provider → 400 · missing key → 400. The SPA sends exactly `{url, name, folder}`
and `{file_ids: [...]}`, so nothing breaks.
**Remaining.** `music.py` (11 dict bodies), `gallery.py:874`, `video_station.py` (`set_config`,
`set_resume`, `tmdb_refresh`), `downloads.save_config` (partial-config body — deliberately permissive), and
`response_model=` on the main reads.

### ☑ P1-10 · One error contract across the API — `M`

### ☑ P1-11 · Fix silent upload overwrite and abandoned chunks — `S`

### ☑ P1-12 · Don't block startup on download WAL replay — `S`

### ☑ P1-13 · Fix inverted toast arguments and the double badge writer — `S`

### ☑ P1-14 · Emit notifications from the remaining state-changing subsystems — `S`

### ☑ P1-15 · Extract OpenCV and yt-dlp out of the core venv — `L`
**Done** (`379c5e5`). `runtime: "venv"` packages with an offline wheelhouse, a private
per-package venv and a supervised loopback process. yt-dlp shipped as `ethos-music-engine`,
OpenCV as `ethos-media-enhance`; `music.py` and `files.py` call them through
`services/package_client.py` and fail closed with an install hint. `requirements.txt` lost
opencv-python-headless, yt-dlp and aiofiles. Spec: `docs/app-store.md`, `docs/packages.md`;
guard: `scripts/check_core_deps.py` (no allowlist left).

# §3 · P2 — Backend architecture & code health

> Routers are supposed to hold HTTP concerns; `backend/services/` holds logic. Today the split is
> inconsistent: only ~10 services exist while 8 routers exceed 800 lines. Each item below is a
> **mechanical move + tests**, not a rewrite. Do them one router per commit, with the router's test
> green before and after.

### ◐ P2-01 · Split `downloads.py` (~2.2k lines) — `XL`
**Problem.** Three torrent/debrid provider clients, a bencode parser, the HTTP engine, the pump, watch
folders and the qBittorrent shim all live in one router.
**Evidence.** `backend/routers/downloads.py` (debrid worker ~887–1435, bencode ~332–470,
worker ~1235–1360).
**Fix.** Extract `services/bencode.py`, `services/debrid/{realdebrid,alldebrid,torbox}.py`,
`services/download_engine.py`. Router keeps endpoints + request models only.
**Done when.** `downloads.py` <400 lines; each extracted module has a unit test; existing
`tests/test_*download*` still pass unchanged.

**Progress (2026-11-14).** Extracted with unit tests: `services/bencode.py`
(torrent/magnet parsing), `services/rss_service.py` (feed parsing + rules),
`services/debrid.py` (all three provider clients + retrying HTTP helper),
`services/download_config.py` (defaults, v1→v5 migration, normalization, load/save,
qB key, category/RSS/watch config), `services/download_tasks.py` (serialization,
secret stripping, terminal-history cap) and `services/download_transfer.py` (part
files, range resume, disk guard, bandwidth token bucket). Dedicated tests:
`test_downloads.py` (API, path scope, providers, queue, transfers), `test_qb_compat.py`,
`test_bencode.py`, `test_debrid_service.py`, `test_download_config.py`,
`test_download_tasks.py`, `test_download_transfer.py`, `test_rss_service.py` and
`test_downloads_ui.py`. Still inline: pump/worker scheduling and watch folders (the file
is ~1.9k lines after the feature work).

### ☐ P2-02 · Split `gallery.py` (~2.1k lines) — `XL`
**Problem.** SQLite indexing, EXIF extraction, the AES-GCM private vault + PIN sessions, albums, facets,
thumbnails and rescan logic share one file.
**Evidence.** `backend/routers/gallery.py` (crypto ~162–360, index scan ~602–805, migrations ~400–415).
**Fix.** Extract `services/gallery_index.py` (schema + scan + migrations), `services/gallery_private.py`
(crypto + sessions, with the in-memory session dict bounded by TTL), `services/gallery_facets.py`.
**Done when.** `gallery.py` <500 lines; private vault has its own unit test (encrypt → index → decrypt →
trash path is `.ethos.enc`).

### ☐ P2-03 · Split `files.py` (~1.95k lines) — `XL`
**Problem.** Listing, upload, thumbnails (ffmpeg + Haar face detection), transcode, archive extract,
universal search and the trash store are one router, and a parallel `files_service.py` re-implements
favorites/trash/thumb metadata.
**Evidence.** `backend/routers/files.py` (media pipeline ~290–715, search ~1513–1600, trash ~1730–1920);
duplicate logic in `backend/services/files_service.py:77,391,450`.
**Fix.** Extract `services/thumbnails.py`, `services/transcode.py`, `services/trash_service.py`,
`services/search_service.py`; delete the duplicated implementations (P2-11).
**Done when.** `files.py` <500 lines; one code path per operation; File Manager tests pass.

### ◐ P2-04 · Split `music.py` (~1.4k) and replace the hand-rolled RSS parser — `L`
**Problem.** yt-dlp wrappers, the per-user library store, podcast RSS parsing and radio share one router;
the RSS/XML parsing was hand-written and fragile, and `pip install --upgrade yt-dlp` was reachable by any
authenticated account.
**Landed.** `services/podcast_service.py` replaces the regex parser with `xml.etree.ElementTree` (works on
local tag names, so `itunes:`/`podcast:` namespaces behave; a CDATA body containing a closing item tag can
no longer split an episode; `<itunes:image href>` → `<image><url>` → `<logo>` cover fallback; Atom
`rel="enclosure"`; drops unplayable items and raises `FeedError` instead of returning partial data). The
router keeps a 6-line `_parse_feed` wrapper that maps `FeedError` → 422 with the message the UI already
shows — `music.py` dropped from ~1.4k to 1247 lines. `POST /api/music/ytdlp/update` is now
`require_admin` (it installs from PyPI into the venv — an ordinary account must not trigger that; the
Music app keeps its button for admins) and pip runs with `--no-input`.
**Verified.** `tests/test_podcast_feed.py` — 25 checks (RSS + Atom shapes, CDATA, namespace cover, duration
and date variants, unplayable-item and non-feed rejections); live check: a `user`-role token gets 403 from
`ytdlp/update` while `GET /api/music/library` still answers 200.
**Remaining.** `ytdlp_service.py` / `music_library.py` extraction and the full router split — the line
budget (<400) is not met yet.

### ☐ P2-05 · Split `video_station.py` (~1.3k) — `L`
**Problem.** Library walk, ffprobe metadata, HW transcode, subtitle handling, resume state and TMDB
enrichment in one file.
**Evidence.** `backend/routers/video_station.py` (walk ~346–406, transcode dispatch ~560–580 and ~990–1024).
**Fix.** Extract `services/transcode_service.py` (shared with `files.py`'s transcode — one implementation),
`services/video_library.py`, `services/tmdb_enrich.py`.
**Done when.** `video_station.py` <450 lines; Files and Videos share one transcode service.

### ☐ P2-06 · Split `editor.py` (~1.3k) — `L`
**Problem.** 30+ endpoints of git porcelain (`_git`, index manipulation, hunk filtering, merge conflicts)
sit in the router, making it the hardest file in the repo to test.
**Evidence.** `backend/routers/editor.py:212-800`.
**Fix.** Extract `services/git_service.py` with a thin `_git(args, cwd)` and typed results; router keeps
endpoints. Also drop the many `except: pass` in git paths (P1-01).
**Done when.** `editor.py` <400 lines; `tests/test_editor.py` passes; git operations are testable without HTTP.

### ☑ P2-07 · Split `backup.py` (~1k) — `M`
**Problem.** A cron expression parser, the scheduler loop, rsync/docker backup executors and the restore
flow live in the router; the scheduler thread writes the same store as request handlers (P0-10).
**Evidence.** Extracted in `ed42acb`: `services/cron_utils.py`, `backup_store.py`, `backup_layout.py`,
`backup_engine.py`, `backup_docker.py`, `backup_remote.py`, `backup_jobs.py`, `backup_scheduler.py`,
`backup_models.py`; `backup.py` is 390 lines (CRUD/API only). Cron has a table-driven test in
`tests/test_backup_engine.py`.

### ☐ P2-08 · Split `docker.py` (~850) — `M`
**Problem.** Container/image/volume operations, compose project management and prune flows in one router,
with docker-py called through `asyncio.to_thread` in most places but JSON persistence inline.
**Evidence.** `backend/routers/docker.py:576-590,825`.
**Fix.** Extract `services/compose_service.py`; route all persistence through `Store` (P0-10).
**Done when.** `docker.py` <450 lines; compose timeouts surface as 504 with a log entry, never as a 200 dict.

### ☑ P2-09 · Schema versions + migrations for every store — `M`

### ☑ P2-10 · Consolidate configuration into `settings` — `S`

### ☑ P2-11 · Delete duplicate store implementations — `M`

### ☑ P2-12 · Enforce the router/service boundary with a lint check — `S`

# §4 · P3 — Frontend architecture, UX & accessibility

### ☐ P3-01 · `core/api.js` v2 — one error path for the whole app — `M`
**Problem.** `apiJsonFetch` throws `new Error('HTTP 403')` with no body, so every app re-implements
`detail` parsing (~10 copies) and 403 permission messages from the backend never reach the user
except in `files.js`. Raw `fetch()` without the auth header exists in `settings.js:36,166` and
`app.js`'s package handlers, which 401s on protected routes.
**Evidence.** `frontend/static/core/api.js:33-53`, `frontend/static/apps/settings.js:36,166`, plus the
Packages apps now in `~/apps/compose/ethos-packages` (`download-manager`, `ethos-kanban`,
`ethos-git-diff`, `ethos-shares`, `ethos-cleaner`), `access-control.js:30`.
**Fix.** `apiRequest(path, {method, body, timeoutMs})` → parsed JSON or throws `ApiError{status, detail,
kind, payload}`; `getJson/postJson/putJson/deleteJson` helpers; `apiJsonFetch` kept for the 20+ existing
call sites but now throws an error carrying `detail`. 401 re-auth unchanged.
**Progress.** Layer done, plus the de-duplication: `readErrorDetail(res)` was added for call sites that
still branch on `res.ok`; `access-control`'s `_detail`, `downloads`' `_post` and `git-diff`'s `_post` are
gone (the latter two now delegate to `postJson`, kept as hoisted function declarations); the ad-hoc
`(await resp.json().catch(() => ({}))).detail || 'HTTP ' + status` parsing in `kanban`, `shares`,
`cleaner`, `video-station` is replaced with `readErrorDetail`, so 403 `detail` text (the File Manager
permission explanation) reaches the toast instead of "HTTP 403".
**Remaining.** `backup.js`/`cron.js`/`docker.js` still build the message from an already-parsed body
(`d.detail || HTTP n`) — behaviourally correct, cosmetic dedupe only; the browser pass per app.

### ☑ P3-02 · Fix HTML escaping and remove the six local copies — `M`

### ☐ P3-03 · Remove `onclick="window.x(...)"` string handlers — `L`
**Problem.** 301 `window.*` assignments exist because templates emit inline handlers, which forces every
function into the global namespace, blocks any future module encapsulation, and makes CSP impossible.
**Evidence.** `files.js` (32), `app.js` (31), plus the package UIs (`ethos-photos` 69, `ethos-video`
42, `ethos-kanban` 22, `ethos-docker` 14, `ethos-shares` 10), `settings.js:166` (a whole fetch chain
inline in an `onclick`).
**Fix.** Convert app by app: `data-action="…"` + one delegated listener per container, using a small
`delegate(root, 'click', '[data-action]', map)` helper. Keep `window.*` only for the few APIs the
inline scripts in `index.html` genuinely need, behind one `core/globals.js`.
**Done when.** `grep -c 'window\.' frontend/static` drops below ~30 and no template contains `onclick="window.`.

### ☐ P3-04 · Split `editor.js` (~4.5k lines) — `XL`
**Problem.** Editor core, tabs, find/replace, preview, settings dialog and the entire git panel (branch,
stash, remote, diff, conflict) share one module. It is the largest file in the repo and the hardest to change.
**Evidence.** `~/apps/compose/ethos-packages/ethos-editor/frontend/app.js` (~4.5k lines; git panel
~1.2k lines, 14 native `confirm`/`prompt` calls). The app moved out of core in 2.0.0.
**Fix.** `apps/editor/{core,tabs,find,preview,git}.js` with a shared state object; replace native prompts
with the existing modal helpers (also kills fragile `page.on('dialog')` in tests).
**Done when.** No file under `apps/editor/` exceeds ~700 lines; `docs/code-editor.md` phase list still passes.

### ☐ P3-05 · Split `gallery.js` (~3.2k lines) — `XL`
**Problem.** Grid, lightbox, calendar, Moments, picker, album nav, PIN vault UI and ~180 lines of injected
CSS in one file.
**Evidence.** `~/apps/compose/ethos-packages/ethos-photos/frontend/app.js:201` (`_ensureStyles`),
lightbox ~840–900, calendar ~1500–1600. The app moved out of core in 2.0.0.
**Fix.** Split the package module into `frontend/{grid,lightbox,calendar,moments,picker}.js`; CSS moves
to the design-system file (P3-10).
**Done when.** No file under `apps/photos/` exceeds ~700 lines; photo E2E (`tests/test_gallery.py`) passes.

### ☐ P3-06 · Split `files.js` (~3.1k) and move the package renderer out of `app.js` — `L`
**Problem.** Context menus, video player, trash, search, upload/preview and the grid live in one file;
`app.js` still contains a ~120-line generic package renderer plus package uninstall wiring.
**Evidence.** `frontend/static/apps/files.js` (context menus ~1923–2100, video ~2397–2630, trash ~2777–2900),
`frontend/static/app.js:157-268` (`_renderGenericPackage`), `:272-296` (`_uninstallPkg`).
**Fix.** `apps/files/{contextmenu,video,trash,search}.js`; package renderer → `apps/packages.js`.
**Done when.** `app.js` is back to imports + wiring (<150 lines); `files.js` <900 lines.

### ☑ P3-07 · Window lifecycle: close hooks, per-window guards, no taskbar rebuild — `M`

### ☑ P3-08 · Harden `core/ws.js` and delete the per-app copies — `M`

### ☑ P3-09 · One shared, visibility-aware poller — `M`

### ☐ P3-10 · Extract the design system out of `index.html` — `L`
**Problem.** ~2,500 lines of CSS live inline in `index.html` (lines ~78–2583), while five apps inject
their own `<style>` at runtime and `video-station.js` re-injects its CSS on **every render**. Theme tokens
(`--surface-*`, `--border`, `--text-*`, `--accent`) are bypassed with hardcoded hex in 30+ places, and
two icon systems coexist (emoji vs `uiIcon`).
**Evidence.** `frontend/index.html` (the editor still owns a large `.ed-*` block there), plus the
package UIs that inject their own `<style>` (`ethos-photos`, `ethos-music`, `ethos-video`,
`ethos-git-diff`), `login.js:124`.
**Fix.** `static/css/{tokens,base,layout,apps}.css`, one token file, one icon module; delete per-render
style injection. No build step required — plain `<link>` tags.
**Progress.** The app-layer slice is done: `frontend/static/css/ethos-ui.css` + `core/ui-shell.js`
(see P3-15). The desktop/editor/login block still lives in `index.html`.
**Done when.** `index.html` < 300 lines; no app creates a `<style>` element; a token change (accent
colour) updates every app including the editor and terminal.

### ☑ P3-11 · Accessibility pass — `M`

### ☑ P3-12 · Consistent loading, empty and error states — `S`

### ☐ P3-13 · Make the shell usable on a phone — `L`
**Problem.** Windows are sized in absolute px from `window.innerWidth` with a 400×300 resize floor;
right-click context menus and `dblclick`-to-open are mouse-only; fixed widths (files sidebar 236 px,
surveillance `minmax(300px,1fr)`) break at 360 px. Result: it opens on a phone but you cannot manage files.
**Evidence.** `frontend/static/core/window-manager.js:110-127,328`, `files.js:644`, `gallery.js:2245,2540`,
`shares.js:299`, only 3 media queries in `index.html`.
**Fix.** Below 768 px: windows open maximized (no drag/resize), context menus become long-press sheets,
single tap opens items, sidebars collapse to a drawer.
**Done when.** On a 390×844 viewport a user can browse files, open a photo and play a video without a mouse.

### ☑ P3-14 · Add a web app manifest + service worker — `S`

### ☑ P3-15 · Core app shell migration (Settings → every window) — `L`
**Problem.** Core apps invent their own chrome: top tab strips, `glass-card` grids, inline styles and
hardcoded colours, while package windows follow a strict 218 px rail shell. The same action looks
different in every window, and light mode only works through `!important` hacks in `index.html`.
**Evidence.** `frontend/static/apps/settings.js` (4 tabs, ~40 inline style attributes),
`storage.js`, `network.js`, `access-control.js`, `packages.js`, `logs.js` (injected `<style>`),
`share-links.js` (injected `<style>`), `overview.js`.
**Fix.** `frontend/static/css/ethos-ui.css` + `core/ui-shell.js`, documented in `docs/app-ui.md`
(rule `.github/instructions/app-ui.instructions.md`, skill `app-ui`). Migrate Settings first, then the
multi-section apps, then single-pane apps; Files/Terminal/Login keep their layouts.
**Progress.** Done in `e77ad04`: stylesheet, helper, doc, rule, skill and guard; Settings rebuilt around the
rail; Storage/Network/Security/Packages/Logs and all single-pane apps migrated; Login CSS moved out
of JS; legacy `glass-card`/`app-tab`/light-theme hacks deleted. The guard allowlist is empty.
**Done when.** `tests/test_app_ui.py` passes with an **empty** allowlist, `index.html` no longer needs
the `color:white` / `glass-card` light-theme hacks, and every app switches cleanly between themes.

### ☑ P4-01 · Delete `task.sh` and add a real runner — `S`

### ☑ P4-02 · Declare test dependencies — `S`

### ☑ P4-03 · CI workflow — `M`

### ☑ P4-04 · Lint/format config + pre-commit — `S`

### ☑ P4-05 · Close coverage on the 16 untested routers — `L`

### ☑ P4-06 · Stop tests from writing into production data — `S`

### ☐ P4-07 · Convert the scripts to pytest — `M`
**Problem.** 28 standalone scripts print `[PASS]` and `sys.exit(1)`, so nothing aggregates results,
fixtures are re-invented, and `pytest` collects nothing useful.
**Fix.** Keep the `__main__` entry point (it is useful) but factor each script's checks into pytest
functions with shared fixtures (`base_url`, `token`, `tmp_library`, `page`). Start with the five fastest.
**Done when.** `pytest -q` runs ≥60 tests and the standalone scripts still work.

### ☑ P4-08 · `tests/test_security_invariants.py` — `S`

### ☑ P4-09 · `tests/test_stores.py` — `S`

### ☑ P0-14 · Migrate the remaining hand-rolled store writers — `M`

### ☑ P4-10 · Drop unused frontend tooling — `S`

# §6 · P5 — Ops, deployment & durability

### ☐ P5-01 · Harden the systemd unit — `S`
**Problem.** `deploy/ethos-nas.service` sets only `User=`. The service needs its privileged helper
calls to keep working, so most `systemd` sandboxing options are incompatible and each one must be
validated on a real box (share edit, media copy, USB mount) before it ships.
**Fix.** Add the options that are unconditionally safe (`LimitNOFILE`), and evaluate the rest as a
tested matrix. See `docs/ops.md` (unit hardening).
**Done when.** `systemd-analyze security ethos-nas` improves and file operations, uploads, downloads
and transcoding still work end-to-end.

### ☑ P5-02 · One launch path — `S`

### ☑ P5-03 · `docs/ops.md` runbook — `M`

### ☑ P5-04 · Backup & restore for `backend/data` — `M`

### ☑ P5-05 · Schema versioning + migration runner — `M`

### ☑ P5-06 · Log retention and rotation — `S`

### ☑ P5-07 · Retention jobs for caches and trash — `M`

### ☑ P5-08 · Add an app version + update check — `S`

### ☑ P5-09 · `/api/system/doctor` self-check — `S`

### ☑ P5-10 · Secret management — `S`

### ☐ P5-11 · Publish the repository (open-source release) — `M`
**Problem.** The repo is private, has no license, and the history holds runtime data and credentials.
**Fix.** Publish a clean `SyncHot/ethos-nas` from one commit; procedure and checklist in
`docs/open-source.md`. Done in this tree: MIT `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`,
`THIRD-PARTY.md`, `.env.example`, CODEOWNERS/Dependabot/issue templates, `/home/<user>` and
personal LAN defaults removed, APK path leak fixed, JWT secret now generated+persisted when unset,
Synology-derived app names renamed (Files/Packages/Videos/Cameras/Downloads/Photos).
**Status 2026-11-15.** Public repo created (`SyncHot/ethos-nas`, tag `v2.0.0`, MIT, secret scanning
+ push protection + private advisories), package catalog renamed and re-signed, JWT secret rotated
on the production host (`10-jwt-secret.conf`), public CI fixed (legacy log archive, poller/ws/progress
thresholds, a11y kanban path).
**Remaining.** Branch protection (required `ci` check) once the public run is green; new admin API key
and PAM password rotation; archive the old `ethosnas` repo after every checkout is repointed; keep
publishing releases with the snapshot flow in `docs/open-source.md`.
**Done when.** `docs/open-source.md` checklist is fully ticked and CI is green on the public repo.

### ☑ P5-12 · Core update check + apply (signed release index) — `M`
**Problem.** A server could not learn that a newer EthOS release exists, let alone install it.
**Fix.** Signed `releases/index.json` (+`.sig`, offline Ed25519 key), `scripts/ethos-release publish|verify`,
`services/update_service.py` (fetch → verify → compare → cache → notify → 24 h loop), root helper
`deploy/ethos-update` (argument-free sudoers rule; verifies the index itself, `git reset --hard` + pip as
the repo owner, restart, 90 s health probe, auto-rollback), `GET /api/system/update`, `GET …/status`,
`POST …/check`, `POST …` (admin), Settings → System card with *Check for updates* / *Update now* +
progress + restart overlay. Spec: `docs/updates.md`.
**Verified.** `tests/test_update_service.py` 28/28, `tests/test_update_helper.py` 24/24, security
invariants + router smoke green.
**Not covered (new item).** Artifact/tarball installs; store migrations still need the pre-update
snapshot on rollback.

### ☐ P5-13 · Update transport for non-git installs — `M`
**Problem.** `ethos-update` only supports git checkouts (`reset --hard` to the signed commit).
**Fix.** Publish a signed tarball per release, verify its sha256 in the helper, extract to a staging dir
and swap code paths (keeping `.venv`, `backend/data`, untracked files). Reuse the rollback flow.
**Done when.** An install without `.git` can update from Settings and roll back on failure.

# §7 · P6 — Documentation

### ☑ P6-01 · Retire the empty `docs/todo.md` — `S`

### ☑ P6-02 · Reduce `docs/archive/todo-code-editor.md` to a pointer — `S`

### ☑ P6-03 · Fix the stale status blocks — `S`

### ☑ P6-04 · Document the 15 undocumented apps — `M`

### ☑ P6-05 · Correct `AGENTS.md` — `S`

### ☑ P6-06 · Architecture doc: router → service map + store catalog — `M`

### ☑ P6-07 · API conventions doc — `S`

### ☑ P6-08 · ADRs for irreversible decisions — `S`

# §8 · P7 — Feature roadmap / NAS parity

> Not defects. Ordered by value to a self-hosted NAS owner. Each entry states impact, effort and the
> prerequisite item, so it can be scheduled without further design work.

| # | Proposal | Why | Effort | Depends on |
|---|---|---|---|---|
| **F-01** | **Notification delivery: SMTP + webhook (Telegram/Discord)** | A NAS must tell you when a disk dies or a backup fails while you are not looking. The event bus already exists. | `S–M` | P1-14 |
| **F-02** | **TOTP 2FA + session/device management + API-key UI** | Single password over a possibly public port; API keys exist but have no management screen. | `M` | — |
| **F-03** | **Users & Groups: group membership + per-share permissions** | Account lifecycle and role management shipped in v2.7.0 (Settings → Users, `docs/user-management.md`). Still missing: Linux group management and a per-user share ACL editor (Shares keep Samba params only). | `M` | P0-01, P0-04 |
| **F-04** | **Storage health: mdadm RAID status + SMART self-tests + temperature history** | SMART trend alerts exist; RAID degradation (the actual data-loss event) is invisible. | `M` | — |
| **F-05** | **Snapshot browser (btrfs/zfs) + restore** | "I deleted the wrong folder" is the #1 home-NAS incident; trash covers only deletes. Detect FS support and hide when unavailable. | `L` | — |
| **F-06** | **UPS (NUT) integration + graceful shutdown + power schedule/WoL** | Unclean shutdown is how NAS filesystems die. | `M` | — |
| **F-07** | **Remote access: WireGuard UI, DDNS, reverse proxy + ACME** | Every DSM competitor ships QuickConnect; today remote access means port-forwarding the admin UI. | `L` | P0-05, F-02 |
| **F-08** | **Audit Log app** | `deletion_log.json` and login-failure parsing already exist, with no UI and no retention policy. | `S–M` | P0-11 |
| **F-09** | **Media: DLNA/UPnP server, subtitle auto-fetch, transcode presets** | Videos can stream but not serve a TV; subtitles are the most-requested gap. | `L` | P2-05 |
| **F-10** | **Photos: face clustering, AI tags, map view, permissioned shared albums** | GPS EXIF is already parsed; Haar cascades are already a dependency — clustering is the missing layer. | `L` | P2-02 |
| **F-11** | **Downloads: RSS feeds + rules, per-task speed limits, time scheduler** | **Done** (`81de7c4`, `048a8c5`): priority queue + reorder, daily start window (self re-arm), global/per-task token-bucket limits, RSS feeds with include/exclude rules, persisted dedupe and a manual run button. | `M` | P2-01 |
| **F-12** | **Global command palette (Ctrl+K)** | 32 apps is too many for a desktop icon grid; one search across files, apps and actions. | `M` | P3-03 |
| **F-13** | **Android ⇄ web parity matrix + CI check** | AGENTS.md mandates parity; nothing verifies it today. A table per app (screen → endpoint) plus a test that fails when an Android-only endpoint has no web caller. | `S` | P4-03 |
| **F-14** | **Recycle-bin retention policies + per-user storage quotas** | Trash is unbounded and invisible; quota reporting is table stakes for multi-user NAS. | `S–M` | P0-11 |
| **F-15** | **Mobile shell variant (card grid instead of floating windows)** | P3-13 makes the desktop shell usable; a dedicated mobile layout makes it good. | `L` | P3-13 |
| **F-16** | **Backup generations + retention** | **Done** (`ed42acb`): hardlink snapshots under `snapshots/<ts>/`, GFS 7/4/6 retention, legacy-layout migration, protected legacy copy. Packaged UI `backup-manager` 1.0.0. | `M` | P2-07 |
| **F-17** | **Backup verify + integrity manifest** | **Done** (`ed42acb`): atomic publish after `manifest.json`, quick verify after each run, deep checksum verify on demand, dry run; docker archives carry sha256 and are integrity-checked. | `M` | F-16 |
| **F-18** | **Backup cancel + missed-run catch-up** | **Done** (`ed42acb`): bounded queue, `POST /api/backup/cancel`, persisted `last_scheduled_at` and one catch-up after startup (24 h). Weekly digest via `emit_event`. | `S–M` | P2-07 |
| **F-19** | **Package dependency resolver** | **Done** (`379c5e5`): `requires` is installed depth-first before the target (cycle-checked, depth ≤ 5), `conflicts` blocks the install, and uninstalling a package other packages require is refused (409). | `M` | Package v2 |
| **F-20** | **Package key rotation + hosted repo** | Sources pin one key; add multiple keys/revocation, then an optional local repo upload UI. | `M` | Package v2 |
| **F-21** | **`vmm` — EthOS VM Manager package (NAS VM-manager parity)** | KVM/QEMU VMs, browser console, snapshots, clone, OVA export — the one appliance-class app with no EthOS equivalent. **1.0.0 shipped**: create/start/stop/shutdown, browser console, autostart, qcow2 disks on shares; `capabilities: ["kvm"]` gates the single `devices: ['/dev/kvm']` exception. Remaining (v1.1): port forwards, pause/resume, snapshots, clone, OVA export, stats charts. See [`docs/vmm.md`](docs/vmm.md). | `L` | `kvm` capability (done) |
| **F-22** | **Docker Engine provisioning — clean-VM verification + catalog rollout** | Engine status/install/restart and `system_requires` shipped (`docs/docker-manager.md`). Catalog rollout done (2.27.0): `validate_manifest` and `ethos-pack build` add `system_requires: ["docker"]` to every compose package automatically and the catalog is republished, so cards preflight the engine instead of failing in the start step. Remaining: verify `install.sh` + the UI flow on a clean Ubuntu VM. | `S` | — |
| **F-23** | **Auto-ban — temporary, escalating IP bans** | Shipped: native sweeper over failed logins (UFW block floods optional), `ufw insert 1` rules tagged `ethos-autoban`, 1 h → 24 h → 7 d escalation, allowlist + RFC1918/loopback/link-local/ULA/CGNAT/multicast protection, dry-run, caps, expiry/cooldown, notifications, new Auto-ban tab (`e884f8e`). Spec `docs/firewall-access-control.md`; tests `tests/test_autoban.py`. | `M` | — |

---

# §9 · Merged legacy backlog

> Absorbed from `docs/archive/todo-code-editor.md` and `docs/file-manager.md` (§backlog). Items that already
> shipped are ticked with the evidence — do not re-implement them.

**Landed.** `docs/adr/0001` no build step · `0002` JSON+SQLite · `0003` PAM+JWT (incl. the async-dependency rule) · `0004` ffmpeg pipeline.

### From the Code Editor backlog

- [x] **In-file find & replace (Ctrl+F)** — shipped (`docs/code-editor.md` Phase 13; Ctrl+F hint at `editor.js:859`). *Verify in the browser before closing.*
- [x] **Content search ("Find in Files")** — shipped (`/api/editor/search`, Phase 14).
- [x] **External-change detection on save** — shipped (save-conflict detection, Phase 14).
- [ ] **Replace the remaining native `confirm()`/`prompt()` calls** → folded into **P3-04**.
- [ ] **Light-theme support for the editor** — token colours are hardcoded VS Code Dark+. Decide: adapt
      to CSS variables, or document "editor is always dark" and keep it consistent. `S`
- [ ] **Terminal theme follows the app theme** — `_termTheme()` + xterm background, same decision as above. `S`
- [ ] **Git status auto-refresh** — poll `GET /git/status` every ~15 s while the SCM view is visible, or
      consume the existing file-events WS for the project root. `S`
- [ ] **Per-file conflict actions in the merge flow** — reuse `git-diff.js` hunk APIs for Accept Ours /
      Theirs / Both. `M`
- [ ] **Commit → amend, and rebase (onto + abort)** — the two remaining vs-VS-Code SCM gaps. `M`
- [ ] **Multi-cursor / replace-all** — `Ctrl+D` (next occurrence), `Ctrl+Shift+L` (all). `S`
- [ ] **Indent settings** — make the "Spaces: 4" status-bar item a picker; optionally honour `.editorconfig`. `S`
- [ ] **Save As (`Ctrl+Shift+S`)** — copy to a new path via `PUT /api/files/content`. `S`
- [ ] **Large-file read-only mode** — 5 MB hard wall today; allow read-only up to ~20 MB for logs. `S`
- [ ] **Binary/image preview** — render images instead of the 🚫 screen. `S`
- [ ] **Vendor CodeMirror/xterm/marked/DOMPurify locally** — an offline NAS currently breaks the editor
      and terminal; keep CDN as fallback. `S–M`
- [ ] **Split `editor.js`** → folded into **P3-04**.

### From the File Manager backlog

- [ ] **Move file logic into `services/`** → **P2-03**; **split `files.js`** → **P3-06**.
- [ ] **Owner + permissions columns** in list view (CSS classes exist). `S`
- [ ] **Transfer panel** for queued/running background ops (copy/move/delete are polled per operation). `M`
- [ ] **Undo for move/rename** (Trash only covers delete). `M`
- [ ] **Per-tab sort/view preferences** (currently per-window, reset on reopen). `S`
- [ ] **Chunked-upload TTL** → **P1-11**; **upload name conflict** → **P1-11**.
- [ ] **On-demand directory sizes** (`/api/files/stat` returns `item_count`, not bytes). `S`
- [x] **Bulk trash as one background op** instead of N requests — shipped (`POST /api/files/trash/batch` + op polling, `DELETE /api/files/trash/items`, `DELETE /api/files/trash/all`; `d71a1ec`, `docs/file-manager.md` §5).
- [ ] **Thumbnail cache eviction** → **P0-11**.
- [ ] **Breadcrumb/toolbar collapse on narrow windows** → **P3-13**.

---


# §10 · Package QA follow-ups

Source: `docs/packages-qa.md` (sweep 2026-11-22, 25 packages @ `db6df8f`, core 2.28.5).
Tools: `scripts/ethos-qa static|api`, `tests/test_packages_qa_ui.py`.

### ☐ Q-01 · Port collisions with host services — `S`
`ethos-monitor` 3001 (wawalocal-user), `ethos-rss` 8080 (wawalocal-api), `vmm` console 8094
(ethos-nas-site). 2.28.5 remaps a compose package to the next free port, so the install works but
the documented port is not what users get; venv/native services still fail. Move them outside the
used range and keep the "known in-use ports" list in `ethos-packages/AGENTS.md` current.
`ethos-git` 3000 → 3005 (`2186e5b`).

### ☐ Q-02 · Music and Books surfaces are Polish only — `L`
`ethos-music` (185 non-English chars) plus the core PWAs `/music` (`music.js` 94 lines) and `/books`
(`books.js` 52, `books.html` 18, `offline.js` 14). `ethos-books` (the package) is already English.
One decision — translate, or add a locale switch — applied per surface. `S18` in `ethos-qa` flags
the packages meanwhile.

### ☑ Q-03 · Stable test hooks on native windows — `S`
Measured with the rule from `package-ui/SKILL.md` (a `data-testid` *or* a stable action attribute):
`backup-manager` 8/8, `ethos-antivirus` 19/22, `vmm` 13/13, only `ethos-music` 0/6. Fixed in
`ethos-music` 1.0.3 (`db6df8f`): `em-nav-N`, `em-mini-*`, `em-fp-*`. Sweep and `ethos-qa` share the
same hook definition now.

### ☐ Q-04 · Move the Code Editor stylesheet out of the core — `L`
`ethos-editor` ships no CSS: 406 `.ed-*` rules live in `frontend/index.html` (206 KB → blocks
P3-10) and the module uses 80 literal colours. Tokenise into the module, then delete from the core.
Overlaps P3-04.

### ☐ Q-05 · Package changelog fields grow without bound — `S`
`vmm` 3.3 KB, `download-manager` 3.5 KB, `ethos-video` 2.7 KB in `index.json`, newest entry buried.
Cap the newest entry at one line; keep history in each package README. 21 descriptions also exceed
the 150 chars the catalog table shows (`S17`).

### ☐ Q-06 · Theme vocabulary drift — `M`
`ethos-cleaner`, `ethos-git-diff`, `ethos-kanban`, `ethos-photos` use the core's `--text-1/--surface-*`
variables; `ethos-editor` uses literals. All follow light/dark, but a redesign has to learn a new
vocabulary per app. Fold into the package-ui skill as an explicit "either/or" or migrate to `--pkg-*`.

### ☐ Q-07 · Publish catalog screenshots — `S`
Every manifest ships `screenshots: []`. The QA sweep already captures every window in both themes;
host a few per package and fill the field.
