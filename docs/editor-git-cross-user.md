# Editor Git Integration — Cross-User Repos (T-0011)

Design decision + status for the editor's git integration failing on repos owned by
the login user. This file is the resume point; update the Status section as work lands.

## Problem (reproduced 2026-10-18 on :9000)

`GET /api/editor/git/status?repo=/srv/repos/example` →
`{"is_repo": false, ...}` on the production server — even though the path is a git
repository. Consequently status/stage/commit/log/push all no-op for user-owned repos.
A same-user dev instance (server run as the login user) works fine.

## Root cause (verified)

- The production server runs as the dedicated `ethos-nas` service user (uid 994).
- git ≥ 2.35.3 refuses to operate on a repository whose owner differs from the
  effective uid: `fatal: detected dubious ownership in repository at ...`
  (`_is_repo()` in `backend/routers/editor.py` runs `git rev-parse`; the failure
  makes it report `is_repo: false`).
- **OS-level access is NOT the problem.** The service user is a member of the repo
  owner's group, the repo tree is group-writable (dirs 2775, files 664, `.git` 2775),
  and the owner's home is 711 (traversable). Read *and* write are already possible;
  only git's ownership check blocks.
- Mechanism matrix (verified with `fakeroot` simulating uid 994 vs a repo owned by
  uid 1000, git 2.53.0):
  | Invocation | Result |
  |---|---|
  | `git -C repo status` (no config) | `fatal: detected dubious ownership` |
  | `git -c safe.directory=* -C repo status` | **works** (git 2.53 respects `-c`; older docs claim otherwise) |
  | global config file with `[safe] directory = *` | **works** |
  | global config file with `[safe] directory = /path` (per-repo) | **works** |

## Decision

**Permanent fix — app-level (recommended): the editor's git helpers pass
`safe.directory=*` for the service user.** One-line change in
`backend/routers/editor.py`: add `-c safe.directory=*` to the git invocations in
`_git()` and `_is_repo()`.

Rationale:
- The dubious-ownership check exists to protect *human* users from running git in
  malicious repos planted by other users. The `ethos-nas` service account is a
  machine identity the app already fully controls — the Terminal app runs
  authenticated users' commands as that same user, and the editor's file browser
  only exposes paths the service user can already access. Blanket trust for the
  service user therefore adds no meaningful new attack surface; the real boundary
  remains app authentication + POSIX permissions.
- No system config, no sudo, no per-repo bookkeeping. Works for every repo the
  service user can access — exactly the set of repos visible in the editor.
- Deployed with a normal production restart (standard for backend changes).

**Interim unblock (works today, no restart): `setup_scripts/enable_editor_git.sh`**
writes `safe.directory` entries into the service user's global gitconfig
(`/home/ethos-nas/.gitconfig`). Git reads that file on every invocation, so the
fix is immediate:

```
sudo bash setup_scripts/enable_editor_git.sh --all          # single-user NAS (fine here)
sudo bash setup_scripts/enable_editor_git.sh /path/to/repo  # per-repo, safer default
```

**Security notes**
- `safe.directory` only disables the ownership check; it grants no file access.
  All real access stays governed by POSIX permissions.
- Do **not** put `safe.directory` in `/etc/gitconfig` (system-wide) — that would
  also disable the check for the login user's own git. Keep it scoped to the
  service user.
- On a multi-user system, prefer per-repo entries over `--all`.

## Follow-up: remote-side ownership check (T-0015, 2026-10-19)

`git push`/`pull` to a **local-path remote** owned by the login user still failed
on the production server, even with `GIT_TRUST_ARGS` in place:

```
fatal: detected dubious ownership in repository at '/tmp/.../remote.git'
fatal: Could not read from remote repository. ... and the repository exists.
```

Reproduced on the live service user (via the terminal WS, git 2.53.0):

| Invocation (remote owned by uid 1000, git as uid 994) | Result |
|---|---|
| `git -c safe.directory=* push` | **fails** — remote-side check ignores `-c` |
| `GIT_CONFIG_COUNT=1 … safe.directory=* git push` | **fails** — env command-line config also ignored |
| `GIT_CONFIG_GLOBAL=<file with [safe] directory=*> git push` | **works** |

The `-c` flag covers the repository git is invoked in; when the local transport
opens the remote repo it re-runs the ownership check with a fresh config load
that excludes command-line options. `safe.directory` in a **global config file**
is respected on both sides.

Fix (app-level, `editor.py`): `_git_env()` writes `[safe] directory = *` to
`DATA_DIR/git/global-gitconfig` (service-owned, no $HOME needed) and every git
subprocess gets `GIT_CONFIG_GLOBAL` pointing at it. `GIT_TRUST_ARGS` is kept
(belt-and-braces for the main repo).

## Status

- [x] T-0015 (2026-10-19): remote-side dubious-ownership fixed via
      `GIT_CONFIG_GLOBAL` (`_git_env()` in `editor.py`); verified live: service
      user pushes to a marcin-owned bare remote succeed. `tests/test_editor.py`
      Part A now uses `service_writable_tmpdir` (server creates project dirs +
      pushes there).
- [x] Reproduced on :9000 (2026-10-18): `is_repo: false` for marcin-owned repo
- [x] Root cause + mechanism verified (fakeroot matrix above)
- [x] Interim script: `setup_scripts/enable_editor_git.sh` (idempotent, tested)
- [x] Regression test: `tests/test_editor_git_cross_user.py`
- [x] App-level fix in `editor.py` (T-0014, 2026-10-18): `GIT_TRUST_ARGS =
      ["-c", "safe.directory=*"]` is passed to **every** git invocation — `_git`,
      `_is_repo`, `git pull`, `git push`, `git push -u`. Verified: new Part D in
      `tests/test_editor_git_cross_user.py` (editor.py imported under fakeroot,
      repo faked to uid 994) returns `is_repo:true` with the fix and `false`
      without; cross-user API E2E on an isolated instance (`git/status` →
      `is_repo:true`, `git/log` → commits) confirmed; `tests/test_editor.py`
      full E2E (API + Playwright) passes on an isolated instance (no regression).
      **Note:** `editor.py` was left uncommitted because it also carries the
      user's uncommitted `/git/diff` endpoint — commit both together.
- [ ] UI: when `is_repo` is false but the path contains a `.git` dir, show a hint
      pointing here (editor.js — same block as above)
- [ ] Human: restart the `ethos-nas` service to deploy the app-level fix on
      :9000 (until then the interim script remains the unblock)
