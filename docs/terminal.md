# Terminal — Pro PTY Emulation

Upgrade the Terminal app from a line-based pseudo-terminal to a professional-grade
PTY emulator (xterm.js), and harden the backend WebSocket endpoint.

## Problems found in the old implementation

**Backend (`backend/routers/terminal.py`)**
- [x] No JWT validation — any client on the network could open a shell via `/ws/terminal/host`.
- [x] `receive_text()` only: binary frames were never handled; all input was JSON-parsed, so typing `{` or `[` (valid bash) raised and killed the session.
- [x] No resize support — PTY stayed 80×24 forever; `vim`/`top` unusable in wide windows.
- [x] No concurrent-session cap.
- [x] Process group not isolated (`start_new_session`) — background children survived disconnect.- [x] **PTY was not made the controlling terminal** (`start_new_session=True` without `TIOCSCTTY`): `su - <name>` sessions printed `cannot set terminal process group …: Inappropriate ioctl for device` / `no job control in this shell` and interactive programs broke. Fixed with a `preexec_fn` that calls `ioctl(0, TIOCSCTTY, 0)` after `setsid()`.- [x] No logging on session start/end/failure (ethos logger rule).

**Frontend (`frontend/static/apps/terminal.js`)**
- [x] Line-based echo UI: no ANSI colors, no cursor, interactive programs impossible.
- [x] WebSocket built with a raw URL — **no JWT token attached**, so it failed against the hardened backend (and would have been rejected by any auth check).
- [x] No reconnect on network blips / server restarts.
- [x] Dead code: `renderTerminalModal` + modal HTML never used by the windowed app.

## Design

### Wire protocol (`/ws/terminal/{target}`)
| Frame | Meaning |
| --- |---|
| binary (client→server) | raw PTY input, written verbatim to the slave fd |
| text `{"type":"resize","cols":N,"rows":M}` | resize control (clamped 1–500 × 1–200) |
| any other text (client→server) | plain PTY input (never JSON-parsed away) |
| binary (server→client) | raw PTY output |
| text (server→client) | control notices only: startup errors, `[shell exited with code N]` |

Close codes: `4001` auth failed · `4002` invalid target · `1013` too many sessions ·
`1000` shell exited / clean close.

### Targets (allowlist)
- `host` → `/bin/bash --login`
- `sudo` → `sudo /bin/bash --login`
- `user:<name>` → `su - <name>` — interactive PAM login as another system user.
  Name validated against `^[a-z_][a-z0-9._-]{0,31}$`, must exist in `/etc/passwd` and
  have an interactive shell (same eligibility rules as PAM UI login). No sudoers rule
  needed — the target account's password is typed into the PTY. This is the sanctioned
  way to reach root from the UI when the server runs as the dedicated service account:
  log in as your own user, then `sudo -i` inside the session.
  **Default target is `host`** (the server account — no password prompt on login or
  project open). `user:<name>` is an explicit picker choice; options that require a
  password are labelled `(password)`, the server's own account `(server)`.
- `container:<name>` → `docker exec -it <name> /bin/bash` — name validated against
  `^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,62}$` (mirrors Docker's own rules; no shell metacharacters).

### Frontend
- xterm.js **5.5.0** + FitAddon **0.10.0**, pinned UMD builds from jsDelivr in `index.html`
  (`window.Terminal`, `window.FitAddon`). No build step, consistent with the CDN approach used for Tailwind/CodeMirror.
- Toolbar: target badge (click → picker), A−/A+ font size (persisted to localStorage, 9–28 px),
  Clear (wipes screen + sends Ctrl-L so the shell redraws its prompt), status dot
  (connecting / connected / reconnecting / error).
- Target picker: host/sudo + "Log in as user" group from `GET /api/terminal/options`
  (eligible users, same rules as PAM login; the server's own account is marked
  `(server)`) + running containers from `GET /api/docker/containers` (DOM-API-built
  options) + free-text container name with client-side validation. When the process
  user cannot open a sudo shell (dedicated service account), the Sudo option is
  disabled and an explanatory note is shown (`sudo_available`/`sudo_reason`).
- Auto-reconnect with exponential backoff (1.5 s → 15 s cap) into the same xterm instance,
  preserving scrollback; no retry on close code `4001` (auth — reload required).
- Resize: `ResizeObserver` + rAF-debounced `fit.fit()` → resize control frame.
- Theme follows EthOS light/dark via CSS custom properties (MutationObserver on `<html class>`).
- DOM construction: static markup templates with **no interpolation**; all dynamic values set
  via `textContent` / DOM APIs (container names from the Docker API are never interpolated into HTML).

## Tabs (multiple concurrent sessions)

The Terminal window supports multiple tabs; each tab is an independent PTY session
(own xterm.js instance + own `/ws/terminal/{target}` WebSocket). No backend change was
needed — the endpoint already serves concurrent sessions (`MAX_SESSIONS = 32`).

- Tab bar under the toolbar: one tab per session (status dot + target label, truncated,
  full target in `title`), `×` closes a tab, `+` opens a new one (defaults to the most
  recently used target).
- Background tabs stay alive: their WebSocket/PTY keep running while hidden; scrollback
  is preserved. Switching tabs refits xterm + sends a resize frame for the newly visible
  session only.
- Toolbar (target badge, status dot/label) always reflects the **active** tab. Clear and
  the target picker act on the active tab; font size applies to all open tabs.
- Target picker "Connect" reconnects the active tab in place (`term.reset()` + new WS)
  instead of adding a tab.
- Closing the last tab spawns a fresh default tab (window is never left empty).
- Close code `1013` (session cap) shows "too many sessions — retrying…" and keeps
  backoff-retrying so it self-heals when other tabs close.
- **Leak fix:** closing the Terminal window now disposes every session via a
  `registerCloseGuard('terminal', …)` hook — previously each open tab leaked its WS +
  shell process until page reload (the guard is what makes multi-tab safe against the
  32-session cap).
- Shell exit still auto-respawns the tab's connection (existing behavior, unchanged).

## Status
- [x] Tabs: `frontend/static/apps/terminal.js` restructured to per-tab sessions + tab bar;
      window-close guard disposes all sessions. Verified on isolated :9002 — concurrent
      sessions independent (env + resize isolation), existing 11-test WS suite still green.
- [x] Backend rewrite (`backend/routers/terminal.py`) — auth, protocol, resize, caps, cleanup, logging.
- [x] Frontend rewrite (`frontend/static/apps/terminal.js`) — xterm.js session, toolbar, picker, reconnect.
- [x] `index.html` — pinned xterm CDN assets; removed dead `.terminal-*` CSS from the old line UI.
- [x] `user:<name>` (su) target + `GET /api/terminal/options` — verified on isolated :9002:
      options payload, 4001 no-token, 4002 unknown/nologin user with specific reason,
      `Password:` prompt over WS for a valid user.
- [ ] End-to-end verification on isolated instance (:9001).

## Verification checklist (isolated instance)
```bash
ETHOS_PORT=9001 ETHOS_DATA_DIR=/tmp/ethos-test-data nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9001 > /tmp/ethos-nas-test.log 2>&1 & disown
```
- [ ] WS without token → closed `4001`.
- [ ] WS with valid JWT (`POST /api/auth/login`) → shell prompt round-trip over binary frames.
- [ ] Resize JSON frame accepted; non-resize text (e.g. `{`) reaches the PTY verbatim.
- [ ] Invalid target (`container:bad name!`) → closed `4002`.
- [ ] `exit` in shell → `[shell exited with code 0]` notice, clean close, no orphan bash.

## 2026-11-09 — PTY job control + no password prompt on startup
- `_pty_preexec()` now sets `TIOCSCTTY` on fd 0 in the child (after
  `start_new_session`), so the PTY is the session's controlling terminal. Verified
  with a raw WebSocket client against an isolated instance (:9008):
  `user:marcin` no longer prints `cannot set terminal process group …` /
  `no job control in this shell` (host target remains clean).
- Default terminal target is now **`host`** in both the Terminal app and the Code
  Editor — no `Password:` prompt when logging in or opening a project. Switching to
  `user:<name>` stays available and is labelled `(password)`.
- Editor terminal verifies its cwd again (`_currentCwdFor` accepted the shell
  element itself), so it starts in the project root instead of the server's cwd.
