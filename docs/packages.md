# Packages — spec & publishing guide

A NAS-style add-on system: signed static repositories, compose-only packages, job-based
install/update/uninstall, and a catalog UI with trust indicators.

Verified against the code on 2026-11-12.

## File map

| What | Where |
|---|---|
| Router (`/api/packages`) | `backend/routers/packages.py` |
| Schema/version rules | `backend/services/package_schema.py` |
| Signed repository fetch/verify | `backend/services/package_repo.py` |
| Compose policy | `backend/services/compose_policy.py` |
| Job registry | `backend/services/package_jobs.py` |
| Installer/state machine | `backend/services/package_installer.py` |
| Offline wheelhouse | `backend/services/package_wheels.py` |
| venv runtime + supervisor | `backend/services/package_runtime.py` |
| Versioned stores + migration | `backend/services/packages_store.py` |
| Frontend app | `frontend/static/apps/packages.js` |
| Repo tooling | `scripts/ethos-pack` |
| Tests | `tests/test_package_logic.py`, `test_package_repo.py`, `test_pack_cli.py`, `test_packages_api.py`, `test_package_runtime.py` |

## Hard rules

- **Compose-only for new packages.** Lifecycle scripts (`preinst`/`postinst`/`prerm`/`postrm`) are
  rejected for installs. Use compose plus declarative `files[]`.
- **Three runtimes.** `runtime: "compose"` deploys a Docker Compose project;
  `runtime: "venv"` builds a private virtualenv inside the package directory from an **offline
  wheelhouse**; `runtime: "files"` is a declarative bundle with no service at all (a native UI or
  assets). The runtime is inferred when omitted: compose file → `compose`, otherwise `files`.
  The core venv is never modified and no package ever reaches PyPI at install time.
- **The compose project is the package id.** The installer runs `docker compose -p <id>`, so the
  stack is listed in the Docker app's **System** section with the `ethos-` display prefix
  (`domains` → `ethos-domains`, already-prefixed ids like `ethos-antivirus` unchanged).
  The prefix is display-only — Up/Down/Destroy still use the real project name.
- Every remote URL is HTTPS (loopback HTTP is allowed only in isolated tests via
  `ETHOS_PACKAGES_ALLOW_INSECURE`/`ETHOS_PACKAGES_ALLOW_PRIVATE`).
- A **pinned** source must have an Ed25519 public key; unsigned sources may be added only as
  `trust: "unverified"`.
- The trust chain is: index signature → `manifest_sha256` → compose/files sha256.
- Package data lives under `<DATA_DIR>/packages/apps/<id>/`; reserved ids (`packages`, `trash`,
  `snapshots`, …) are rejected.
- Install/update/uninstall/source mutations are admin-only.

## Repository layout

```
repo/
├── index.json
├── index.json.sig            # detached Ed25519 signature over the raw index bytes
└── <package-id>/
    ├── manifest.json
    ├── docker-compose.yml    # optional if the package is files-only
    └── ...files referenced by manifest.files[]
```

## `index.json` (schema 2)

```json
{
  "schema": 2,
  "name": "Community packages",
  "publisher": {"name": "SyncHot", "key_id": "ab12cd34"},
  "generated_at": "2026-11-12T12:00:00+00:00",
  "packages": [
    {
      "id": "uptime-kuma",
      "name": "Uptime Kuma",
      "version": "1.0.0",
      "description": "Uptime monitor",
      "icon": "📈",
      "icon_url": null,
      "category": "monitoring",
      "author": "SyncHot",
      "homepage": "https://github.com/louislam/uptime-kuma",
      "license": "MIT",
      "channel": "stable",
      "requires": [],
      "screenshots": [],
      "changelog": "Initial release",
      "manifest_url": "https://…/uptime-kuma/manifest.json",
      "manifest_sha256": "64 hex chars",
      "size_bytes": 12345
    }
  ]
}
```

## `manifest.json` (schema 2)

```json
{
  "schema": 2,
  "id": "uptime-kuma",
  "name": "Uptime Kuma",
  "version": "1.0.0",
  "description": "Uptime monitor",
  "icon": "📈",
  "category": "monitoring",
  "author": "SyncHot",
  "license": "MIT",
  "compose": {"url": "https://…/docker-compose.yml", "sha256": "64 hex chars"},
  "files": [
    {"url": "https://…/config.example.json", "sha256": "64 hex chars", "path": "config.example.json", "mode": 420}
  ],
  "ui": {"scheme": "http", "port": 3001, "path": "/"},
  "healthcheck": {"scheme": "http", "port": 3001, "path": "/"},
  "requires": [],
  "conflicts": [],
  "capabilities": ["compose"],
  "system_requires": []
}
```

- `compose` may be omitted only if `files` is non-empty.
- `files[].path` is relative and must stay inside the package directory.
- `ui` enables the “Open UI” action; `healthcheck` is reserved for a later probe.
- Two UI modes:
  - `{"mode": "window", "scheme": "http", "port": 3001, "path": "/"}` — the
    standalone service is embedded in an EthOS window (iframe). The port must be reachable.
  - `{"mode": "native", "entry": "frontend/app.js"}` — the entry module is loaded
    directly into the EthOS window (no iframe, no required port). `entry` must be listed in
    `files[]` and export `render(container, ctx)`. The module must be self-contained (the loader
    imports it as a blob module, so relative imports are not available). Available context:
    `ctx.pkg`, `ctx.apiFetch`, `ctx.apiJsonFetch`, `ctx.notify`, `ctx.notifications`,
    `ctx.log(level, message, detail)` / `ctx.logger.{debug,info,warn,error}`, `ctx.proxy(path, options)`,
    `ctx.pickFile(opts)`, `ctx.pickFolder(opts)`, `ctx.reload()`, plus the shared UI helpers
    `ctx.progress.startTask`, `ctx.modal.{confirm,alert,form}` and `ctx.escape.{html,attr,js}`.
    Using those keeps native packages on the same progress panel / accessible modals as core apps.
    `ctx.notifications` is the **only** allowed notification surface — `toast(message, kind)`,
    `emit({kind, title, message, action})` (persisted, `source="pkg:<id>"`) and `open(filter)`.
    A package-local toast/panel/badge/history is a review blocker
    (see `docs/notifications.md`).
    `ctx.log` is the only logging surface: the server verifies `pkg.id` and stores the row as
    `app="pkg:<id>"` in the unified Logs app (`docs/logs.md`). Package **backend** logs go to
    stdout/stderr and appear in the Logs app grouped by the compose project (= package id). A
    package-local log viewer, history list or log file is a review blocker.
    The pickers accept
    `{title, startPath, roots, acceptLabel}`; `roots` restricts browsing to those absolute paths
    (e.g. `['/home','/mnt','/media']`) and cannot be escaped via parent navigation.
  - Native modules require a **pinned, signed source**; their code runs in the EthOS origin, so only
    install them from publishers you trust. A package that needs a service sets `ui.port`; calls go
    through the authenticated `/proxy/` endpoint (GET, POST, PUT, PATCH and DELETE are forwarded),
    so the service can stay bound to loopback.
  - **Window shell.** Native UIs reuse the EthOS AntiVirus layout and tokens — 218 px rail with a
    text-only nav, panel/table/summary building blocks, one status line and the shared
    `ctx.progress`/`ctx.modal` helpers. The exact rules live in `ethos-packages/AGENTS.md` →
    “Native package UI shell (mandatory)”. Do not ship a one-off visual language.
  - **Both themes are mandatory.** Native modules follow `<html class="light|dark">` (and any runtime
    toggle); window/port UIs get `?theme=light|dark` appended to the iframe URL (also used by
    “Open in new tab”) and are reloaded when the EthOS toggle changes, falling back to
    `prefers-color-scheme`. See the packages repo's `.github/skills/package-theme/SKILL.md`.
  - **The window header and taskbar use the manifest identity.** EthOS renders `manifest.name` and
    `manifest.icon_url` in the package window title and taskbar entry (image with the emoji as
    fallback). `name` must be the human-readable product name, never the package id; native modules
    must not override the window title themselves.
- `icon_url` is optional; when present it is used by Packages **and** the desktop / App Drawer
  (the public `/api/packages/desktop-icons` endpoint exposes only absolute HTTPS URLs). `icon`
  (emoji) remains the fallback when no image is set.
- A non-null `scripts` object is rejected with “Lifecycle scripts are not supported for new installs”.

## Runtimes: compose, venv and files

A package declares one runtime; it is inferred when omitted (compose file → `compose`, otherwise
`files`). `compose` is the classic behaviour. `venv` exists so that dependency-heavy apps
(Music's yt-dlp engine, face-aware thumbnails with OpenCV, …) can leave the core `requirements.txt`
and install their own Python stack only when the package is installed. `files` is a bundle with no
service: a native UI that calls the core API (Photos, Music, Videos, Cameras, Kanban's
sibling apps) or plain assets. A `files` package reports status `ready` and Packages hides the
start/stop buttons. Background and the core/package boundary: `docs/app-store.md`.

```json
{
  "runtime": "venv",
  "files": [
    {"url": "https://…/server/main.py", "sha256": "64 hex", "path": "server/main.py"},
    {"url": "https://…/frontend/app.js", "sha256": "64 hex", "path": "frontend/app.js"}
  ],
  "ui": {"mode": "native", "entry": "frontend/app.js"},
  "healthcheck": {"scheme": "http", "port": 8096, "path": "/health"},
  "python": {
    "entry": "server/main.py",
    "port": 8096,
    "requires_python": "3.14",
    "requirements": ["yt-dlp==2026.10.26"],
    "wheels": [
      {"url": "https://files.pythonhosted.org/…/yt_dlp-2026.10.26-py3-none-any.whl",
       "sha256": "64 hex", "arch": "any", "filename": "yt_dlp-2026.10.26-py3-none-any.whl"}
    ]
  }
}
```

An **engine package** (a service the core calls but that has no window) omits `ui` entirely —
it then gets no desktop icon. `ethos-music-engine` and `ethos-media-enhance` are engines.

Rules enforced by `package_schema.validate_manifest`:

- `runtime: "venv"` requires `python` with a `port` (the loopback service port); it may not
  declare `compose`. A `python` block on a compose package is rejected. `ui` is optional.
- `python.entry` must be listed in `files[]` (same rule as a native UI entry).
- `python.requirements` are plain `name` / `name<op>version` strings: no URLs, no `@`, no
  whitespace, no pip options, at most 100 entries. `requires_python` (e.g. `3.14`) is enforced
  before the venv is built.
- `python.wheels` must be non-empty, hash-pinned, and carry `arch` `x86_64`, `aarch64` or `any`.
  `any` is accepted only for a `-none-any.whl`. Sdists and bare filenames are rejected.
  `url` may point at an upstream file (PyPI CDN); the signed manifest's `sha256` is the
  integrity guarantee, so wheels do not have to be copied into this repository.
- `min_ethos_version` is enforced at install time (409 when the core is older).
- `requires` is installed first (depth-first, cycle-checked); `conflicts` blocks the install when
the named package is already present, and uninstalling a package that others require is refused.

Install/update/uninstall semantics:

1. Job steps for a venv package are `verify → download → policy → wheels → deps → deploy →
   venv → start → done` (update inserts `backup` before `venv`).
2. `package_wheels.stage` downloads every wheel for this machine's architecture into
   `<pkg>/.wheelhouse/` and verifies its sha256.
3. `package_runtime.build` creates `<pkg>/venv` and runs pip with
   `--no-index --find-links=<pkg>/.wheelhouse --only-binary :all:`; `PIP_CONFIG_FILE` is
   `os.devnull` and no index is reachable. The venv is always built in its **final** path — pip
   writes absolute paths that would break after the staging move.
4. `package_runtime.start` launches `<venv>/bin/python <python.entry>` as the service user with
   `ETHOS_PACKAGE_ID`, `ETHOS_PACKAGE_PORT`, `ETHOS_PACKAGE_DIR` and
   `ETHOS_PACKAGE_DATA_DIR` (`<DATA_DIR>/packages/data/<id>`). stdout/stderr stream into the
   unified log store as `pkg:<id>`; the supervisor restarts a crashed service at most 3 times in
   10 minutes, then raises one `error` notification. The core reaches it through
   `services/package_client.py` (loopback only, `python.port`).
5. Status/start/stop/restart go through `GET /api/packages/{id}/status` and
   `POST /api/packages/{id}/action` exactly like compose packages; installed services are
   restarted on boot by `package_runtime.autostart_all()`.

## Capabilities

`capabilities` is a list of manifest strings, defaulting to `["compose"]`. Unknown values are
rejected at manifest validation, so a typo cannot silently disable a policy gate. The index entry
carries the same list (`ethos-pack build` copies it) and Packages shows a badge for it.

| Capability | Unlocks |
|---|---|
| `compose` | implied by shipping a compose file; nothing extra |
| `kvm` | the **only** `devices:` exception: `/dev/kvm` (optionally `"/dev/kvm:/dev/kvm:rwm"`) |

`compose_policy.lint_compose(text, capabilities)` keeps rejecting every other device. Without the
capability the install fails with “device /dev/kvm requires capabilities: ["kvm"] in the manifest”.
`/dev/kvm` is itself a capability probe: `GET /api/system/status` reports
`virtualization: {"kvm": true|false, "device": "/dev/kvm"}`, and a package must degrade gracefully
(for example QEMU `-accel tcg`) when the host has none.

## System requirements

`system_requires` declares **host software** a package cannot ship itself. Allowed values are the
registry ids in `backend/services/system_deps.py`; anything else is rejected at manifest validation.

| id | Binaries probed | apt package | Lifecycle |
|---|---|---|---|
| `docker` | `docker` | `docker.io` | Installed on demand through `ethos-docker-install`; **never** auto-removed (shared by every compose package) |
| `samba` | `smbd`, `testparm`, `smbpasswd` | `samba` | Installed with the package through `ethos-system-deps`; removed again when the last package that needs it goes away |
| `archive-tools` | `bsdtar` | `libarchive-tools` | **Optional**: installs `bsdtar` (`.7z`, `.rar`, `.zst`, `.lz4`) for File Manager extraction; a missing tool never fails an install/update, and the ownership is released like Samba's |

```json
{ "system_requires": ["samba"] }
```

**Host software follows the package.** Two root-owned helpers do the privileged work; neither takes
an arbitrary package name:

| Helper | Interface | sudoers |
|---|---|---|
| `/usr/local/bin/ethos-docker-install` | argument-free | one rule |
| `/usr/local/bin/ethos-system-deps` | `<install\|remove> <id>` | one exact rule per (action, id) |

- `ensure()` (install/update, `deps` step) probes first. A satisfied requirement does nothing; a
  missing serviceable one is installed through the helper and recorded in
  `<DATA_DIR>/packages/system_deps.json` as **EthOS-owned** with the package that pulled it in.
- `release()` (uninstall, `sysdeps` step) removes an EthOS-owned requirement only when **no other
  installed package declares it** and the registry marks it `autoremove`. Software the operator
  installed by hand is never touched — it is not in the ownership store.
- Registry flags per id: `serviceable` (EthOS may install/remove), `autoremove` (may be removed on
  uninstall), `provisionable` (dedicated helper — Docker), `optional` (recommended extra — a missing
  tool is logged instead of failing the install; the helper is still used when it is available).
- Optional example: `download-manager` declares `system_requires: ["archive-tools"]`. Without the
  helper/sudoers rule the package still installs, File Manager simply keeps reporting that no
  extractor is installed until the operator installs `libarchive-tools`.
- Probe uses absolute candidates (`/usr/sbin`, `/usr/bin`, …) before `shutil.which`, because the
  service account's `PATH` usually lacks `/usr/sbin`.
- Without the helper (host installed before this feature), the job fails with HTTP 503 telling the
  operator to re-run `sudo bash setup_scripts/setup_service.sh` or to run the `apt` command shown.
- `GET /api/packages/catalog/{id}` returns `system_status`: `[{id, label, ok, missing_tools, hint,
  provisionable, serviceable, autoremove, owned}]`, so Packages warns **before** the click. The
  detail dialog shows a red “Cannot install yet” box with the command and disables Install/Update
  only for requirements EthOS cannot provision.
- Cards show one badge per requirement; Samba Shares is the reference package. Core features that
  need the same tool reuse the probe (`shares_service.require_samba()`), so the app and the catalog
  can never disagree.

## Official source on fresh installs

If no sources are configured, startup seeds one **pinned** source so a new EthOS install can browse
the public catalog without copying a key:

| Setting | Default |
|---|---|
| `ETHOS_PACKAGES_DEFAULT_SOURCE` | `true` |
| `ETHOS_PACKAGES_DEFAULT_SOURCE_NAME` | `EthOS Community Packages` |
| `ETHOS_PACKAGES_DEFAULT_SOURCE_URL` | `https://raw.githubusercontent.com/SyncHot/ethos-packages/main/index.json` |
| `ETHOS_PACKAGES_DEFAULT_SOURCE_KEY` | the publisher's base64 Ed25519 public key |

The key is a **trust anchor from EthOS configuration**, never downloaded from the repository —
otherwise a compromised repository could replace both the index and its key. Existing installs with
any source are left untouched; edit that source (Sources → Edit → Signed + key) instead. Tests set
`ETHOS_PACKAGES_DEFAULT_SOURCE=0` so they never depend on GitHub.

## Signing and publishing

```bash
# 1. create a repo key (private key stays out of git)
./scripts/ethos-pack keygen --out-dir ~/.ethos-pack-keys

# 2. build manifests/index with absolute URLs and hashes
./scripts/ethos-pack build \
  --repo ~/apps/compose/ethos-nas-packages \
  --base-url https://raw.githubusercontent.com/SyncHot/ethos-nas-packages/main

# 3. sign the raw index bytes
./scripts/ethos-pack sign \
  --repo ~/apps/compose/ethos-nas-packages \
  --key ~/.ethos-pack-keys/repo.key

# 4. verify locally (and in CI)
./scripts/ethos-pack verify \
  --repo ~/apps/compose/ethos-nas-packages \
  --pub ~/.ethos-pack-keys/repo.pub
```

`build` accepts a package manifest whose `compose`/`files` entries are local paths; it rewrites them
to URLs with sha256 hashes and writes `manifest_sha256` into the index. Commit `index.json`, the
manifests and `index.json.sig`; never commit `repo.key`.

## Install layout and state

| Path | Content |
|---|---|
| `<DATA_DIR>/packages/installed.json` | versioned installed records |
| `<DATA_DIR>/packages/sources.json` | source config: url/name/public_key/key_id/trust/enabled |
| `<DATA_DIR>/packages/cache.json` | per-source index cache (TTL 15 min) |
| `<DATA_DIR>/packages/apps/<id>/` | deployed compose + files + `ethos-package.json` |
| `<DATA_DIR>/packages/.staging/` | verified downloads before the swap |
| `<DATA_DIR>/packages/.backup/` | previous version during an update |

Startup migration (`packages_store.migrate_legacy`) converts the old bare-list stores and moves
`<DATA_DIR>/<id>` to `packages/apps/<id>`. Package state is included in config export/import.

## Uninstall semantics

`DELETE /api/packages/{id}` always stops and removes the containers, the compose network and the
package directory. Two opt-in flags extend it; both default to **off** so a reinstall resumes with the
package's state (scan logs, certificates, settings):

| Flag | Effect |
|---|---|
| `remove_volumes=1` | `docker compose down --volumes` — deletes the named volumes declared by *this* package's compose file |
| `remove_images=1` | `docker rmi` for the images the compose file references, but only while no container uses them |

Removal is deliberately scoped: there is no `docker image prune` / `docker volume prune` anywhere in
the flow, because those act on the whole host and would delete unrelated stacks. Images still used by
another container are reported in `shared_images[].in_use_by` (container + compose project) and kept;
the UI greys them out with the owning package name.

`GET /api/packages/{id}/uninstall-preview` returns
`{containers, volumes, images[], shared_images[], has_compose, docker}` so the confirmation dialog can
show exactly what would go. It is read-only and degrades to empty lists with `docker: false` when the
Docker CLI is unavailable.

Job steps for an uninstall are `stop → images (only when requested) → cleanup → done`; image results and
skipped shared images are written to the job log.

## Compose policy

Rejected before deploy: `privileged`, `network_mode/pid/ipc: host`, `cap_add`, `security_opt`,
`devices`, `volumes_from`, `build`, `extends`, `include`, `docker.sock`, and `env_file` paths
outside the package directory. Host binds are limited to `/home`, `/mnt`, `/media` and the
package's **own** package/data root; every other path inside the package store (another
package, `installed.json`, `sources.json`) is rejected even when `DATA_DIR` itself lives under
`/home`. Unpinned images (`:latest` or no tag/digest) produce a warning.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/packages` | installed records (`packages` + `items`) |
| GET | `/api/packages/desktop-icons` | public icon list |
| GET/POST/PATCH/DELETE | `/api/packages/registry/sources…` | source CRUD (mutations admin) |
| GET | `/api/packages/registry/browse` | merged catalog (`q`, `category`, `channel`) |
| POST | `/api/packages/registry/refresh` | force re-fetch |
| GET | `/api/packages/catalog/{id}` | entry + verified manifest |
| GET | `/api/packages/updates` | packages with an update |
| POST | `/api/packages/install` | `{package_id, source?}` → `{job_id}` |
| GET | `/api/packages/jobs[/{id}]` | job status, steps and log |
| POST | `/api/packages/{id}/update` | `{job_id}` |
| DELETE | `/api/packages/{id}` | `{job_id}`; `?remove_volumes=1&remove_images=1` |
| POST | `/api/packages/{id}/action` | start/stop/restart |
| GET | `/api/packages/{id}/status` | compose status |
| GET | `/api/packages/{id}/uninstall-preview` | admin: volumes, images and containers an uninstall can remove |
| GET | `/api/packages/{id}/ui/{path}` | authenticated native UI asset (declared files only) |
| GET/POST/PUT/PATCH/DELETE | `/api/packages/{id}/proxy/{path}` | authenticated proxy to the package `ui.port` (loopback); admin-only unless `user_access` |

Job payloads carry `progress` (0–100), `step_index`/`step_total` and the `steps[]` history so clients
can render a determinate bar. Each step owns a slice of the bar (`verify` 0–17 %, `download` 17–33 %,
…) and the `download` slice is interpolated per verified file. Package downloads and manifest fetches
run via `asyncio.to_thread`, so `POST /install` returns the job id immediately and progress polls stay
responsive. The Packages UI shows a live bar with the step label and last log line, disables
conflicting actions while a job runs, and re-attaches to a queued/running job when the window reopens.
**Update all** (toolbar, admin-only, visible only while updates are pending) walks the pending list
sequentially through `POST /api/packages/{id}/update`, so a failing update cannot block the rest —
each package keeps its own rollback and the button reports `n/m` progress.

## Legacy compatibility

- v1 packages without scripts still install from an **unverified** source (no manifest hash/signature).
- v1 packages with non-null scripts are rejected; port them to `files[]` + compose.
- The old `registry_sources.json`/`registry_cache.json` are migrated on first start.

## Limits

`ETHOS_PACKAGES_MAX_INDEX_BYTES` (5 MB), `ETHOS_PACKAGES_MAX_MANIFEST_BYTES` (2 MB),
`ETHOS_PACKAGES_MAX_ARTIFACT_BYTES` (256 MB), `ETHOS_PACKAGES_TTL_SECONDS` (900).
`ETHOS_PACKAGES_ALLOW_PRIVATE` enables LAN repos; `ETHOS_PACKAGES_ALLOW_INSECURE` is test-only.

## Status

- [x] Schema v2, reserved ids, scripts rejection
- [x] Ed25519 index signatures + sha256 chain
- [x] Versioned stores + startup migration
- [x] Compose policy
- [x] Jobs (install/update/uninstall) with rollback
- [x] Catalog/detail/sources/updates UI
- [x] Install/uninstall progress bar with step, percentage and log line
- [x] "Update all" — sequential updates with per-package rollback
- [x] Native UI modules + authenticated loopback proxy
- [x] Optional volume/image removal on uninstall
- [x] Unit, CLI, signed-repo and API tests
- [x] `system_requires` (Docker Engine) resolved at install time
- [x] `system_requires` registry: host binaries probed before deploy, installed with the package
      through a root-owned helper (one sudoers rule per (action, id)), and removed on uninstall when
      no installed package needs them any more (ownership store + refcount, never touches software
      the operator installed by hand)
- [x] `runtime: "venv"` — offline wheelhouse, private per-package venv, supervised loopback process
- [x] `min_ethos_version` enforced for new installs
- [x] Dependency resolver: `requires` installed first, `conflicts` and dependent-uninstall blocked
- [x] Core extraction: `ethos-music-engine` (yt-dlp) and `ethos-media-enhance` (OpenCV) published
- [ ] Sign and publish `ethos-nas-packages` (needs the private key)
- [ ] Port `vm-manager` to declarative `files[]` and publish

## Deferred

- Key rotation / multiple keys per source.
- Hosted upload server and local repo UI.
- Container image builds on the NAS, and installing OS packages for a package (`apt` stays out of
  scope on purpose: `system_requires` only probes and reports; only Docker is provisionable).
- Wheel deduplication across packages (each venv keeps its own copy by design).
- systemd transient units instead of the in-process supervisor (packages would survive core restarts).
