---
name: security-invariant-review
description: "Audit backend changes for security invariants. Use for routers, services, paths, WebSockets, public endpoints, auth; checks admin, path scope, allowlist, credential logging."
argument-hint: "Which change or diff should be audited?"
---

# Security invariant review

Run this after implementing and before reporting a backend change as done. It is
diff-scoped: audit what changed, not the whole repo.

## Procedure

### 1. Scope the diff
```bash
git status --short
git diff --stat
git diff -U0 -- backend/ frontend/static/
```

### 2. Route-by-route questions
For every new/changed endpoint or WebSocket:

| Question | Required answer |
|---|---|
| Is it privileged (system, processes, docker, storage, packages, cron, backup, logs, editor, firewall, terminal)? | Router- or route-level `Depends(require_admin)` / `require_role(...)` |
| Does it accept a filesystem path? | Router mounts `Depends(scope_request)` **and** every path goes through `sanitize()` |
| Is it public? | Exact path in the `public_paths` set — never a prefix; internal auth documented if it is a deliberate prefix |
| Does it stream over WS? | Token validated before `accept`; close codes 4001/4002/4003/1013; role checked |
| Does it run a subprocess? | Argument list only, never `shell=True` |
| Does it write state? | `services/json_store.py`, not `json.dump` / `write_text` |

### 3. Grep the changed files for hazards
```bash
git diff -U0 -- backend/ | grep -nE \
  'except (Exception|BaseException): *pass|json\.dump\(|os\.getenv|allow_origins|shell=True|\.write_text\('
git diff -U0 -- backend/ | grep -niE \
  'logger\.(debug|info|warning|error).*(password|passwd|token|secret|api_key)'
```
- `except ...: pass` is allowed only with an explicit comment explaining why the failure is irrelevant.
- `os.getenv` belongs in `backend/config.py` only.
- Credentials must never reach a log line or an API error message (redact RTSP URLs, login bodies).

### 4. Configuration checks
CORS through `settings.ALLOWED_ORIGINS` (wildcard only in `DEBUG`), no new default `ETHOS_JWT_SECRET`, and no absolute paths in public 401/404 bodies.

### 5. Run the guards
```bash
.venv/bin/python scripts/check_logging.py
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python tests/test_security_invariants.py
.venv/bin/python tests/test_logging_convention.py
```

### 6. Live probes (isolated instance)
Using the `isolated-dev-instance` skill:
- no token → **401** on every new non-public route
- `user`-role token on privileged routes → **403**
- `?path=/etc/passwd` (and `/etc/../etc/passwd`) with a `user` token → **403**
- public endpoints return no filesystem paths in bodies

## Fail = fix before reporting
Any "no" in the table above is a blocker, not a follow-up. If an invariant cannot be met,
document the exception in `docs/api-conventions.md` or the relevant `docs/<feature>.md`.

## Done when
The checklist is fully green, guard scripts pass, and the live probes return the expected codes.
Report the checks you ran and their results.
