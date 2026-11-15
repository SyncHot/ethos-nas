---
name: add-package
description: "Create or update an EthOS Packages package: manifest, compose, signed repo, install/update tests. Use for new installable apps, package updates, signing, or broken package sources."
argument-hint: "Which package or app are you adding/updating?"
---

# Add or update a Packages package

## Model in one screen
- A package is a **signed static repository entry**, not a native `.spk`/`.qpkg`. There are no
  lifecycle scripts (install scripts are rejected by design).
- It ships a `manifest.json` (schema 2), a `docker-compose.yml`, and any files the app needs.
- `compose` may be omitted only for files-only packages. A UI can either expose one port
  (`ui.mode: "window"`) or ship a module loaded into the EthOS window (`ui.mode: "native"`,
  no iframe, no required port).
- Repositories are Ed25519-signed: `index.json` + `index.json.sig`; every manifest, compose file and
  `files[]` entry has a `sha256`.
- Canonical spec and API: `docs/packages.md`. Package code lives in a **separate repo** (for example
  `~/apps/compose/ethos-packages`), never inside `backend/`.

## Procedure

### 1. Scaffold the package directory
```text
<repo>/<package-id>/
├── manifest.json
├── docker-compose.yml       # optional for files-only
├── server/                  # app code shipped via files[]
│   ├── app.py
│   └── static/
└── site/                    # static-only apps
```

Use a lowercase id matching `^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$`; reserved ids include `packages`,
`trash`, `snapshots`, `thumbnails`, `temp_chunks`, `wallpapers`, `git`, `models`, `temp`, `users`,
`desktop_layouts`.

### 2. Write `manifest.json`
Minimum fields: `schema: 2`, `id`, `name`, `version`, `description`, `icon`, `category`, `author`.
Add `compose`, `files`, `ui`, `healthcheck`, `license`, `homepage`, `changelog` when relevant.

```json
{
  "schema": 2,
  "id": "my-app",
  "name": "My App",
  "version": "1.0.0",
  "description": "One sentence for the catalog card.",
  "icon": "🧩",
  "category": "tools",
  "author": "SyncHot",
  "license": "MIT",
  "compose": "docker-compose.yml",
  "files": ["server/app.py", "server/static/index.html"],
  "ui": {"scheme": "http", "port": 8099, "path": "/"},
  "healthcheck": {"scheme": "http", "port": 8099, "path": "/api/health"}
}
```

Native UI variant (no iframe, no exposed port; signed source required):

```json
{
  "ui": {"mode": "native", "entry": "frontend/app.js"},
  "files": [{"path": "frontend/app.js", "...": "..."}]
}
```

The entry must be listed in `files[]` and export `render(container, ctx)`. The loader imports it as
an authenticated blob module: keep it **self-contained** (no relative imports), inline styles, and
use `ctx.pkg`, `ctx.apiFetch`, `ctx.apiJsonFetch`, `ctx.notify`, `ctx.notifications`,
`ctx.proxy(path, options)` and `ctx.reload()`.

**Notifications are mandatory and global** (`docs/notifications.md`): user feedback goes through
`ctx.notifications.toast(message, kind)` and persisted events through
`ctx.notifications.emit({kind, title, message, action})`; `ctx.notifications.open()` focuses the
sheet. Never ship a package-local toast, notification panel, unread badge or event list —
`tests/test_notification_ui.py` enforces the same rule for core apps. `ctx.notify` remains a
legacy toast-only alias.

The native module runs in the EthOS origin, so only allow it from a **pinned, signed** source. If it
needs a container service, set `ui.port` and call it through `ctx.proxy('api/...')`; the service
should bind `127.0.0.1:<port>` only and the authenticated `/proxy/` endpoint forwards to it.

`files[].path` must be relative and stay inside the package directory. Bump `version` for every
published change; the catalog uses it for update detection.

### 3. Write `docker-compose.yml`
- Pin an image tag (`nginx:alpine`, `clamav/clamav:stable`); avoid `:latest`.
- Publish exactly one UI port; do not expose internal services (ClamD, databases).
- Use named volumes for state; never write to host paths.
- Read-only host binds are allowed only under `/home`, `/mnt`, `/media` or the package/data roots.
- Health endpoint in the app (`/api/health`) and, if useful, a Compose `healthcheck`.
- Use service DNS names inside the network (for example `CLAMD_HOST: clamav`).

Forbidden by `services/compose_policy.py`: `privileged`, host network/PID/IPC, `cap_add`,
`security_opt`, `devices`, `volumes_from`, `build`, `extends`, `include`, Docker socket binds,
host binds outside the allowlist, and `env_file` outside the package.

**Permissions:** containers run as their image user. To scan arbitrary NAS files with `0660`
permissions, a scanner may need root **inside** the container and must keep host mounts read-only.
Document that tradeoff. For the ClamAV image, replace `User clamav` in `clamd.conf` before `/init`:
```yaml
user: "0:0"
entrypoint: ["/bin/sh", "-c"]
command:
  - |
    sed -i 's/^User clamav$/User root/' /etc/clamav/clamd.conf
    exec /init
```
Do not add privileged or writable host mounts to compensate for permissions.

### 4. Build the app
- Keep it dependency-free when practical (`python:3.12-alpine` + stdlib); otherwise use a pinned
  image that already contains the runtime.
- Serve static assets from the same container. Never hardcode tokens/secrets in package files.
- For a native UI, ship one ES module and inline its CSS. Do not use relative imports; the module is
  imported from a blob URL. Test the module in an EthOS window (no iframe) and through `ctx.proxy`.
- Notification UI is **not** part of the package: use `ctx.notifications.{toast,emit,open}`. Any
  package-owned toast/panel/history/badge is a review blocker (it breaks the global bell contract and
  double-renders the unread count).
- Logging is **not** part of the package either: native modules call `ctx.log(level, message, detail)`
  (or `ctx.logger.{debug,info,warn,error}`), and backend logs go to stdout/stderr. The unified Logs app
  shows them tagged `pkg:<id>`; a package-owned log viewer, history list or log file is a review
  blocker (`docs/logs.md`, `tests/test_logging_ui.py`).
- Package UIs on a published port have **no EthOS login**. State that in the README; recommend a
  trusted LAN and do not expose the port to the internet. Prefer a loopback bind + `ctx.proxy` for
  native packages.
- Handle scanner/service startup gracefully: a `health` endpoint returning `503 starting` is better
  than a container crash loop.
- Validate every user path against an allowlist before passing it to a subprocess or socket.
- Use `textContent`/DOM nodes for untrusted strings in the package UI.

### 5. Build, sign, verify the repository
```bash
cd ~/apps/compose/ethos-nas
.venv/bin/python scripts/ethos-pack build \
  --repo ~/apps/compose/ethos-packages \
  --base-url https://raw.githubusercontent.com/SyncHot/ethos-packages/main
.venv/bin/python scripts/ethos-pack sign \
  --repo ~/apps/compose/ethos-packages --key ~/.ethos-pack/ethos-packages/repo.key
.venv/bin/python scripts/ethos-pack verify \
  --repo ~/apps/compose/ethos-packages --pub ~/apps/compose/ethos-packages/repo.pub
```
Commit `index.json`, `index.json.sig`, manifests, and files. Never commit `repo.key`; keep
`__pycache__/` and `*.pyc` ignored.

### 6. Test before publishing
1. Apply the real policy:
   ```bash
   .venv/bin/python - <<'PY'
   from pathlib import Path
   from backend.services.compose_policy import enforce_compose
   print(enforce_compose(Path(".../docker-compose.yml").read_text()) or "passed")
   PY
   ```
2. Start the Compose project directly and verify the UI, health endpoint and one real action.
3. Install through an **isolated** EthOS instance (`isolated-dev-instance`) with a pinned source,
   then verify `/api/packages/{id}/status`, the UI, and uninstall. Use a commit-pinned raw URL while
   the `main` raw cache is still stale:
   `https://raw.githubusercontent.com/SyncHot/<repo>/<sha>/index.json`.
4. For scanners, use the harmless EICAR test string in a temp folder under `/home`, verify
   `FOUND`/`Eicar-Signature`, then delete the test files and the test-only named volume.
5. Test the frontend entry points: Packages card, detail modal, Installed tab, refresh, and
   uninstall. A missing `DELETE` in the server log means the UI never sent the request.

### 7. Publish
```bash
git add -A && git commit -m "feat: add <package> package" && git push
```
Tell the user to add/refresh the source and use the pinned public key. GitHub raw can lag a few
minutes; `POST /api/packages/registry/refresh` bypasses the local TTL cache, not the CDN cache.

## Security checklist
- [ ] No lifecycle scripts, no `build`, no `privileged`, no Docker socket.
- [ ] Native UI only from a pinned source; entry is declared in `files[]` and self-contained.
- [ ] Native service ports bind loopback and are reached through `ctx.proxy`.
- [ ] Host binds read-only and inside `/home`, `/mnt`, `/media`.
- [ ] No secrets in manifests/files; no `env_file` outside the package.
- [ ] All user feedback uses `ctx.notifications` / `ctx.progress` — no package-local toast, panel,
      badge or event history.
- [ ] UI path/commands validated; no shell interpolation; argument lists only.
- [ ] State in named volumes; uninstall keeps volumes by design.
- [ ] Signed index, all hashes verified, private key outside Git.
