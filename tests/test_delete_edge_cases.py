"""Delete/trash edge cases — symlinks, permission failures, partial trees, batch ops.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret) and mints an admin token, so no PAM login is needed. Exercised paths:

  * move-to-trash of a dangling symlink and of a symlink to a directory
  * hard delete of a symlink to a directory (never follows the link)
  * read-only parent: trash reports a reason, file stays on disk
  * partially deletable tree: surviving entries are reported, not silently lost
  * batch trash op: op_id, progress fields, not_found reason
  * trash purge op (DELETE /trash/items) and its 403 for a foreign owner

Everything lives in a temp directory; the server is terminated at the end.

Run:  .venv/bin/python tests/test_delete_edge_cases.py
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "delete-edge-cases-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(username: str = "delprobe", role: str = "admin") -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": username, "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def call(base: str, method: str, path: str, token: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except ValueError:
            return exc.code, {"_raw": raw[:200].decode("utf-8", "replace")}
    except OSError as exc:
        return 0, {"_error": str(exc)}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_op(base: str, token: str, op_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        _, snap = call(base, "GET", f"/api/files/ops/{op_id}", token)
        if snap.get("status") != "running":
            return snap
        time.sleep(0.15)
    return snap


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-del-data-"))
    work = Path(tempfile.mkdtemp(prefix="ethos-del-work-"))
    log_path = data_dir / "server.log"
    log = open(log_path, "w")
    env = dict(os.environ, ETHOS_DATA_DIR=str(data_dir), ETHOS_JWT_SECRET=SECRET,
               ETHOS_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    admin = _token("delprobe", "admin")
    user = _token("deluser", "user")
    ro_dirs: list[Path] = []

    try:
        deadline = time.time() + 40
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if call(base, "GET", "/api/system/status", admin)[0] in (200, 401):
                ready = True
                break
            time.sleep(0.3)
        if not ready:
            print("  [FAIL] isolated server did not start")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1

        def trash_batch(paths: list[str]) -> tuple[int, dict]:
            status, body = call(base, "POST", "/api/files/trash/batch", admin, {"paths": paths})
            if status != 200 or not body.get("op_id"):
                return status, body
            return status, wait_op(base, admin, body["op_id"])

        # ── 1. dangling symlink → trash ─────────────────────────────────────
        dead = work / "dead-link"
        os.symlink(work / "missing-target", dead)
        status, snap = trash_batch([str(dead)])
        check("trash dangling symlink accepts the op", status == 200 and snap.get("status") == "done",
              f"{status} {snap}")
        check("trash dangling symlink removed the entry", not os.path.lexists(dead))
        check("trash dangling symlink counted as success", snap.get("succeeded") == 1, str(snap.get("details")))

        # ── 2. symlink to a directory → trash moves only the link ───────────
        target = work / "target-dir"
        target.mkdir()
        (target / "inside.txt").write_text("keep me", encoding="utf-8")
        link = work / "link-to-dir"
        os.symlink(target, link)
        status, snap = trash_batch([str(link)])
        check("trash symlink-to-dir succeeds", status == 200 and snap.get("succeeded") == 1, str(snap))
        check("trash symlink-to-dir removed the link", not os.path.lexists(link))
        check("trash symlink-to-dir left the target intact",
              (target / "inside.txt").read_text(encoding="utf-8") == "keep me")

        # ── 3. hard delete a symlink to a directory ─────────────────────────
        link2 = work / "link-to-dir-2"
        os.symlink(target, link2)
        status, body = call(base, "DELETE", "/api/files/delete", admin, {"paths": [str(link2)]})
        snap = wait_op(base, admin, body.get("op_id", ""))
        check("hard delete symlink-to-dir succeeds", status == 200 and snap.get("succeeded") == 1,
              f"{status} {snap}")
        check("hard delete symlink-to-dir removed the link", not os.path.lexists(link2))
        check("hard delete symlink-to-dir left the target intact", (target / "inside.txt").is_file())

        # ── 4. hard delete a dangling symlink ───────────────────────────────
        dead2 = work / "dead-link-2"
        os.symlink(work / "nope", dead2)
        status, body = call(base, "DELETE", "/api/files/delete", admin, {"paths": [str(dead2)]})
        snap = wait_op(base, admin, body.get("op_id", ""))
        check("hard delete dangling symlink is not 'not_found'",
              snap.get("succeeded") == 1 and not os.path.lexists(dead2), str(snap.get("details")))

        # ── 5. read-only parent: reason instead of an opaque 500 ────────────
        ro = work / "readonly-dir"
        ro.mkdir()
        locked = ro / "locked.txt"
        locked.write_text("x", encoding="utf-8")
        os.chmod(ro, 0o555)
        ro_dirs.append(ro)
        status, snap = trash_batch([str(locked)])
        detail = (snap.get("details") or [{}])[0]
        check("trash into read-only parent reports a failure", snap.get("failed") == 1, str(snap))
        check("trash permission failure carries a reason",
              "Permission" in str(detail.get("reason", "")) or detail.get("status") == "permission_denied",
              str(detail))
        check("trash permission failure keeps the file", locked.is_file())

        status, body = call(base, "DELETE", "/api/files/delete", admin, {"paths": [str(locked)]})
        snap = wait_op(base, admin, body.get("op_id", ""))
        detail = (snap.get("details") or [{}])[0]
        check("hard delete permission failure reported", snap.get("failed") == 1, str(snap))
        check("hard delete permission failure carries a reason",
              "Permission" in str(detail.get("reason", "")) or detail.get("status") == "permission_denied",
              str(detail))

        # ── 6. partially deletable tree ─────────────────────────────────────
        tree = work / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "ok.txt").write_text("bye", encoding="utf-8")
        (tree / "sub" / "frozen.txt").write_text("stay", encoding="utf-8")
        os.chmod(tree / "sub", 0o555)
        ro_dirs.append(tree / "sub")
        status, body = call(base, "DELETE", "/api/files/delete", admin, {"paths": [str(tree)]})
        snap = wait_op(base, admin, body.get("op_id", ""))
        detail = (snap.get("details") or [{}])[0]
        check("partial tree removal is not a silent success", snap.get("failed") == 1, str(snap))
        check("partial tree names the surviving entry", "partial:" in str(detail.get("status", "")),
              str(detail))
        check("partial tree deleted what it could", not (tree / "ok.txt").exists())
        check("partial tree kept the protected entry", (tree / "sub" / "frozen.txt").is_file())

        # ── 7. mixed batch: one real item, one already gone ─────────────────
        good = work / "good.txt"
        good.write_text("x", encoding="utf-8")
        status, snap = trash_batch([str(good), str(work / "missing.txt")])
        statuses = {d.get("name"): d.get("status") for d in snap.get("details", [])}
        check("batch trash moves the existing item", snap.get("succeeded") == 1, str(snap))
        check("batch trash reports the missing path",
              statuses.get("missing.txt") == "not_found", str(statuses))

        # ── 8. purge op removes only the named trash entries ────────────────
        status, listing = call(base, "GET", "/api/files/trash", admin)
        names = [it["name"] for it in listing.get("items", [])]
        check("trash listing contains the trashed items", bool(names), str(listing))
        if names:
            status, body = call(base, "DELETE", "/api/files/trash/items", admin,
                                {"items": [{"name": names[0]}]})
            snap = wait_op(base, admin, body.get("op_id", ""))
            check("purge op succeeds", status == 200 and snap.get("succeeded") == 1, f"{status} {snap}")
            _, listing = call(base, "GET", "/api/files/trash", admin)
            check("purged item is gone", names[0] not in [it["name"] for it in listing.get("items", [])])

        # ── 9. non-admin cannot purge another account's trash ───────────────
        status, body = call(base, "DELETE", "/api/files/trash/items", user,
                            {"items": [{"name": "anything.txt", "owner": "delprobe"}]})
        check("non-admin purge of a foreign trash is rejected", status == 403, f"{status} {body}")

        # ── 10. empty-all op (admin) ───────────────────────────────────────
        status, body = call(base, "DELETE", "/api/files/trash/all?all=1", admin)
        op_id = body.get("op_id")
        if op_id:
            snap = wait_op(base, admin, op_id)
            check("empty-all op finishes", snap.get("status") == "done", str(snap))
        else:
            check("empty-all on an empty trash returns no op", status == 200, f"{status} {body}")
        _, listing = call(base, "GET", "/api/files/trash?all=1", admin)
        check("trash is empty after empty-all", listing.get("total", 1) == 0, str(listing))
    finally:
        for directory in ro_dirs:
            try:
                os.chmod(directory, 0o755)
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("Failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
