# Code Editor — AI agent chat (`pi`, opencode, Claude Code)

A **CHAT** tab in the Code Editor's bottom panel that attaches an AI coding
agent CLI to the open project, using the same PTY bridge as the terminal.

## Status

- [x] Backend: `agent:<name>` PTY target, allowlisted binaries, admin-only
- [x] Backend: `GET /api/terminal/options` → `agents[]` (`name`, `label`, `path`)
- [x] Frontend: CHAT tab with its own pane, lazily started, per-session target
- [x] Hardening: out-of-range resize frames are dropped, never typed into the PTY
- [x] Tests: `tests/test_terminal_ws.py` (target + options + resize),
      `tests/test_editor.py` Part B (CHAT tab)
- [x] Verified on an isolated instance against `pi` 1.5.2 (TUI renders, session
      created under `~/.pi/agent/sessions/<cwd>`)
- [x] Writable-state fallback: `pi --session-dir <DATA_DIR>/agent-sessions/<user>`
      when the service account cannot write the user's `~/.pi`
- [x] `deploy/ethos-nas.service`: `StateDirectory=ethos-nas` +
      `Environment=HOME=/var/lib/ethos-nas` (the system user had no home at all)
- [ ] Optional: dedicated `agent-home` for the service account (see below)

## Why a PTY and not a chat UI

`pi` is an interactive TUI (composer, slash commands, tool approval, session
history). Wrapping it in a hand-rolled message list would duplicate its UI and
break the tool-approval flow. The editor therefore renders the agent's own TUI in
an xterm pane — the same thing you get in a terminal, but docked in the editor,
resizable, and maximizable (⤢, Phase 18).

## Backend

`backend/routers/terminal.py`

- `AGENT_COMMANDS = {pi, opencode, claude}` — the **only** binaries that may be
  executed. The client can pick a name, never a command line.
- `agent:<name>` is validated by `shutil.which(...)`; unknown or missing →
  close 4002 with an explanatory message.
- **Admin-only**, exactly like `host`: the agent can read/write project files and
  run shell commands, i.e. it acts with the service account's privileges.
- Started in the session `cwd` (the open project root) with
  `HOME = <database user's home>` so the CLI finds that user's credentials and
  session store. `TERM=xterm-256color`, PTY is the controlling terminal.
- `GET /api/terminal/options` returns the installed agents so the editor can
  hide/explain the CHAT tab.

## Frontend

`frontend/static/apps/editor.js`

- The tab bar gained **CHAT** between TERMINAL and PROBLEMS.
- `_termEnsureChat()` starts one pane lazily on first activation, with
  `target: "agent:<first installed>"`; with no agent installed the view explains
  what to install instead of failing silently.
- Sessions now carry their own `target` (`sess.target`), so the chat pane and the
  shell splits in the TERMINAL tab are independent: separate WebSockets, separate
  cwds, separate labels. Restart (⟳) restarts shell panes only.
- Agent TUIs install their SIGWINCH handler after start-up, so the resize sent on
  socket open can be missed and the pane stays blank. A second resize ~1.8 s after
  the session starts makes the TUI paint without any user interaction.
- Terminal menu → **AI Chat** jumps straight to the CHAT view (opens the panel if
  it is closed).
- `✕` on the chat pane ends the agent; re-opening CHAT starts a fresh one.
  The whole panel can be maximized with ⤢ for a full-window chat.

## Credentials / HOME model (production)

The NAS server runs as the `ethos-nas` service account, while agent credentials
live in the *login user's* home (`~/.pi/agent/auth.json`, `~/.config/opencode`,
`~/.claude.json`). The PTY sets `HOME` to the requesting user's home, so the CLI
finds the right config — but it must be **readable** by `ethos-nas`, and writes
(session files, refreshed tokens) need write access there too.

Two supported setups:

1. **Share the login user's config with the service account** (simplest when the
   NAS has a single admin, e.g. `marcin`):

   ```bash
   sudo setfacl -R -m u:ethos-nas:rwX /home/marcin/.pi
   sudo setfacl -R -d -m u:ethos-nas:rwX /home/marcin/.pi
   ```

   Dot-directories are intentionally *not* covered by
   `setup_scripts/setup_service.sh` (see `docs/service-user.md`), so this is a
   deliberate opt-in.

2. **Give the service account its own agent home** and authenticate once:

   ```bash
   sudo -u ethos-nas -H pi auth login <provider>
   ```

   Then point the shell env (`ETHOS_AGENT_HOME` is not implemented yet) or keep
   the default: with `HOME=/home/marcin` unreachable, `pi` falls back to its own
   error message in the chat pane.

Symptoms of a credentials problem show up **inside** the chat pane (the agent
prints its own "not authenticated" error) — the PTY session itself stays healthy.

### Troubleshooting: `EACCES: mkdir /home/ethos-nas/.pi/agent/sessions/…`

```
ethos-nas@nasos:…$ pi
Error: EACCES: permission denied, mkdir
  '/home/ethos-nas/.pi/agent/sessions/--home-marcin-apps-compose-wawalocal.pl--'
```

Two separate causes — both addressed:

1. **`pi` typed in the TERMINAL tab runs as the service account.** The `ethos-nas`
   account is created with `useradd --system`, so it has no home directory at all
   — `$HOME` pointed at `/home/ethos-nas`, which does not exist. The deployed unit
   now sets `StateDirectory=ethos-nas` + `Environment=HOME=/var/lib/ethos-nas`;
   reinstall it (`sudo setup_scripts/setup_service.sh`, then
   `systemctl daemon-reload && systemctl restart ethos-nas`) and the host shell
   gets a writable HOME. Use the **CHAT tab** for the agent either way: it runs
   with `HOME` = *your* home, so your config/extensions/models are found.
2. **`~/.pi` was not writable for the service account** (0600 `auth.json`, 0775
   session dirs owned by the login user). Fix without root — you own the files:

   ```bash
   setfacl -R -m u:ethos-nas:rwX /home/marcin/.pi
   setfacl -R -d -m u:ethos-nas:rwX /home/marcin/.pi
   ```

   As a safety net the backend also checks writability before launching `pi`: if
   `~/.pi/agent` is not writable it appends
   `--session-dir <DATA_DIR>/agent-sessions/<username>` (0700, service-owned), so
   the chat still starts instead of dying in Node's stack trace. The warning is
   logged as `agent pi: <user>/.pi is not writable …`.

3. **`~/.cache` is 0700 as well.** Extensions run through jiti, which insists on
   writing `~/.cache/jiti`; as the service account that is another `EACCES`.
   Agent sessions therefore get
   `XDG_CACHE_HOME=<DATA_DIR>/agent-cache/<username>` (0700, service-owned) — the
   user's own cache is never touched.

`pi`'s own wording for the second case is just the raw Node error; if the pane
shows a stack trace starting with `mkdir`, it is this permissions chain.

## Security notes

- Allowlisted binaries only; no `shell=True`, no client-supplied argv.
- Admin-only target (same rule as `host`/`sudo`) — a non-admin gets close 4003.
- An agent runs with the service account's OS permissions. That is the same
  file-access model as the File Manager/editor already use (`docs/service-user.md`),
  widened by whatever ACLs the operator grants above.
- Resize control frames are parsed strictly: anything that *is* a resize message
  but has unusable geometry is dropped instead of being typed into the PTY.

## Verification

- `pi` 1.5.2 detected at `/usr/local/bin/pi`, TUI renders in the CHAT pane,
  session dir created under `~/.pi/agent/sessions/--tmp-ethos-chat-test--/`.
- Raw WS client: `agent:pi` relays the TUI byte stream; `agent:unknown` → close
  4002; `{"type":"resize","cols":100,"rows":999999}` is dropped (marker command
  still echoed, no JSON in the shell).
- Editor: CHAT opens one pane labelled `pi agent`, TERMINAL keeps its own shell
  pane, maximizing the panel expands the chat to the full workbench.
- Permission fallback: `_build_cmd("agent:pi", user)` → `[pi]` when `~/.pi` is
  writable, `[pi, --session-dir <DATA_DIR>/agent-sessions/<user>]` (dir created
  0700) otherwise; `host` stays `/bin/bash --login`.
- Env for agent sessions: `HOME`/`USER`/`LOGNAME` = login user,
  `XDG_CACHE_HOME=<DATA_DIR>/agent-cache/<user>` (0700); `pi` starts cleanly with
  it (no `EACCES`).
- ACLs on this host: `getfacl ~/.pi/agent/auth.json` → `user:ethos-nas:rw-`,
  `getfacl ~/.pi` → `user:ethos-nas:rwx` + inherited default entry.
