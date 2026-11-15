"""Regression test — editor git integration for user-owned repos (T-0011).

The production server runs as the dedicated `ethos-nas` service user. Since git
>= 2.35.3, git refuses to operate on a repository owned by a different user
("detected dubious ownership"), so the editor's git endpoints report
is_repo:false for repos owned by the login user even though the service user
has full read/write access via group membership. See
docs/editor-git-cross-user.md for the design decision.

Part A (always runs): the interim script setup_scripts/enable_editor_git.sh
exists, is syntactically valid, and (run in non-root test mode via the
ETHOS_GITCONFIG override) writes a correct [safe] section, is idempotent, and
rejects relative paths.

Part B (needs fakeroot, auto-skips otherwise): proves the mechanism end to end
on a temp repo — with the owner faked to another uid, bare git fails with
"dubious ownership"; with the script-generated config (as GIT_CONFIG_GLOBAL)
and with `git -c safe.directory=*` (the planned app-level fix) it works.

Part C (state probe of the live server, informational): reports whether the
limitation is currently active on this deployment. It FAILs only in the
regression case: the service gitconfig exists with a safe.directory entry
covering the repo, yet git still reports is_repo:false.

Part D (needs fakeroot, auto-skips otherwise): app-level regression check —
imports backend.routers.editor in a fakeroot child where a temp repo is
faked to be owned by uid 994, and asserts _is_repo() is True and _git()
succeeds. This fails while editor.py lacks `safe.directory` and passes with
the app-level fix (T-0014).

Run:  .venv/bin/python tests/test_editor_git_cross_user.py
      ETHOS_TEST_BASE=http://127.0.0.1:9001 to target a non-default port.
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
if not BASE.startswith(("http://", "https://")):
    raise SystemExit(f"ETHOS_TEST_BASE must be an http(s) URL, got {BASE!r}")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
# Unattended T3 runs have no credential in env; default matches the rest of the
# suite (test_editor.py et al.). Override with ETHOS_TEST_PASSWORD when set.
PASS = _require_test_password()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ethos-nas repo root
SCRIPT = os.path.join(REPO, "setup_scripts", "enable_editor_git.sh")
SERVICE_GITCONFIG = os.environ.get("ETHOS_SERVICE_GITCONFIG", "/home/ethos-nas/.gitconfig")

failures = 0


def check(name, ok, detail=""):
    global failures
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1
    return bool(ok)


def api(method, path, token=None, body=None):
    url = BASE + path
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"blocked non-http(s) URL: {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
            return e.code, parsed if isinstance(parsed, dict) else {"_raw": str(parsed)}
        except json.JSONDecodeError:
            return e.code, {"_raw": raw}
    except Exception as e:  # noqa: BLE001 — connection refused etc.
        return 0, {"_raw": str(e)}


def run(cmd, env=None, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, timeout=60)

def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


# ── Part A: interim script mechanics ────────────────────────────────────────

def part_a():
    print("\nPart A — setup_scripts/enable_editor_git.sh mechanics")
    if not check("script exists", os.path.isfile(SCRIPT), SCRIPT):
        return
    check("script is executable", os.access(SCRIPT, os.X_OK))
    check("bash -n (syntax)", run(["bash", "-n", SCRIPT]).returncode == 0)

    tmp = tempfile.mkdtemp(prefix="ethos-gitcfg-")
    try:
        cfg = os.path.join(tmp, ".gitconfig")
        env = dict(os.environ, ETHOS_GITCONFIG=cfg)
        r1 = run(["bash", SCRIPT, "--all"], env=env)
        check("run --all (non-root test mode)", r1.returncode == 0, r1.stderr.strip()[:200])
        check("config has [safe] section", "[safe]" in _read(cfg))
        check("config has directory = *", "directory = *" in _read(cfg))
        check("config is mode 600", oct(os.stat(cfg).st_mode & 0o777) == "0o600")

        before = _read(cfg)
        r2 = run(["bash", SCRIPT, "--all"], env=env)
        check("re-run is idempotent", r2.returncode == 0 and _read(cfg) == before,
              r2.stderr.strip()[:200])

        r3 = run(["bash", SCRIPT, "relative/path"], env=env)
        check("relative path rejected", r3.returncode != 0)
        r4 = run(["bash", SCRIPT, "/nonexistent/repo/path"], env=env)
        check("nonexistent dir rejected", r4.returncode != 0)

        # per-repo entry: the real repo path, canonicalized into the config
        r5 = run(["bash", SCRIPT, REPO], env=env)
        cfg_text = _read(cfg)
        check("per-repo entry added", r5.returncode == 0 and f"directory = {REPO}" in cfg_text,
              r5.stderr.strip()[:200])

        # Part B consumes the --all config if fakeroot is available
        part_b(cfg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Part B: cross-user mechanism (fakeroot) ─────────────────────────────────

def part_b(cfg_all):
    if shutil.which("fakeroot") is None:
        print("\nPart B — cross-user mechanism: SKIP (fakeroot not installed)")
        return
    print("\nPart B — cross-user mechanism (fakeroot: repo owner faked to uid 994)")
    tmp = tempfile.mkdtemp(prefix="ethos-duo-")
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        for cmd in (
            ["git", "init", "-q", "."],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            run(cmd, cwd=repo)
        with open(os.path.join(repo, "f.txt"), "w") as f:
            f.write("hi\n")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-qm", "init"], cwd=repo)

        # baseline: same-user git works
        r = run(["git", "-C", repo, "status"])
        check("baseline same-user git works", r.returncode == 0, r.stderr.strip()[:120])

        # cross-user, no config → blocked
        r = run(["fakeroot", "sh", "-c",
                 f"chown -R 994:994 {repo}; git -C {repo} status"])
        check("cross-user without config is blocked",
              r.returncode != 0 and "dubious ownership" in r.stderr, r.stderr.strip()[:120])

        # cross-user with the script-generated config → works
        r = run(["fakeroot", "sh", "-c",
                 f"chown -R 994:994 {repo}; env GIT_CONFIG_GLOBAL={cfg_all} git -C {repo} status"])
        check("script-generated config unblocks cross-user git",
              r.returncode == 0, (r.stderr or r.stdout).strip()[:120])

        # cross-user with -c safe.directory=* (the planned app-level fix) → works
        r = run(["fakeroot", "sh", "-c",
                 f"chown -R 994:994 {repo}; git -c safe.directory=* -C {repo} status"])
        check("-c safe.directory=* unblocks (validates planned editor.py fix)",
              r.returncode == 0, (r.stderr or r.stdout).strip()[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Part C: live-server state probe ─────────────────────────────────────────

def part_c():
    print(f"\nPart C — live server state probe ({BASE})")
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict) or "token" not in data:
        print(f"  [SKIP] cannot reach {BASE} or login failed (code={code}) — probe skipped")
        return
    token = data["token"]
    q = urllib.parse.quote(REPO, safe="")
    code, data = api("GET", f"/api/editor/git/status?repo={q}", token=token)
    if code != 200 or not isinstance(data, dict):
        print(f"  [SKIP] git/status probe failed (code={code}) — probe skipped")
        return
    is_repo = data.get("is_repo")
    has_cfg = os.path.isfile(SERVICE_GITCONFIG)
    covers = False
    if has_cfg:
        text = _read(SERVICE_GITCONFIG)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("directory"):
                val = line.split("=", 1)[1].strip() if "=" in line else ""
                if val in ("*", REPO):
                    covers = True
    if is_repo:
        check("git integration active for user-owned repo", True,
              f"branch={data.get('branch')} files={len(data.get('files', []))}")
    elif covers:
        check("git integration active", False,
              f"{SERVICE_GITCONFIG} has a safe.directory entry covering {REPO} but "
              f"is_repo is still false — real regression (git version? config parse?)")
    else:
        note = "known limitation ACTIVE (T-0011): service user blocked by git dubious-ownership"
        if has_cfg:
            note += f"; {SERVICE_GITCONFIG} exists but does not cover this repo"
        else:
            note += f"; {SERVICE_GITCONFIG} absent"
        print(f"  [INFO] {note}")
        print("         unblock now: sudo bash setup_scripts/enable_editor_git.sh --all")
        print("         (no service restart needed), or wait for the app-level fix in editor.py")


def part_d():
    if shutil.which("fakeroot") is None:
        print("\nPart D — app-level cross-user (editor._is_repo/_git): SKIP (fakeroot not installed)")
        return
    print("\nPart D — app-level cross-user (editor.py under fakeroot, repo owner faked to uid 994)")
    tmp = tempfile.mkdtemp(prefix="ethos-appduo-")
    try:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        for cmd in (
            ["git", "init", "-q", "."],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            run(cmd, cwd=repo)
        with open(os.path.join(repo, "f.txt"), "w") as f:
            f.write("hi\n")
        run(["git", "add", "f.txt"], cwd=repo)
        run(["git", "commit", "-qm", "init"], cwd=repo)

        script = os.path.join(tmp, "child.py")
        with open(script, "w") as f:
            f.write(
                "from pathlib import Path\n"
                "from backend.routers.editor import _is_repo, _git\n"
                f"repo = Path({repo!r})\n"
                "print('IS_REPO', str(_is_repo(repo)).lower())\n"
                "try:\n"
                "    r = _git(repo, 'status', '--porcelain=v1')\n"
                "    print('GIT_STATUS', r.returncode)\n"
                "except Exception as e:\n"
                "    print('GIT_STATUS', 'error', str(e)[:120])\n"
            )
        # PYTHONPATH: python puts the *script* dir on sys.path, not the cwd, so
        # point it at the repo root for the `backend` import.
        r = run(["fakeroot", "sh", "-c",
                 f"chown -R 994:994 {repo}; exec env PYTHONPATH={REPO} {sys.executable} {script}"],
                cwd=REPO)
        out = r.stdout
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        detail = " | ".join(lines) or f"rc={r.returncode} {r.stderr.strip()[:200]}"
        print(f"    child: {detail}")
        check("editor._is_repo() is True for foreign-owned repo (app-level fix)",
              "IS_REPO true" in out, detail)
        check("editor._git() status succeeds for foreign-owned repo",
              "GIT_STATUS 0" in lines, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    print(f"editor git cross-user test (T-0011) against {BASE}")
    part_a()
    part_c()
    part_d()
    print(f"\n{'=' * 50}\nRESULT: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
