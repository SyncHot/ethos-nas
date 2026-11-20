"""Per-user trash — API isolation & authz regression test.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret), mints three test tokens and proves that one account cannot see,
restore or purge another account's trashed files, while an admin can.

Run:  .venv/bin/python tests/test_trash_multiuser_api.py
"""

import base64
import getpass
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
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "trash-multiuser-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(username: str, role: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": username, "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {}
    except OSError:
        return 0, {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    home = Path.home()
    if not str(home).startswith("/home/"):
        print(f"  [FAIL] this test needs a writable /home (got {home})")
        return 1

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-trash-api-"))
    log_path = data_dir / "server.log"
    log = open(log_path, "w")
    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    tag = uuid.uuid4().hex[:8]
    alice_file = home / f"ethos-trash-alice-{getpass.getuser()}-{tag}.txt"
    alice_file.write_text("alice\n", encoding="utf-8")
    admin_file = home / f"ethos-trash-admin-{getpass.getuser()}-{tag}.txt"
    admin_file.write_text("admin\n", encoding="utf-8")
    name = alice_file.name

    try:
        if not _wait_ready(base, proc, log_path):
            print(log_path.read_text()[-2000:])
            check("isolated instance ready", False)
            return 1

        alice = _token(getpass.getuser(), "user")
        bob = _token("bob", "user")
        admin = _token("trashadmin", "admin")

        # ── Move + list isolation ─────────────────────────────────────────
        st, body = _call(base, "POST", "/api/files/trash", alice, {"path": str(alice_file)})
        check("alice can trash her file", st == 200, f"{st} {body}")
        check("trashed into alice's namespace", body.get("owner") == getpass.getuser(), str(body))
        check("source file moved away", not alice_file.exists())

        st, body = _call(base, "GET", "/api/files/trash", alice)
        check("alice lists her item", st == 200 and body.get("total") == 1, str(body))
        check("item reports its owner",
              (body.get("items") or [{}])[0].get("owner") == getpass.getuser(), str(body))

        st, body = _call(base, "GET", "/api/files/trash", bob)
        check("bob does not see alice's trash", st == 200 and body.get("total") == 0, str(body))

        st, _ = _call(base, "GET", "/api/files/trash?all=1", bob)
        check("non-admin all-users view denied", st == 403, f"got {st}")

        st, body = _call(base, "GET", "/api/files/trash?all=1", admin)
        check("admin all-users view sees the item",
              st == 200 and body.get("total") == 1
              and body["items"][0]["owner"] == getpass.getuser(),
              str(body))

        # ── Restore isolation ─────────────────────────────────────────────
        st, _ = _call(base, "POST", "/api/files/trash/restore", bob, {"name": name})
        check("bob cannot restore alice's item", st == 404, f"got {st}")

        st, _ = _call(base, "POST", f"/api/files/trash/restore?owner={getpass.getuser()}", bob,
                      {"name": name})
        check("bob cannot impersonate alice via owner= param", st == 403, f"got {st}")

        st, body = _call(base, "POST", "/api/files/trash/restore", alice, {"name": name})
        check("alice restores her item", st == 200, f"{st} {body}")
        check("file back at its original path", alice_file.exists())
        check("restore reports the owner", body.get("owner") == getpass.getuser(), str(body))

        # ── Destructive isolation ─────────────────────────────────────────
        st, _ = _call(base, "POST", "/api/files/trash", alice, {"path": str(alice_file)})
        check("re-trash for destructive checks", st == 200)

        st, _ = _call(base, "DELETE", f"/api/files/trash/item/{name}", bob)
        check("bob cannot delete alice's item", st == 404, f"got {st}")

        st, body = _call(base, "DELETE", "/api/files/trash", bob)
        check("bob's empty-trash deletes nothing",
              st == 200 and body.get("deleted_count") == 0, str(body))
        st, body = _call(base, "GET", "/api/files/trash", alice)
        check("alice's item survived bob's empty", body.get("total") == 1, str(body))

        st, body = _call(base, "DELETE", f"/api/files/trash/item/{name}?owner={getpass.getuser()}", bob)
        check("bob cannot pass owner= to delete", st == 403, f"got {st}")

        # ── Restore must respect the caller's path scope ──────────────────
        ns = data_dir / "trash" / getpass.getuser()
        marker = ns / ".namespace"
        check("alice namespace exists on disk", marker.is_file(), str(list(ns.glob("*"))))
        outside = ns / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (ns / "outside.txt.meta.json").write_text(json.dumps({
            "original_path": "/tmp/ethos-trash-outside.txt",
            "trashed_name": "outside.txt",
            "owner": getpass.getuser(),
        }), encoding="utf-8")
        st, body = _call(base, "POST", "/api/files/trash/restore", alice, {"name": "outside.txt"})
        check("restore outside the user's scope is refused", st == 403, f"{st} {body}")
        check("out-of-scope restore wrote nothing", not Path("/tmp/ethos-trash-outside.txt").exists())

        # ── Admin powers ──────────────────────────────────────────────────
        st, body = _call(base, "POST", "/api/files/trash", admin, {"path": str(admin_file)})
        check("admin can trash a file", st == 200, f"{st} {body}")
        st, body = _call(base, "DELETE", "/api/files/trash?all=1", admin)
        check("admin can empty every namespace",
              st == 200 and body.get("scope") == "all" and body.get("deleted_count", 0) >= 2,
              str(body))
        st, body = _call(base, "GET", "/api/files/trash?all=1", admin)
        check("trash is empty afterwards", body.get("total") == 0, str(body))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        for leftover in (alice_file, admin_file, Path("/tmp/ethos-trash-outside.txt")):
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass
        shutil.rmtree(data_dir, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for item, _ok, detail in failed:
            print(f"  FAILED: {item} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
