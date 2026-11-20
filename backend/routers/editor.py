"""Code Editor API — /api/editor.

Recent-projects store (JSON under DATA_DIR) + git operations for project folders.

Git runs as subprocess argument lists only (never shell=True), with cwd set to the
repo root and a hard timeout. All user-supplied paths are realpath-checked against
the repo root and passed after `--` so they can never be parsed as options.
"""

import asyncio
import ast
import contextlib
import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.config import settings
from backend.services import path_scope, user_data

logger = logging.getLogger("ethos-nas.routers.editor")

# Same per-role path scope as the File Manager: the editor reads and writes
# arbitrary absolute paths otherwise (TODO.md P0-06/P0-13).
router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

MAX_RECENTS = 12
GIT_TIMEOUT_S = 15

# git >= 2.35.3 refuses to run in a repository whose owner differs from the
# effective uid ("detected dubious ownership"). The service user legitimately
# manages user-owned repos (group-writable trees), so trust every repo it can
# access; real access stays governed by POSIX permissions. See
# docs/editor-git-cross-user.md (T-0011/T-0014).
GIT_TRUST_ARGS = ["-c", "safe.directory=*"]
_GIT_GLOBAL_CONFIG: str | None = None


def _git_env() -> dict:
    """Env for git subprocesses: safe.directory via a global-config file.

    The `-c safe.directory=*` flag above only covers the repository git is
    invoked in — when push/pull opens a *remote* repository (e.g. a local bare
    remote owned by the login user), git re-runs the ownership check there and
    ignores command-line safe.directory, failing with "detected dubious
    ownership ... and the repository exists". safe.directory in a global
    config file is respected for both sides, and this works even though the
    service user has no $HOME. The file lives under DATA_DIR (service-owned).
    """
    global _GIT_GLOBAL_CONFIG
    env = dict(os.environ)
    if _GIT_GLOBAL_CONFIG is None:
        try:
            path = settings.DATA_DIR / "git" / "global-gitconfig"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("[safe]\ndirectory = *\n")
            _GIT_GLOBAL_CONFIG = str(path)
        except OSError as exc:
            logger.warning("could not prepare git global config: %s", exc)
    if _GIT_GLOBAL_CONFIG:
        env["GIT_CONFIG_GLOBAL"] = _GIT_GLOBAL_CONFIG
    return env


class GitError(Exception):
    pass


# ── Recent projects store ───────────────────────────────────────────────────

_LOCK = threading.Lock()


def _load_recents() -> list[dict]:
    data = user_data.user_store("editor.json", version=1).load()
    recents = data.get("recents", []) if isinstance(data, dict) else []
    return [r for r in recents if isinstance(r, dict) and r.get("path")]


def _save_recents(recents: list[dict]) -> None:
    user_data.user_store("editor.json", version=1).save({"version": 1, "recents": recents})


class RecentIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

class NewProjectIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    git_init: bool = False

def _expand_request_path(request: Request, path: str) -> str:
    """Expand a leading ``~`` against the *requesting* user's home directory.

    The service runs as the ``ethos-nas`` account, so ``os.path.expanduser``
    alone would resolve ``~/…`` against the service account's HOME (e.g.
    ``~/apps/…`` → ``/home/apps/…``) instead of the browser user's home.
    The JWT payload (attached by the auth middleware) carries the username —
    use its passwd entry. ``~user/…`` forms and unknown users fall back to
    plain ``expanduser``.
    """
    if path == "~" or path.startswith("~/"):
        username = (getattr(request.state, "current_user", None) or {}).get("username")
        if username:
            try:
                path = pwd.getpwnam(username).pw_dir + path[1:]
            except KeyError:
                logger.warning("cannot resolve home for user %r; falling back to service HOME", username)
        else:
            logger.warning("no username in request state; falling back to service HOME for %r", path)
    return path_scope.sanitize(path)

@router.post("/projects/new")
async def create_new_project(request: Request, body: NewProjectIn) -> dict:
    """Create a new project folder (optionally with `git init`) and add it to recents."""
    path = _expand_request_path(request, body.path)
    if not path or path == "/":
        raise HTTPException(status_code=400, detail="Invalid project path")
    if os.path.exists(path):
        raise HTTPException(status_code=409, detail="Path already exists")
    try:
        os.makedirs(path)
    except PermissionError:
        logger.error("New project failed — permission denied for %s", path)
        raise HTTPException(status_code=403, detail="Permission denied") from None
    except OSError as exc:
        logger.error("New project failed for %s: %s", path, exc)
        raise HTTPException(status_code=500, detail=f"Could not create folder: {exc}") from exc

    is_repo = False
    if body.git_init:
        try:
            _git(Path(path), "init")
            is_repo = True
        except GitError as exc:
            # Folder exists; report the git failure in-band instead of failing the request.
            logger.warning("New project created at %s but git init failed: %s", path, exc)

    with _LOCK:
        recents = [r for r in _load_recents() if r["path"] != path]
        recents.insert(0, {
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or "/",
            "opened_at": datetime.now(UTC).isoformat(),
        })
        recents = recents[:MAX_RECENTS]
        _save_recents(recents)

    logger.info("New project created: %s (git_init=%s, is_repo=%s)", path, body.git_init, is_repo)
    return {"ok": True, "path": path, "name": os.path.basename(path.rstrip("/")) or "/", "is_repo": is_repo}

@router.get("/projects/recent")
async def list_recent_projects() -> dict:
    with _LOCK:
        return {"recents": _load_recents()}


@router.post("/projects/recent")
async def add_recent_project(request: Request, body: RecentIn) -> dict:
    path = _expand_request_path(request, body.path)
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Not a directory")
    with _LOCK:
        recents = [r for r in _load_recents() if r["path"] != path]
        recents.insert(0, {
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or "/",
            "opened_at": datetime.now(UTC).isoformat(),
        })
        recents = recents[:MAX_RECENTS]
        _save_recents(recents)
    return {"recents": recents}


@router.delete("/projects/recent")
async def remove_recent_project(path: str = Query(...)) -> dict:
    with _LOCK:
        recents = [r for r in _load_recents() if r["path"] != path]
        _save_recents(recents)
    return {"recents": recents}


# ── Syntax check (Problems panel) ───────────────────────────────────────────
# The editor's PROBLEMS tab reports diagnostics for the *active file*, not
# server log lines (those live in the Logs app → App Events). There is no
# language server on a NAS, so only syntax-level checks run:
#   .py/.pyi      → ast.parse      (in-process, nothing is executed)
#   .json         → json.loads
#   .jsonc/.json5 → json.loads after stripping comments + trailing commas
#   .sh/.bash     → bash -n        (needs bash on the host)
#   .js/.mjs/.cjs → node --check   (needs node on the host)
# Anything else answers checked=false so the UI can say "not checked" instead
# of implying the file is clean.

CHECK_TIMEOUT_S = 5
_CHECK_MAX_CHARS = 5 * 1024 * 1024


class CheckIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(default="", max_length=_CHECK_MAX_CHARS)


def _problem(severity: str, line, col, message: str, source: str) -> dict:
    def _int(value, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    return {
        "severity": severity,
        "line": _int(line, 1),
        "col": _int(col, 1),
        "message": (message or "syntax error").strip()[:500],
        "source": source,
    }


def _check_python(text: str) -> list[dict]:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return [_problem("error", exc.lineno, exc.offset, exc.msg, "python")]
    except ValueError as exc:        # e.g. null bytes in the source
        return [_problem("error", 1, 1, str(exc), "python")]
    return []


def _strip_jsonc(text: str) -> str:
    """Blank out // and /* */ comments (string-aware), then drop trailing commas."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and nxt == "*":
            out.extend((" ", " "))
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.extend((" ", " "))
                i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _check_json(text: str, source: str) -> list[dict]:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [_problem("error", exc.lineno, exc.colno, exc.msg, source)]
    return []


def _parse_tool_error(output: str, source: str) -> list[dict]:
    """Turn `bash -n` / `node --check` stderr into a single diagnostic."""
    text = (output or "").strip()
    if not text:
        return [_problem("error", 1, 1, "syntax check failed", source)]
    first = text.splitlines()[0]
    if source == "bash":
        m = re.search(r": line (\d+): (.*)", text)
        if m:
            return [_problem("error", m.group(1), 1, m.group(2), source)]
        return [_problem("error", 1, 1, first, source)]
    # node --check: "<file>:<line>" then the source line, caret, "SyntaxError: …"
    line = 1
    m = re.search(r":(\d+)\s*$", first) or re.search(r":(\d+)\b", text)
    if m:
        line = m.group(1)
    msg = next((ln for ln in text.splitlines() if ln.lstrip().startswith(("SyntaxError", "Error"))), first)
    return [_problem("error", line, 1, msg, source)]


def _check_with_tool(cmd: list[str], suffix: str, text: str, source: str) -> list[dict] | None:
    """Run an external syntax checker on a temp copy of *text*.

    Returns None when the tool is not installed (nothing was checked) so the
    caller can report checked=false rather than "no problems".
    """
    if not shutil.which(cmd[0]):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="ethos-check-"))
    try:
        target = tmp / f"check{suffix}"
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("syntax check: cannot write temp file: %s", exc)
            return None
        try:
            proc = subprocess.run([*cmd, str(target)], capture_output=True, text=True,
                                  timeout=CHECK_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("syntax check (%s) failed to run: %s", source, exc)
            return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode == 0:
        return []
    return _parse_tool_error(proc.stderr or proc.stdout, source)


def _check_javascript(text: str) -> list[dict] | None:
    # node interprets .mjs as ESM and .cjs as CommonJS; pick by top-level syntax.
    esm = re.search(r"^\s*(?:import|export)\b", text, re.MULTILINE) is not None
    return _check_with_tool(["node", "--check"], ".mjs" if esm else ".cjs", text, "node")


@router.post("/check")
async def check_file(body: CheckIn) -> dict:
    """Syntax-check editor content → diagnostics for the bottom panel's PROBLEMS tab."""
    ext = os.path.splitext(body.path)[1].lower()
    language = ext.lstrip(".") or "plaintext"
    checked = True
    try:
        if ext in {".py", ".pyi"}:
            problems = _check_python(body.content)
        elif ext == ".json":
            problems = _check_json(body.content, "json")
        elif ext in {".jsonc", ".json5"}:
            problems = _check_json(_strip_jsonc(body.content), "jsonc")
        elif ext in {".sh", ".bash"}:
            problems = await asyncio.to_thread(_check_with_tool, ["bash", "-n"], ".sh", body.content, "bash")
            checked = problems is not None
            problems = problems or []
        elif ext in {".js", ".mjs", ".cjs"}:
            problems = await asyncio.to_thread(_check_javascript, body.content)
            checked = problems is not None
            problems = problems or []
        else:
            return {"path": body.path, "language": language, "checked": False, "problems": []}
    except Exception as exc:  # a checker bug must never break the editor
        logger.error("syntax check failed: path=%s: %s", body.path, exc, exc_info=True)
        return {"path": body.path, "language": language, "checked": False, "problems": [],
                "detail": "syntax check failed"}
    return {"path": body.path, "language": language, "checked": checked, "problems": problems}


# ── Git helpers ─────────────────────────────────────────────────────────────

def _repo_root(repo: str) -> Path:
    root = Path(path_scope.sanitize(repo))
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="Repo path is not a directory")
    return root


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(
            ["git", "-c", "advice.detachedHead=false", *GIT_TRUST_ARGS, *args],
            cwd=str(root), capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
            env=_git_env())
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])}… timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise GitError(str(exc)) from exc
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else f"exit {r.returncode}"
        raise GitError(detail[:300])
    return r


def _repo_toplevel(root: Path) -> str | None:
    """Absolute toplevel of the git work tree containing root, or None.

    git walks up the directory tree, so a folder merely *inside* a parent
    repo also resolves to a toplevel — callers must compare it against root
    to decide whether the folder is its own repository.
    """
    try:
        r = subprocess.run(["git", *GIT_TRUST_ARGS, "rev-parse", "--show-toplevel"],
                           env=_git_env(),
                           cwd=str(root), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _is_repo_root(root: Path) -> bool:
    """True only when root is itself the top level of a git repository.

    The editor's Git panel is scoped to the opened project folder: an
    enclosing repo must not leak into it (opening ~/apps/compose/x must not
    show or mutate the state of ~/apps).
    """
    toplevel = _repo_toplevel(root)
    if toplevel is None:
        return False
    return os.path.realpath(toplevel) == os.path.realpath(str(root))


def _safe_paths(root: Path, paths: list[str]) -> list[str]:
    """Validate relative repo paths; reject option-like or escaping entries."""
    root_real = os.path.realpath(str(root))
    out = []
    for p in paths:
        if not p or p.startswith(("/", "-")) or "\x00" in p:
            raise HTTPException(status_code=400, detail=f"Invalid path: {p!r}")
        full = os.path.realpath(os.path.join(root_real, p))
        if full != root_real and not full.startswith(root_real + os.sep):
            raise HTTPException(status_code=400, detail=f"Path escapes repo: {p!r}")
        out.append(p)
    return out


_STATUS_CHARS = set("MADRCU?! ")


def _status_payload(root: Path) -> dict:
    toplevel = _repo_toplevel(root)
    is_repo = (toplevel is not None
               and os.path.realpath(toplevel) == os.path.realpath(str(root)))
    inside_repo = None if is_repo else toplevel
    branch = None
    ahead = behind = None
    files: list[dict] = []
    if is_repo:
        with contextlib.suppress(GitError):
            branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
        try:
            out = _git(root, "rev-list", "--left-right", "--count", "@{u}...HEAD").stdout.split()
            ahead, behind = int(out[0]), int(out[1])
        except (GitError, ValueError, IndexError):
            ahead = behind = None  # no upstream configured
        out = _git(root, "status", "--porcelain=v1", "-z").stdout
        for rec in out.split("\x00"):
            if len(rec) < 4 or rec[0] not in _STATUS_CHARS or rec[1] not in _STATUS_CHARS:
                continue   # skips rename source-name records ("R  new\0old\0")
            files.append({"path": rec[3:], "status": rec[:2]})
        merging = _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    else:
        merging = False
    return {"is_repo": is_repo, "inside_repo": inside_repo, "branch": branch,
            "ahead": ahead, "behind": behind, "merging": merging, "files": files}


# ── Git endpoints ───────────────────────────────────────────────────────────

@router.get("/git/status")
async def git_status(repo: str = Query(...)) -> dict:
    root = _repo_root(repo)
    try:
        return _status_payload(root)
    except GitError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class PathsIn(BaseModel):
    repo: str
    paths: list[str] = Field(default_factory=list)


@router.post("/git/stage")
async def git_stage(body: PathsIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    paths = _safe_paths(root, body.paths)
    try:
        _git(root, "add", "--", *paths)
    except GitError as exc:
        logger.error("git stage failed: repo=%s paths=%d: %s", root, len(paths), exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git stage: %d path(s) in %s", len(paths), root)
    return {"ok": True}


@router.post("/git/unstage")
async def git_unstage(body: PathsIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    paths = _safe_paths(root, body.paths)
    try:
        _git(root, "restore", "--staged", "--", *paths)
    except GitError as exc:
        logger.error("git unstage failed: repo=%s paths=%d: %s", root, len(paths), exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git unstage: %d path(s) in %s", len(paths), root)
    return {"ok": True}


class CommitIn(BaseModel):
    repo: str
    message: str = Field(min_length=1, max_length=4096)


@router.post("/git/commit")
async def git_commit(body: CommitIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    staged = [f for f in _status_payload(root)["files"] if f["status"][0] in ("M", "A")]
    if not staged:
        raise HTTPException(status_code=400, detail="Nothing staged to commit — stage files first")
    try:
        r = _git(root, "commit", "-m", body.message)
        hash_m = re.search(r"\[[^\s]+ ([0-9a-f]{7,})\]", r.stdout)
        return {"ok": True, "hash": hash_m.group(1) if hash_m else None, "subject": body.message.splitlines()[0]}
    except GitError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/git/log")
async def git_log(repo: str = Query(...), limit: int = Query(default=20, ge=1, le=100)) -> dict:
    root = _repo_root(repo)
    if not _is_repo_root(root):
        return {"commits": []}
    try:
        out = _git(root, "log", f"-{limit}", "--pretty=format:%h%x1f%an%x1f%aI%x1f%s").stdout
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]})
    except GitError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"commits": commits}


@router.get("/git/diff")
async def git_diff(repo: str = Query(...), path: str = Query(...)) -> dict:
    """Unified diff of a single file vs HEAD (staged + unstaged combined).

    Untracked files have no HEAD version, so the diff is empty and the
    ``untracked`` flag tells the client to treat every line as added.
    """
    root = _repo_root(repo)
    if not _is_repo_root(root):
        return {"path": path, "diff": "", "untracked": False}
    paths = _safe_paths(root, [path])
    try:
        status = _git(root, "status", "--porcelain=v1", "-z", "--", paths[0]).stdout
        untracked = status.startswith("??")
        out = _git(root, "diff", "HEAD", "--unified=3", "--", paths[0]).stdout
        return {"path": paths[0], "diff": out, "untracked": untracked}
    except GitError as exc:
        logger.error("git diff failed: repo=%s path=%s: %s", repo, path, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Side-by-side diff view + hunk-level stage/unstage ───────────────────────
#
# The Git Diff app (docs/git-diff-tool.md) shows old|new side by side. "Staged"
# compares HEAD→index and "Changes" compares index→worktree, so hunk actions
# always operate on the index:
#   stage-hunks   = git apply --cached      (selected hunks of index→worktree)
#   unstage-hunks = git apply --cached -R   (selected hunks of HEAD→index)
# The patch is regenerated server-side at apply time, filtered to the requested
# hunks, and piped to git apply via stdin (no shell, no temp files).

MAX_DIFF_VIEW_CHARS = 200_000   # per-side cap for the side-by-side view


def _git_show_bytes(root: Path, spec: str) -> bytes | None:
    """Read a blob by rev-spec ('HEAD:path' or ':path' for the index).

    Returns None when the object does not exist (file absent from that tree).
    Binary capture so NUL-based binary detection stays reliable. The spec is
    built from a _safe_paths-validated relative path (never starts with '-').
    """
    try:
        r = subprocess.run(["git", *GIT_TRUST_ARGS, "show", spec],
                           cwd=str(root), capture_output=True, timeout=GIT_TIMEOUT_S,
                           env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git show failed: {exc}") from exc
    if r.returncode != 0:
        return None
    return r.stdout


def _hunk_count(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith("@@"))


def _filter_hunks(diff_text: str, indices: list[int]) -> str:
    """Keep the file header + only the selected @@ hunk blocks (0-based, in order)."""
    lines = diff_text.splitlines(keepends=True)
    bounds = [i for i, line in enumerate(lines) if line.startswith("@@")]
    if not bounds:
        return ""
    spans = [(bounds[k], bounds[k + 1] if k + 1 < len(bounds) else len(lines))
             for k in range(len(bounds))]
    out = list(lines[:bounds[0]])
    for k in dict.fromkeys(indices):
        if 0 <= k < len(spans):
            start, end = spans[k]
            out.extend(lines[start:end])
    return "".join(out)


def _apply_to_index(root: Path, patch: str, reverse: bool) -> tuple[bool, str]:
    """Pipe a unified patch into `git apply --cached` (-R when reverse)."""
    if not patch.strip():
        return False, "empty patch"
    cmd = ["git", *GIT_TRUST_ARGS, "apply", "--cached", "--whitespace=nowarn"]
    if reverse:
        cmd.append("-R")
    try:
        r = subprocess.run(cmd, input=patch, cwd=str(root), capture_output=True,
                           text=True, timeout=GIT_TIMEOUT_S, env=_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git apply failed: {exc}"
    if r.returncode != 0:
        detail = ((r.stderr or r.stdout).strip().splitlines()[-1]
                  if (r.stderr or r.stdout) else f"exit {r.returncode}")
        return False, detail[:300]
    return True, ""


def _diff_view_empty(rel: str, side: str, **flags) -> dict:
    base = {"path": rel, "side": side, "old": "", "new": "", "unified": "",
            "untracked": False, "binary": False, "too_large": False}
    base.update(flags)
    return base


@router.get("/git/diff-view")
async def git_diff_view(repo: str = Query(...), path: str = Query(...),
                        side: str = Query(default="changes")) -> dict:
    """Old/new file contents + unified diff for the Git Diff app.

    side=changes: old=index, new=worktree (unified diff index→worktree)
    side=staged:  old=HEAD,  new=index    (unified diff HEAD→index)
    """
    root = _repo_root(repo)
    if side not in ("changes", "staged"):
        raise HTTPException(status_code=400, detail="side must be 'changes' or 'staged'")
    if not _is_repo_root(root):
        return _diff_view_empty(path, side)
    paths = _safe_paths(root, [path])
    rel = paths[0]
    try:
        status = _git(root, "status", "--porcelain=v1", "-z", "--", rel).stdout
        untracked = status.startswith("??")
        if side == "changes":
            unified = _git(root, "diff", "--unified=3", "--", rel).stdout
            old_bytes = None if untracked else _git_show_bytes(root, f":{rel}")
            worktree = root / rel
            new_bytes = worktree.read_bytes() if worktree.is_file() else None
        else:
            unified = _git(root, "diff", "--cached", "--unified=3", "--", rel).stdout
            old_bytes = _git_show_bytes(root, f"HEAD:{rel}")
            new_bytes = _git_show_bytes(root, f":{rel}")
    except GitError as exc:
        logger.error("git diff-view failed: repo=%s path=%s side=%s: %s", root, rel, side, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def _nul(data: bytes | None) -> bool:
        return data is not None and b"\x00" in data[:8192]

    if "Binary files" in unified or _nul(old_bytes) or _nul(new_bytes):
        return _diff_view_empty(rel, side, untracked=untracked, binary=True)

    def _text(data: bytes | None) -> str:
        return "" if data is None else data.decode("utf-8", errors="replace")

    old, new = _text(old_bytes), _text(new_bytes)
    if len(old) > MAX_DIFF_VIEW_CHARS or len(new) > MAX_DIFF_VIEW_CHARS:
        return _diff_view_empty(rel, side, untracked=untracked, too_large=True)
    return {"path": rel, "side": side, "old": old, "new": new, "unified": unified,
            "untracked": untracked, "binary": False, "too_large": False}


class HunksIn(BaseModel):
    repo: str
    path: str
    hunks: list[int] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)


@router.post("/git/stage-hunks")
async def git_stage_hunks(body: HunksIn) -> dict:
    """Stage the selected hunks of a file's unstaged changes (index→worktree)."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    if not body.hunks:
        raise HTTPException(status_code=400, detail="No hunks selected")
    paths = _safe_paths(root, [body.path])
    rel = paths[0]
    try:
        status = _git(root, "status", "--porcelain=v1", "-z", "--", rel).stdout
        if status.startswith("??"):
            # Untracked: no index version exists — staging is always whole-file.
            _git(root, "add", "--", rel)
            logger.info("stage-hunks: added untracked %s in %s", rel, root)
            return {"ok": True, "whole_file": True}
        diff = _git(root, "diff", "--unified=3", "--", rel).stdout
    except GitError as exc:
        logger.error("stage-hunks failed: repo=%s path=%s: %s", root, rel, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if body.total and body.total != _hunk_count(diff):
        raise HTTPException(status_code=409, detail="Changes changed since you opened this diff — refresh and retry")
    ok, err = _apply_to_index(root, _filter_hunks(diff, body.hunks), reverse=False)
    if not ok:
        logger.error("stage-hunks apply failed: repo=%s path=%s: %s", root, rel, err)
        raise HTTPException(status_code=400 if err == "empty patch" else 409,
                            detail="No unstaged changes to stage" if err == "empty patch"
                            else f"Could not stage hunks: {err}") from None
    logger.info("stage-hunks: staged hunks %s of %s in %s", body.hunks, rel, root)
    return {"ok": True, "whole_file": False}


@router.post("/git/unstage-hunks")
async def git_unstage_hunks(body: HunksIn) -> dict:
    """Unstage the selected hunks of a file's staged changes (reject HEAD→index hunks)."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    if not body.hunks:
        raise HTTPException(status_code=400, detail="No hunks selected")
    paths = _safe_paths(root, [body.path])
    rel = paths[0]
    try:
        status = _git(root, "status", "--porcelain=v1", "-z", "--", rel).stdout
        if status.startswith("??"):
            raise HTTPException(status_code=400, detail="File is not staged")
        diff = _git(root, "diff", "--cached", "--unified=3", "--", rel).stdout
    except GitError as exc:
        logger.error("unstage-hunks failed: repo=%s path=%s: %s", root, rel, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if body.total and body.total != _hunk_count(diff):
        raise HTTPException(status_code=409, detail="Changes changed since you opened this diff — refresh and retry")
    ok, err = _apply_to_index(root, _filter_hunks(diff, body.hunks), reverse=True)
    if not ok:
        logger.error("unstage-hunks apply failed: repo=%s path=%s: %s", root, rel, err)
        raise HTTPException(status_code=400 if err == "empty patch" else 409,
                            detail="No staged changes to unstage" if err == "empty patch"
                            else f"Could not unstage hunks: {err}") from None
    logger.info("unstage-hunks: unstaged hunks %s of %s in %s", body.hunks, rel, root)
    return {"ok": True, "whole_file": False}


# ── Project content search ──────────────────────────────────────────────────
#
# VS Code-style "search in files": case-insensitive (or sensitive) substring
# search, optional regex, walking the project tree in a worker thread. Heavy
# build/vendor directories are skipped and only reasonably sized plain-text
# files are read, so a search on a NAS share cannot stall the event loop.

SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".cache",
    "dist", "build", "target", ".next", ".nuxt", ".gradle", ".idea",
    ".vscode", "vendor", "coverage", ".parcel-cache",
}
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_FILES = 5000
MAX_RESULT_LINE_CHARS = 400


def _content_search(root: Path, q: str, *, regex: bool, case_sensitive: bool,
                    max_results: int) -> tuple[list[dict], bool, int]:
    """Search file contents under root; returns (results, truncated, files_scanned)."""
    if regex:
        try:
            pattern = re.compile(q, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
    else:
        needle = q if case_sensitive else q.lower()

    results: list[dict] = []
    truncated = False
    files_scanned = 0
    stop = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SEARCH_SKIP_DIRS and not d.startswith(".git")
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError as exc:
                logger.warning("project search: cannot stat %s: %s", full, exc)
                continue
            if not os.path.isfile(full) or st.st_size > MAX_SEARCH_FILE_BYTES:
                continue
            files_scanned += 1
            if files_scanned > MAX_SEARCH_FILES:
                truncated = stop = True
                break
            try:
                with open(full, "rb") as fh:
                    data = fh.read(MAX_SEARCH_FILE_BYTES)
            except OSError as exc:
                logger.warning("project search: cannot read %s: %s", full, exc)
                continue
            if b"\x00" in data[:8192]:
                continue  # binary — not text
            text = data.decode("utf-8", errors="replace")
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex:
                    match = pattern.search(line)
                    if not match:
                        continue
                    column = match.start() + 1
                else:
                    haystack = line if case_sensitive else line.lower()
                    pos = haystack.find(needle)
                    if pos < 0:
                        continue
                    column = pos + 1
                results.append({"path": rel, "line": lineno, "column": column,
                                "text": line[:MAX_RESULT_LINE_CHARS]})
                if len(results) >= max_results:
                    truncated = stop = True
                    break
            if stop:
                break
        if stop:
            break
    return results, truncated, files_scanned


@router.get("/search")
async def project_search(repo: str = Query(...),
                         q: str = Query(..., min_length=1, max_length=200),
                         regex: bool = Query(default=False),
                         case_sensitive: bool = Query(default=False),
                         max_results: int = Query(default=200, ge=1, le=1000)) -> dict:
    """Search file contents inside a project folder (VS Code-style)."""
    root = _repo_root(repo)
    try:
        results, truncated, files_scanned = await asyncio.to_thread(
            _content_search, root, q, regex=regex,
            case_sensitive=case_sensitive, max_results=max_results)
    except ValueError as exc:
        logger.warning("project search rejected: repo=%s q=%r: %s", root, q, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.error("project search failed: repo=%s q=%r: %s", root, q, exc)
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}") from exc
    logger.info("project search: repo=%s q=%r → %d hit(s) in %d file(s)%s",
                root, q, len(results), files_scanned, " (truncated)" if truncated else "")
    return {"query": q, "results": results, "truncated": truncated,
            "files_scanned": files_scanned}


# ── Go to Definition ────────────────────────────────────────────────────────
#
# No language server exists on the NAS, so "definition" is resolved with a
# project-wide declaration search: one combined regex finds `namespace` /
# `package` / type / function / constant declarations for the symbol, and the
# hits are ranked (qualified namespace match > type > function > variable,
# then files named after the symbol, then files other than the one we came
# from). This is the same fallback VS Code uses without an LSP — precise
# enough for `using WawaLocal.Infrastructure.Persistence;` → `namespace …`.
#
# A *linter* is not involved: lint/syntax problems are a separate feature
# (POST /api/editor/check + the PROBLEMS panel).

DEFINITION_KIND_RANK = {"namespace": 0, "package": 0, "type": 1, "function": 2, "variable": 3}


def _definition_patterns(qualified: str, name: str) -> list[tuple[str, str, bool]]:
    """(kind, regex, matched_full_qualified_name) — most specific first."""
    q, n = re.escape(qualified), re.escape(name)
    return [
        ("namespace", rf"\bnamespace\s+{q}\b", True),
        ("namespace", rf"\bpackage\s+{q}\b", True),
        ("namespace", rf"\b(?:namespace|package)\s+{n}\b", False),
        ("type", rf"\b(?:class|interface|struct|enum|record|trait|protocol|object|module)\s+{n}\b", False),
        ("type", rf"\btype\s+{n}\b", False),
        ("function", rf"\b(?:def|function|func|fn|fun|sub)\s+{n}\b", False),
        ("variable", rf"\b(?:const|let|var|val|readonly|final)\s+{n}\b", False),
    ]


def _definition_kind(line: str, patterns: list[tuple[str, str, bool]]) -> tuple[str, bool] | None:
    for kind, pattern, qualified in patterns:
        if re.search(pattern, line):
            return kind, qualified
    return None


class DefinitionIn(BaseModel):
    repo: str = Field(min_length=1, max_length=4096)
    symbol: str = Field(min_length=1, max_length=512)
    # File the jump starts from (only used for ranking), e.g. /home/me/p/Program.cs
    from_path: str | None = Field(default=None, max_length=4096)


@router.post("/definition")
async def find_definition(body: DefinitionIn) -> dict:
    """Resolve a symbol to its declaration in the project (Ctrl+Click / F12)."""
    root = _repo_root(body.repo)
    qualified = re.sub(r"[^\w.$]", "", body.symbol or "").strip(". ")
    if not qualified:
        raise HTTPException(status_code=400, detail="No symbol to resolve")
    name = qualified.rsplit(".", 1)[-1]
    patterns = _definition_patterns(qualified, name)
    combined = "|".join(f"(?P<g{i}>{pat})" for i, (_, pat, _q) in enumerate(patterns))
    try:
        hits, truncated, scanned = await asyncio.to_thread(
            _content_search, root, combined, regex=True, case_sensitive=True, max_results=300)
    except ValueError as exc:
        logger.warning("definition: bad pattern for %r: %s", qualified, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.error("definition search failed: repo=%s symbol=%r: %s", root, qualified, exc)
        raise HTTPException(status_code=500, detail=f"Definition search failed: {exc}") from exc

    from_rel = ""
    if body.from_path:
        try:
            from_rel = os.path.relpath(path_scope.sanitize(body.from_path), root).replace(os.sep, "/")
        except (ValueError, OSError):
            from_rel = ""

    name_l = name.lower()
    ranked: list[tuple[int, dict]] = []
    for hit in hits:
        kind = _definition_kind(hit["text"], patterns)
        if not kind:
            continue
        kind_name, full_name = kind
        score = DEFINITION_KIND_RANK.get(kind_name, 9) + (0 if full_name else 1)
        stem = os.path.splitext(os.path.basename(hit["path"]))[0].lower()
        if stem == name_l:
            score -= 3
        elif name_l in hit["path"].lower():
            score -= 1
        if hit["path"] == from_rel:
            score += 1        # prefer the declaration elsewhere over a local mention
        ranked.append((score, {**hit, "kind": kind_name}))
    ranked.sort(key=lambda t: (t[0], t[1]["path"], t[1]["line"]))

    candidates = [h for _s, h in ranked[:10]]
    if not candidates:
        logger.info("definition: %r not found in %s (%d file(s) scanned)", qualified, root, scanned)
        return {"found": False, "symbol": qualified, "candidates": [],
                "truncated": truncated, "files_scanned": scanned}
    best = candidates[0]
    logger.info("definition: %r → %s:%d (%s)", qualified, best["path"], best["line"], best["kind"])
    return {
        "found": True,
        "symbol": qualified,
        "path": best["path"],
        "line": best["line"],
        "column": best["column"],
        "kind": best["kind"],
        "candidates": candidates,
        "truncated": truncated,
        "files_scanned": scanned,
    }


class RepoIn(BaseModel):
    repo: str


@router.post("/git/pull")
async def git_pull(body: RepoIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    try:
        # git pull/fetch talks to the network — running it on the event loop
        # froze every other request in the UI until it finished.
        r = await asyncio.to_thread(
            subprocess.run, ["git", *GIT_TRUST_ARGS, "pull"], cwd=str(root),
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S * 2, env=_git_env(),
        )
        tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""
        return {"ok": r.returncode == 0, "detail": tail[:300]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"pull timed out after {GIT_TIMEOUT_S * 2}s"}


@router.post("/git/push")
async def git_push(body: RepoIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["git", *GIT_TRUST_ARGS, "push"], cwd=str(root),
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S * 2, env=_git_env(),
        )
        if r.returncode != 0 and "upstream" in (r.stderr or ""):
            # No upstream yet — auto-setup like VS Code does: with a single
            # remote configured, push the current branch to it.
            remotes = _git(root, "remote").stdout.split()
            if len(remotes) == 1:
                branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
                r = await asyncio.to_thread(
                    subprocess.run,
                    ["git", *GIT_TRUST_ARGS, "push", "-u", remotes[0], branch],
                    cwd=str(root), capture_output=True, text=True,
                    timeout=GIT_TIMEOUT_S * 2, env=_git_env(),
                )
        tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""
        return {"ok": r.returncode == 0, "detail": tail[:300]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"push timed out after {GIT_TIMEOUT_S * 2}s"}


# ── Pro git: clone / fetch / branches / remotes / stash / discard / merge ───

NET_TIMEOUT_S = 60
CLONE_TIMEOUT_S = 300
_MERGE_TIMEOUT_S = 120

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_branch_name(root: Path, name: str) -> None:
    """Validate a branch name with git itself (check-ref-format)."""
    if not name or name.startswith("-") or "\x00" in name or "\n" in name or ".." in name:
        raise HTTPException(status_code=400, detail=f"Invalid branch name: {name!r}")
    try:
        r = subprocess.run(["git", *GIT_TRUST_ARGS, "check-ref-format", "--branch", name],
                           cwd=str(root), capture_output=True, text=True, timeout=10, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail=f"check-ref-format failed: {exc}") from exc
    if r.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Invalid branch name: {name!r}")


def _check_remote_name(name: str) -> None:
    if not _REMOTE_NAME_RE.match(name) or name.endswith(".lock") or ".." in name:
        raise HTTPException(status_code=400, detail=f"Invalid remote name: {name!r}")


def _check_url(url: str) -> None:
    if not url or "\x00" in url or "\n" in url or "\r" in url:
        raise HTTPException(status_code=400, detail="Invalid remote URL")


async def _git_net(root: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a long-running git command (network/merge) in a worker thread.

    Keeps the event loop responsive while fetch/merge/clone can take minutes.
    """
    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(["git", *GIT_TRUST_ARGS, *args], cwd=str(root),
                              capture_output=True, text=True, timeout=timeout, env=_git_env())
    try:
        return await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitError(str(exc)) from exc


class CloneIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    path: str = Field(min_length=1, max_length=4096)
    branch: str = ""


@router.post("/git/clone")
async def git_clone(request: Request, body: CloneIn) -> dict:
    """Clone a remote repository into a new directory and add it to recents."""
    _check_url(body.url)
    path = _expand_request_path(request, body.path)
    if not path or path == "/":
        raise HTTPException(status_code=400, detail="Invalid clone path")
    if os.path.exists(path):
        raise HTTPException(status_code=409, detail="Path already exists")
    parent = os.path.dirname(path.rstrip("/"))
    if not os.path.isdir(parent):
        raise HTTPException(status_code=400, detail="Parent folder does not exist")
    args = ["clone"]
    if body.branch:
        _check_branch_name(Path(parent), body.branch)
        args += ["-b", body.branch]
    args += [body.url, path]
    try:
        r = await _git_net(Path(parent), args, CLONE_TIMEOUT_S)
    except GitError as exc:
        logger.error("git clone failed: url=%s path=%s: %s", body.url, path, exc)
        await asyncio.to_thread(shutil.rmtree, path, True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else f"exit {r.returncode}"
        logger.error("git clone failed: url=%s path=%s: %s", body.url, path, detail[:300])
        # A half-cloned tree can be large; remove it off the event loop.
        await asyncio.to_thread(shutil.rmtree, path, True)
        return {"ok": False, "path": path, "detail": detail[:300]}
    with _LOCK:
        recents = [r for r in _load_recents() if r["path"] != path]
        recents.insert(0, {
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or "/",
            "opened_at": datetime.now(UTC).isoformat(),
        })
        recents = recents[:MAX_RECENTS]
        _save_recents(recents)
    logger.info("git clone ok: url=%s → %s", body.url, path)
    return {"ok": True, "path": path, "detail": "cloned"}


@router.post("/git/fetch")
async def git_fetch(body: RepoIn) -> dict:
    """Fetch all remotes (updates remote-tracking branches + ahead/behind)."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    try:
        r = await _git_net(root, ["fetch", "--all"], NET_TIMEOUT_S)
        tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "up to date"
        return {"ok": r.returncode == 0, "detail": tail[:300]}
    except GitError as exc:
        logger.error("git fetch failed: repo=%s: %s", root, exc)
        return {"ok": False, "detail": str(exc)}


class BranchIn(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=255)
    check_out: bool = False


@router.get("/git/branches")
async def git_branches(repo: str = Query(...)) -> dict:
    root = _repo_root(repo)
    if not _is_repo_root(root):
        return {"current": None, "branches": [], "remote_branches": []}
    try:
        out = _git(root, "branch", "--format=%(refname:short)%00%(upstream:short)%00%(objectname:short)%00%(subject)").stdout
        branches = []
        for line in out.splitlines():
            parts = line.split("\x00")
            if len(parts) >= 4:
                branches.append({"name": parts[0], "upstream": parts[1] or None,
                                 "hash": parts[2], "subject": parts[3]})
        r = _git(root, "branch", "-r", "--format=%(refname:short)").stdout
        remote_branches = [ln.strip() for ln in r.splitlines()
                           if ln.strip() and not ln.strip().startswith("HEAD")]
        current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    except GitError as exc:
        logger.error("git branches failed: repo=%s: %s", root, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"current": current, "branches": branches, "remote_branches": remote_branches}


@router.post("/git/branch")
async def git_create_branch(body: BranchIn) -> dict:
    """Create a branch from HEAD; optionally switch to it."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_branch_name(root, body.name)
    try:
        _git(root, "branch", body.name)
        if body.check_out:
            _git(root, "switch", body.name)
    except GitError as exc:
        logger.error("git branch create failed: repo=%s name=%s: %s", root, body.name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git branch created: %s in %s (check_out=%s)", body.name, root, body.check_out)
    return {"ok": True, "branch": body.name}


class CheckoutIn(BaseModel):
    repo: str
    ref: str = Field(min_length=1, max_length=4096)


@router.post("/git/checkout")
async def git_checkout(body: CheckoutIn) -> dict:
    """Switch branch. A remote-tracking ref (origin/x) creates/uses local x."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    ref = body.ref.strip()
    if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref or ".." in ref:
        raise HTTPException(status_code=400, detail=f"Invalid ref: {ref!r}")
    try:
        is_remote_ref = _git(root, "rev-parse", "--verify", "--quiet",
                             f"refs/remotes/{ref}^{{commit}}", check=False).returncode == 0
        if is_remote_ref:
            local = ref.split("/", 1)[1]
            has_local = _git(root, "rev-parse", "--verify", "--quiet",
                             f"refs/heads/{local}^{{commit}}", check=False).returncode == 0
            if has_local:
                _git(root, "switch", local)
            else:
                _git(root, "switch", "-c", local, ref)
        else:
            _git(root, "switch", ref)
        current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or None
    except GitError as exc:
        logger.error("git checkout failed: repo=%s ref=%s: %s", root, ref, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git checkout: %s → %s in %s", ref, current, root)
    return {"ok": True, "branch": current}


class BranchDeleteIn(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=255)


@router.post("/git/branch/delete")
async def git_delete_branch(body: BranchDeleteIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if body.name == current:
        raise HTTPException(status_code=400, detail="Cannot delete the current branch")
    try:
        _git(root, "branch", "-d", body.name)
    except GitError as exc:
        logger.error("git branch delete failed: repo=%s name=%s: %s", root, body.name, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("git branch deleted: %s in %s", body.name, root)
    return {"ok": True}


class BranchRenameIn(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=255)
    new_name: str = Field(min_length=1, max_length=255)


@router.post("/git/branch/rename")
async def git_rename_branch(body: BranchRenameIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_branch_name(root, body.new_name)
    try:
        _git(root, "branch", "-m", body.name, body.new_name)
    except GitError as exc:
        logger.error("git branch rename failed: repo=%s %s → %s: %s", root, body.name, body.new_name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git branch renamed: %s → %s in %s", body.name, body.new_name, root)
    return {"ok": True, "branch": body.new_name}


class RemoteIn(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)


@router.get("/git/remotes")
async def git_remotes(repo: str = Query(...)) -> dict:
    root = _repo_root(repo)
    if not _is_repo_root(root):
        return {"remotes": []}
    try:
        # `git remote -v` lines are `<name>\t<url> (fetch|push)` — one tab,
        # the kind is a space-suffixed tag on the URL.
        out = _git(root, "remote", "-v").stdout
        remotes: dict[str, dict] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, url_and_kind = parts
            entry = remotes.setdefault(name, {"name": name, "fetch_url": None, "push_url": None})
            if url_and_kind.endswith(" (fetch)"):
                entry["fetch_url"] = url_and_kind[:-len(" (fetch)")]
            elif url_and_kind.endswith(" (push)"):
                entry["push_url"] = url_and_kind[:-len(" (push)")]
    except GitError as exc:
        logger.error("git remotes failed: repo=%s: %s", root, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"remotes": list(remotes.values())}


@router.post("/git/remote/add")
async def git_remote_add(body: RemoteIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_remote_name(body.name)
    _check_url(body.url)
    try:
        _git(root, "remote", "add", body.name, body.url)
    except GitError as exc:
        logger.error("git remote add failed: repo=%s name=%s: %s", root, body.name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git remote added: %s → %s in %s", body.name, body.url, root)
    return {"ok": True}


class RemoteNameIn(BaseModel):
    repo: str
    name: str = Field(min_length=1, max_length=64)


@router.post("/git/remote/remove")
async def git_remote_remove(body: RemoteNameIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_remote_name(body.name)
    try:
        _git(root, "remote", "remove", body.name)
    except GitError as exc:
        logger.error("git remote remove failed: repo=%s name=%s: %s", root, body.name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git remote removed: %s in %s", body.name, root)
    return {"ok": True}


@router.post("/git/remote/set-url")
async def git_remote_set_url(body: RemoteIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_remote_name(body.name)
    _check_url(body.url)
    try:
        _git(root, "remote", "set-url", body.name, body.url)
    except GitError as exc:
        logger.error("git remote set-url failed: repo=%s name=%s: %s", root, body.name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git remote url set: %s → %s in %s", body.name, body.url, root)
    return {"ok": True}


class UpstreamIn(BaseModel):
    repo: str
    remote: str = Field(min_length=1, max_length=64)
    branch: str = ""


@router.post("/git/upstream")
async def git_set_upstream(body: UpstreamIn) -> dict:
    """Track remote/branch for the current (or given) local branch."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_remote_name(body.remote)
    target = body.branch or _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    try:
        _git(root, "branch", "--set-upstream-to", f"{body.remote}/{target}", target)
    except GitError as exc:
        logger.error("git set-upstream failed: repo=%s remote=%s branch=%s: %s", root, body.remote, target, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("git upstream set: %s → %s/%s in %s", target, body.remote, target, root)
    return {"ok": True}


class StashIn(BaseModel):
    repo: str
    message: str = ""
    include_untracked: bool = False


class StashRefIn(BaseModel):
    repo: str
    index: int = Field(ge=0, le=100000)


@router.get("/git/stash")
async def git_stash_list(repo: str = Query(...)) -> dict:
    root = _repo_root(repo)
    if not _is_repo_root(root):
        return {"stashes": []}
    try:
        out = _git(root, "stash", "list", "--format=%gd%x1f%cs%x1f%gs").stdout
        stashes = []
        for i, line in enumerate(out.splitlines()):
            parts = line.split("\x1f")
            if len(parts) == 3:
                stashes.append({"index": i, "ref": parts[0], "date": parts[1], "message": parts[2]})
    except GitError as exc:
        logger.error("git stash list failed: repo=%s: %s", root, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"stashes": stashes}


@router.post("/git/stash")
async def git_stash_push(body: StashIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    args = ["stash", "push"]
    if body.include_untracked:
        args.append("--include-untracked")
    if body.message:
        args += ["-m", body.message]
    try:
        r = _git(root, *args)
        out = (r.stdout or "").strip()
        detail = out.splitlines()[-1] if out else "stashed"
    except GitError as exc:
        logger.error("git stash push failed: repo=%s: %s", root, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git stash push: repo=%s untracked=%s: %s", root, body.include_untracked, detail[:200])
    return {"ok": True, "detail": detail[:300]}


@router.post("/git/stash/apply")
async def git_stash_apply(body: StashRefIn) -> dict:
    return await _stash_op(body, "apply")


@router.post("/git/stash/pop")
async def git_stash_pop(body: StashRefIn) -> dict:
    return await _stash_op(body, "pop")


@router.post("/git/stash/drop")
async def git_stash_drop(body: StashRefIn) -> dict:
    return await _stash_op(body, "drop")


async def _stash_op(body: StashRefIn, op: str) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    ref = f"stash@{{{body.index}}}"
    try:
        _git(root, "stash", op, ref)
    except GitError as exc:
        logger.error("git stash %s failed: repo=%s ref=%s: %s", op, root, ref, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("git stash %s: %s in %s", op, ref, root)
    return {"ok": True}


@router.post("/git/stash/clear")
async def git_stash_clear(body: RepoIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    try:
        _git(root, "stash", "clear")
    except GitError as exc:
        logger.error("git stash clear failed: repo=%s: %s", root, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git stash cleared: repo=%s", root)
    return {"ok": True}


@router.post("/git/discard")
async def git_discard(body: PathsIn) -> dict:
    """Discard changes to the given paths — restore them to their HEAD state.

    Tracked changes → `git checkout HEAD --` (index + worktree); staged
    additions → `git rm -f --`; untracked → `git clean -f --`. Unmerged
    paths are refused — conflicts must be resolved by hand.
    """
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    paths = _safe_paths(root, body.paths)
    try:
        out = _git(root, "status", "--porcelain=v1", "-z", "--", *paths).stdout
        untracked, added, tracked, unmerged = [], [], [], []
        for rec in out.split("\x00"):
            if len(rec) < 4 or rec[0] not in _STATUS_CHARS or rec[1] not in _STATUS_CHARS:
                continue
            st, p = rec[:2], rec[3:]
            if st == "??":
                untracked.append(p)
            elif "U" in st:
                unmerged.append(p)
            elif st[0] == "A":
                added.append(p)
            else:
                tracked.append(p)
        if unmerged:
            raise HTTPException(status_code=400,
                                detail=f"Unmerged paths — resolve conflicts first: {', '.join(unmerged)}")
        if tracked:
            _git(root, "checkout", "HEAD", "--", *tracked)
        if added:
            _git(root, "rm", "-f", "--", *added)
        if untracked:
            _git(root, "clean", "-f", "--", *untracked)
    except HTTPException:
        raise
    except GitError as exc:
        logger.error("git discard failed: repo=%s paths=%s: %s", root, paths, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("git discard: %d path(s) in %s (tracked=%d added=%d untracked=%d)",
                len(paths), root, len(tracked), len(added), len(untracked))
    return {"ok": True, "discarded": len(tracked) + len(added) + len(untracked)}


class MergeIn(BaseModel):
    repo: str
    branch: str = Field(min_length=1, max_length=4096)


@router.post("/git/merge")
async def git_merge(body: MergeIn) -> dict:
    """Merge branch into the current one. Conflicts → ok:false + merge state."""
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    _check_branch_name(root, body.branch)
    try:
        r = await _git_net(root, ["merge", body.branch], _MERGE_TIMEOUT_S)
        tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""
        return {"ok": r.returncode == 0, "detail": tail[:300]}
    except GitError as exc:
        logger.error("git merge failed: repo=%s branch=%s: %s", root, body.branch, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/git/merge/abort")
async def git_merge_abort(body: RepoIn) -> dict:
    root = _repo_root(body.repo)
    if not _is_repo_root(root):
        raise HTTPException(status_code=400, detail="Not a git repository")
    try:
        r = _git(root, "merge", "--abort")
        tail = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else "aborted"
        return {"ok": True, "detail": tail[:300]}
    except GitError as exc:
        logger.error("git merge abort failed: repo=%s: %s", root, exc)
        return {"ok": False, "detail": str(exc)}

