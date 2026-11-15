"""EthOS NAS — Browser PTY Terminal Router

WebSocket:
- WS /ws/terminal/{target} — interactive PTY session (raw bytes both ways)

REST:
- GET /api/terminal/options — picker data: eligible login users + sudo availability

Target values:
- host            → /bin/bash --login on the host
- sudo            → sudo /bin/bash --login
- user:<name>     → su - <name> (interactive PAM login as another system user)
- container:<name> → docker exec -it <name> /bin/bash
- agent:<name>    → an AI agent CLI from AGENT_COMMANDS (pi / opencode / claude),
                    started in `cwd` with HOME pointing at the logged-in user's
                    home so it finds that user's credentials. Admin-only, like
                    `host`: these CLIs can read and write files.

Wire protocol:
- Binary frames  → raw PTY input (keyboard data from xterm.js)
- Text frame     → control message {"type":"resize","cols":N,"rows":M}
                   Any other text is written to the PTY verbatim.

Query params:
- token  → JWT (required)
- cwd    → optional working directory for the shell (absolute path, must exist).
           Used by the editor's embedded terminal so an open project starts
           with a shell already inside it. Ignored for container targets.
           Invalid value → close 4003.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
from pathlib import Path

try:
    import fcntl  # type: ignore[import-untyped]
    import pty  # type: ignore[import-untyped]
    import pwd  # type: ignore[import-untyped]
    import termios  # type: ignore[import-untyped]
except ImportError:  # Linux-only modules, skip on Windows
    fcntl = pwd = pty = termios = None

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ethos-nas.routers.terminal")
router = APIRouter()

_VALID_CONTAINER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,62}$")
_VALID_USERNAME = re.compile(r"^[a-z_][a-z0-9._-]{0,31}$")

# Sanity bounds for resize control messages (xterm.js reports real sizes).
MAX_COLS = 500
MAX_ROWS = 200

# AI agent CLIs the editor's CHAT tab may attach to. Only these binaries are
# ever executed — a client can pick a name, never a command line. See
# docs/agent-chat.md for the credential/HOME model.
AGENT_COMMANDS = {
    "pi": "pi",
    "opencode": "opencode",
    "claude": "claude",
}
AGENT_LABELS = {
    "pi": "PI agent",
    "opencode": "opencode",
    "claude": "Claude Code",
}

# Cap concurrent PTY sessions so a misbehaving client cannot exhaust fds.
MAX_SESSIONS = 32
_active_sessions = 0


def _check_user_target(name: str) -> tuple[bool, str]:
    """Validate a `user:<name>` target.

    Returns (ok, reason). The user must exist and have an interactive shell,
    mirroring the PAM-login eligibility rules so the picker and the WS agree.
    """
    if pwd is None:
        return False, "user lookup unavailable on this platform"
    if not _VALID_USERNAME.match(name):
        return False, f"invalid username '{name}'"
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return False, f"user '{name}' does not exist"
    from backend.routers.auth_pam import VALID_SHELLS
    if entry.pw_shell not in VALID_SHELLS:
        return False, f"user '{name}' has no interactive shell ({entry.pw_shell})"
    return True, ""

def _validate_cwd(cwd: str) -> str | None:
    """Validate an optional working-directory parameter.

    Returns the resolved absolute path if `cwd` is an absolute path that
    exists as a directory, else None. The NAS gives the OS-level access
    control the final say (same policy as file paths — no allowlist).
    """
    if not cwd or not cwd.startswith("/"):
        return None
    resolved = os.path.abspath(cwd)
    if not os.path.isdir(resolved):
        return None
    return resolved

def _agent_binary(name: str) -> str | None:
    """Path to an allowlisted agent CLI, or None when unknown/not installed."""
    binary = AGENT_COMMANDS.get(name)
    return shutil.which(binary) if binary else None


def _agent_session_dir(username: str) -> str | None:
    """Writable ``--session-dir`` for an agent whose own store is read-only.

    In production the server runs as the ``ethos-nas`` service account while the
    agent's config lives in the *login user's* home. Reading that config usually
    works (world-readable files), but creating session folders there does not
    unless the operator granted ACLs — the agent then dies with
    ``EACCES: mkdir /home/ethos-nas/.pi/...``. Fall back to a service-owned
    directory under DATA_DIR so the chat still starts (docs/agent-chat.md).
    Returns None when the user's own store is usable.
    """
    home = ""
    if pwd is not None:
        try:
            home = pwd.getpwnam(username).pw_dir
        except KeyError:
            home = ""
    if home:
        agent_dir = os.path.join(home, ".pi", "agent")
        if os.path.isdir(agent_dir) and os.access(agent_dir, os.W_OK):
            return None      # user's own store works — keep their sessions
    try:
        from backend.config import settings

        fallback = Path(settings.DATA_DIR) / "agent-sessions" / (username or "unknown")
        fallback.mkdir(parents=True, exist_ok=True)
        os.chmod(fallback, 0o700)
        return str(fallback)
    except OSError as exc:
        logger.error("agent session dir: cannot prepare fallback for %r: %s", username, exc)
        return None


def _prepare_agent_env(username: str, env: dict) -> None:
    """Environment for an agent CLI: the user's config, a writable cache.

    ``HOME`` must point at the *login user's* home so the CLI finds their
    credentials/models. Their `~/.cache` is usually 0700 and therefore not
    writable by the service account, while extensions (jiti) insist on writing
    a cache — so `XDG_CACHE_HOME` is redirected into a service-owned directory.
    """
    if pwd is not None:
        try:
            entry = pwd.getpwnam(username)
            env["HOME"] = entry.pw_dir
            env["USER"] = username
            env["LOGNAME"] = username
        except KeyError:
            logger.warning("Agent session: no passwd entry for %r; keeping service HOME", username)
    try:
        from backend.config import settings

        cache = Path(settings.DATA_DIR) / "agent-cache" / (username or "unknown")
        cache.mkdir(parents=True, exist_ok=True)
        os.chmod(cache, 0o700)
        env["XDG_CACHE_HOME"] = str(cache)
    except OSError as exc:
        logger.warning("Agent session: cannot prepare cache dir for %r: %s", username, exc)


def _validate_target(target: str) -> bool:
    """Return True if the terminal target is on the allowlist."""
    if target in ("host", "sudo"):
        return True
    if target.startswith("container:"):
        name = target.split(":", 1)[1]
        return bool(_VALID_CONTAINER.match(name))
    if target.startswith("user:"):
        ok, _reason = _check_user_target(target.split(":", 1)[1])
        return ok
    if target.startswith("agent:"):
        return _agent_binary(target.split(":", 1)[1]) is not None
    return False


def _build_cmd(target: str, username: str = "") -> list[str]:
    """Build the subprocess command for the given target."""
    if target == "host":
        return ["/bin/bash", "--login"]
    if target == "sudo":
        return ["sudo", "/bin/bash", "--login"]
    if target.startswith("user:"):
        name = target.split(":", 1)[1]
        return ["su", "-", name]
    if target.startswith("container:"):
        name = target.split(":", 1)[1]
        return ["docker", "exec", "-it", name, "/bin/bash"]
    if target.startswith("agent:"):
        name = target.split(":", 1)[1]
        binary = _agent_binary(name)
        if not binary:
            raise ValueError(f"agent '{name}' is not installed on this server")
        args = [binary]
        if name == "pi":
            session_dir = _agent_session_dir(username)
            if session_dir:
                logger.info(
                    "agent %s: %s/.pi is not writable for the server account — "
                    "using --session-dir %s", name, username, session_dir)
                args += ["--session-dir", session_dir]
        return args
    return ["/bin/bash", "--login"]


def _read_resize(text: str) -> tuple[tuple[int, int] | None, bool]:
    """Classify a text frame as a resize control message.

    Returns ``(size, is_control)``:
    * ``((cols, rows), True)`` — valid resize, apply it;
    * ``(None, True)``         — a resize frame with unusable geometry: drop it.
      It must NEVER be written to the PTY, otherwise the JSON lands in the
      shell/agent prompt as text;
    * ``(None, False)``        — not a control message, i.e. typed user input.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None, False  # not JSON → plain PTY input
    if not isinstance(obj, dict) or obj.get("type") != "resize":
        return None, False  # other JSON is user input too — never swallow it
    try:
        cols = int(obj.get("cols", 0))
        rows = int(obj.get("rows", 0))
    except (TypeError, ValueError):
        return None, True
    if not (1 <= cols <= MAX_COLS and 1 <= rows <= MAX_ROWS):
        logger.warning("Ignoring out-of-range terminal resize: %dx%d", cols, rows)
        return None, True
    return (cols, rows), True


def _apply_resize(fd: int, cols: int, rows: int) -> None:
    if fcntl is None or termios is None:  # non-Linux platform
        raise OSError("PTY resize not supported on this platform")
    fcntl.ioctl(
        fd, termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, cols, 0, 0),
    )


def _pty_preexec() -> None:
    """Make the PTY the controlling terminal of the child session.

    ``Popen(start_new_session=True)`` already calls ``setsid()`` in the child,
    but a new session starts *without* a controlling terminal.  Interactive
    programs then cannot manage job control: bash prints

        cannot set terminal process group …: Inappropriate ioctl for device
        no job control in this shell

    and this hits especially hard for ``user:<name>`` sessions, where ``su``
    sits between the PTY and the login shell.  Popen redirects fds before
    running preexec_fn, so fd 0 is already the PTY slave here; TIOCSCTTY
    makes it the controlling terminal for the whole session.
    """
    if fcntl is None or termios is None:  # non-Linux platform
        return
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _get_pgid(pid: int) -> int | None:
    """Process group id for the session, or None if the process is already gone."""
    with contextlib.suppress(ProcessLookupError, OSError):
        return os.getpgid(pid)
    return None


def _terminate_session(proc: subprocess.Popen) -> None:
    """Terminate the shell's whole process group (SIGTERM → SIGKILL).

    The session runs in its own session/process group (start_new_session=True),
    so background children of the shell (top, vim, sleep …) are reaped too.
    """
    pgid = _get_pgid(proc.pid)

    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        logger.warning("Terminal process %s ignored SIGTERM; sending SIGKILL", proc.pid)
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    # Final reap; ignore if already gone.
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        logger.error("Terminal process %s still alive after SIGKILL", proc.pid)


def _sudo_shell_capability() -> tuple[bool, str]:
    """Whether `sudo /bin/bash --login` can work for the user running this process.

    The dedicated service account (nologin shell) has no sudo rule for a shell —
    by design (see docs/service-user.md); the UI should hide the option and point
    at 'Log in as user' instead. Interactive dev accounts keep it.
    """
    try:
        uid = os.getuid()
    except AttributeError:  # non-Unix platform
        return True, ""
    if uid == 0:
        return True, "running as root"
    if pwd is None:
        return False, "user lookup unavailable on this platform"
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        return False, "unknown process user — sudo shell unavailable"
    if os.path.basename(entry.pw_shell or "") in ("nologin", "false"):
        return (False,
                f"server runs as service account '{entry.pw_name}' without a login "
                "shell — use 'Log in as user' instead")
    return True, ""


_SUDO_CAPABILITY = _sudo_shell_capability()


@router.get("/api/terminal/options")
async def terminal_options() -> dict:
    """Return picker data for the Terminal app.

    `users` mirrors PAM-login eligibility (UID >= 1000, interactive shell) so the
    'Log in as user' dropdown only offers accounts a user could actually log in
    as. `sudo_available` reflects whether the process's own account may open a
    sudo shell (no for the dedicated service account).
    """
    from backend.routers.auth_pam import _get_eligible_system_users

    users = await asyncio.to_thread(_get_eligible_system_users)
    try:
        server_user = pwd.getpwuid(os.getuid()).pw_name if pwd is not None else ""
    except (KeyError, AttributeError):
        server_user = ""
    return {
        "users": users,
        "server_user": server_user,
        "sudo_available": _SUDO_CAPABILITY[0],
        "sudo_reason": _SUDO_CAPABILITY[1],
        "agents": [
            {"name": name, "label": AGENT_LABELS.get(name, name), "path": path}
            for name in AGENT_COMMANDS
            if (path := _agent_binary(name))
        ],
    }


@router.websocket("/ws/terminal/{target}")
async def ws_terminal(websocket: WebSocket, target: str) -> None:
    """Provide an interactive PTY session over WebSocket.

    Args:
        websocket: The incoming WebSocket connection.
        target: Session target — host | sudo | user:<name> | container:<name>
    """
    # Validate JWT from query params before accepting the connection.
    token = websocket.query_params.get("token", "")
    from backend.routers.auth import _verify_token
    payload = _verify_token(token)
    if not payload:
        # Accept before closing so the client receives the semantic close code
        # (4001) — a pre-accept close surfaces as an opaque HTTP 403 instead.
        await websocket.accept()
        logger.warning("Terminal WebSocket rejected: missing or invalid token")
        await websocket.close(code=4001, reason="Authentication required")
        return
    username = payload.get("username", "?")

    # `host` opens a login shell as the service user, `sudo` escalates to
    # root and `agent:<name>` runs an AI agent with file/bash tools — all
    # admin-only. `user:<name>` stays available to everyone.
    if target in ("host", "sudo") or target.startswith("agent:"):
        if str(payload.get("role") or "user") != "admin":
            logger.warning("Terminal target %r denied for non-admin user %s", target, username)
            await websocket.accept()
            await websocket.close(code=4003, reason="Admin access required")
            return

    global _active_sessions
    if _active_sessions >= MAX_SESSIONS:
        logger.warning("Terminal session rejected for %s: %d sessions already active",
                       username, _active_sessions)
        await websocket.accept()
        await websocket.close(code=1013, reason="Too many terminal sessions; try again later")
        return

    await websocket.accept()

    if pty is None:  # non-Linux platform
        logger.error("Terminal requested but PTY support is unavailable on this platform")
        await websocket.send_text("[Error: PTY not supported on this platform]")
        await websocket.close(code=1011)
        return

    if not _validate_target(target):
        reason = ""
        if target.startswith("user:"):
            _ok, reason = _check_user_target(target.split(":", 1)[1])
        logger.warning("Rejected invalid terminal target %r from user %s%s",
                       target, username, f" ({reason})" if reason else "")
        await websocket.send_text(f"[Error: {reason or 'invalid terminal target'}]")
        await websocket.close(code=4002, reason="Invalid target")
        return

    # Optional working directory (editor's embedded terminal starts inside the
    # open project). Must be an absolute path that exists as a directory.
    # Containers keep their own default — a host path is meaningless inside.
    resolved_cwd = None
    cwd_param = websocket.query_params.get("cwd", "")
    if cwd_param:
        if target.startswith("container:"):
            logger.debug("Ignoring cwd %r for container target %s", cwd_param, target)
        else:
            resolved_cwd = _validate_cwd(cwd_param)
            if resolved_cwd is None:
                logger.warning(
                    "Rejected terminal session for %s: invalid cwd %r", username, cwd_param)
                await websocket.send_text(f"[Error: invalid working directory {cwd_param!r}]")
                await websocket.close(code=4003, reason="Invalid working directory")
                return

    try:
        cmd = _build_cmd(target, username)
    except ValueError as exc:
        logger.warning("Terminal target %r rejected for %s: %s", target, username, exc)
        await websocket.send_text(f"[Error: {exc}]")
        await websocket.close(code=4002, reason="Invalid target")
        return
    master_fd, slave_fd = pty.openpty()
    # Give the shell a sane initial size before it starts (the client sends its
    # real geometry as soon as xterm.js fits).
    with contextlib.suppress(OSError):
        _apply_resize(master_fd, 80, 24)

    proc: subprocess.Popen | None = None
    read_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    try:
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        if target.startswith("agent:"):
            _prepare_agent_env(username, env)
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,  # own process group → clean killpg on exit
            preexec_fn=_pty_preexec,  # PTY becomes the controlling terminal
            env=env,
            cwd=resolved_cwd,
        )
    except Exception as exc:
        logger.error("Failed to start terminal process for %s (user=%s): %s", target, username, exc)
        os.close(master_fd)
        await websocket.send_text(f"[Error: could not start shell: {exc}]")
        await websocket.close(code=1011)
        return
    finally:
        # The slave end is never needed by the parent process.
        with contextlib.suppress(OSError):
            os.close(slave_fd)

    _active_sessions += 1
    logger.info("Terminal session started: target=%s user=%s pid=%d cwd=%s",
                target, username, proc.pid, resolved_cwd)

    async def _read_pty() -> None:
        """Forward PTY output to the WebSocket until the shell exits."""
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
            except OSError:
                break  # master fd closed (cleanup race) — end quietly
            if not data:
                break  # EOF → shell exited
            try:
                await websocket.send_bytes(data)
            except Exception:
                return  # client gone; the receive loop will tear down

        # Shell has exited. Tell the client, then close gracefully.
        # Reap the child so returncode is set — the PTY read loop ends as soon
        # as the slave side closes, which can beat Popen's own bookkeeping.
        if proc is not None:
            def _reap() -> int | None:
                try:
                    return proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    return None

            code = await loop.run_in_executor(None, _reap)
            if code is None:  # shell still alive (rare) — fall back to last known state
                code = proc.returncode if proc.returncode is not None else -1
        else:
            code = -1
        logger.info("Terminal shell exited: target=%s user=%s code=%s", target, username, code)
        try:
            await websocket.send_text(f"\r\n\x1b[90m[shell exited with code {code}]\x1b[0m\r\n")
            await websocket.close(code=1000, reason="shell exited")
        except Exception as exc:
            logger.debug("Could not send shell-exit notice for %s (client likely gone): %s",
                         target, exc)

    read_task = asyncio.create_task(_read_pty())

    try:
        while True:
            msg = await websocket.receive()
            # Raw receive() RETURNS the disconnect message (it does not raise) —
            # without this check the loop calls receive() again and Starlette
            # raises "Cannot call 'receive' once a disconnect message has been received."
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg:
                data = msg["bytes"]
            elif "text" in msg:
                size, is_control = _read_resize(msg["text"])
                if is_control:
                    if size is not None:
                        try:
                            _apply_resize(master_fd, *size)
                        except OSError as exc:
                            logger.warning("Resize failed for %s: %s", target, exc)
                    continue
                data = msg["text"].encode()
            else:
                continue
            # Direct write (not executor): pty writes are small and must keep
            # keystroke ordering; the slave buffer drains as the shell reads.
            try:
                os.write(master_fd, data)
            except OSError:
                break  # PTY gone — shell exited; _read_pty ends the session
    except WebSocketDisconnect:
        logger.info("Terminal WebSocket disconnected: %s", target)
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
        _terminate_session(proc)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        _active_sessions -= 1
        logger.info("Terminal session ended: target=%s user=%s after %.1fs",
                    target, username, loop.time() - started_at)
