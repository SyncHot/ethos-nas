#!/usr/bin/env python3
"""Per-user library isolation + legacy migration.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + JWT secret)
and proves that gallery/music/video/editor state is stored per account, while
the pre-multi-user stores are adopted by the admin once at startup.

Run:
    .venv/bin/python tests/test_user_isolation.py
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
SECRET = "user-isolation-test-secret"
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
        with urllib.request.urlopen(req, timeout=20) as resp:
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


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 40.0) -> bool:
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
    if not Path("/mnt").is_dir():
        print("  [FAIL] this test needs /mnt for the second account")
        return 1

    alice_name = getpass.getuser()
    bob_name = f"iso-bob-{uuid.uuid4().hex[:6]}"
    tag = uuid.uuid4().hex[:8]
    alice_dir = home / f"ethos-iso-alice-{tag}"
    alice_dir.mkdir()
    (alice_dir / "note.txt").write_text("alice\n", encoding="utf-8")
    bob_dir = Path("/mnt")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-iso-"))
    log_path = data_dir / "server.log"
    log = open(log_path, "w")

    # Legacy stores + an admin account so adopt_all() has an owner to migrate to.
    (data_dir / "users.json").write_text(json.dumps([
        {"id": "90", "username": "migrator", "role": "admin"},
    ]), encoding="utf-8")
    (data_dir / "gallery_config.json").write_text(json.dumps(
        {"folders": [{"path": str(alice_dir), "name": "legacy"}], "options": {}, "private_pin": {}}),
        encoding="utf-8")
    (data_dir / "music_config.json").write_text(json.dumps(
        {"download_dir": str(alice_dir)}), encoding="utf-8")

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

    try:
        if not _wait_ready(base, proc):
            print(log_path.read_text()[-2000:])
            return 1

        alice = _token(alice_name, "user")
        bob = _token(bob_name, "user")

        print("\n[legacy migration]")
        migrator_dir = data_dir / "users" / "migrator"
        check("gallery config adopted by the admin", (migrator_dir / "gallery_config.json").is_file())
        check("music config adopted by the admin", (migrator_dir / "music_config.json").is_file())
        check("legacy root file removed", not (data_dir / "gallery_config.json").exists())

        print("\n[music]")
        st, _ = _call(base, "POST", "/api/music/config", alice,
                      {"download_dir": str(alice_dir)})
        check("alice saves her music dir", st == 200, f"{st}")
        _, a_cfg = _call(base, "GET", "/api/music/config", alice)
        _, b_cfg = _call(base, "GET", "/api/music/config", bob)
        check("alice reads her own music dir", a_cfg.get("download_dir") == str(alice_dir),
              str(a_cfg))
        check("bob does not see alice's music dir",
              b_cfg.get("download_dir") != str(alice_dir), str(b_cfg))

        track = {"id": "t1", "title": "Song", "artist": "A", "dur": 10}
        st, _ = _call(base, "PUT", "/api/music/state", alice, {"liked": [track]})
        check("alice imports a liked track", st == 200, f"{st}")
        _, a_state = _call(base, "GET", "/api/music/state", alice)
        _, b_state = _call(base, "GET", "/api/music/state", bob)
        check("alice sees her liked track", len(a_state.get("liked") or []) == 1, str(a_state)[:120])
        check("bob's library is empty", not (b_state.get("liked") or []), str(b_state)[:120])
        check("music store is under the user directory",
              (data_dir / "users" / alice_name / "music_library.json").is_file())

        print("\n[videos]")
        st, _ = _call(base, "POST", "/api/videos/config", alice, {"roots": [str(alice_dir)]})
        check("alice saves her video root", st == 200, f"{st}")
        _, a_v = _call(base, "GET", "/api/videos/config", alice)
        _, b_v = _call(base, "GET", "/api/videos/config", bob)
        check("alice sees her video root", str(alice_dir) in (a_v.get("roots") or []), str(a_v)[:160])
        check("bob does not see alice's video root",
              str(alice_dir) not in (b_v.get("roots") or []), str(b_v)[:160])

        resume_path = str(alice_dir / "clip.mp4")
        st, _ = _call(base, "POST", "/api/videos/resume", alice,
                      {"path": resume_path, "time": 42, "duration": 100})
        check("alice saves a resume position", st == 200, f"{st}")
        _, a_res = _call(base, "GET", "/api/videos/resume/all", alice)
        _, b_res = _call(base, "GET", "/api/videos/resume/all", bob)
        check("alice's resume list has the item", resume_path in (a_res.get("resumes") or {}),
              str(a_res)[:120])
        check("bob's resume list is empty", not (b_res.get("resumes") or {}), str(b_res)[:120])

        print("\n[gallery]")
        st, _ = _call(base, "POST", "/api/gallery/config", alice,
                      {"folders": [{"path": str(alice_dir), "name": "alice"}]})
        check("alice saves her gallery folder", st == 200, f"{st}")
        _, a_g = _call(base, "GET", "/api/gallery/config", alice)
        _, b_g = _call(base, "GET", "/api/gallery/config", bob)
        check("alice sees her gallery folder",
              [f.get("path") for f in a_g.get("folders") or []] == [str(alice_dir)],
              str(a_g)[:160])
        check("bob's gallery is empty", not (b_g.get("folders") or []), str(b_g)[:160])
        check("gallery index is per user",
              (data_dir / "users" / alice_name / "gallery_config.json").is_file())

        print("\n[editor recents]")
        st, _ = _call(base, "POST", "/api/editor/projects/recent", alice,
                      {"path": str(alice_dir)})
        check("alice adds a recent project", st == 200, f"{st}")
        _, a_r = _call(base, "GET", "/api/editor/projects/recent", alice)
        _, b_r = _call(base, "GET", "/api/editor/projects/recent", bob)
        check("alice sees her recent project",
              any(r.get("path") == str(alice_dir) for r in a_r.get("recents") or []),
              str(a_r)[:160])
        check("bob does not see alice's recents", not (b_r.get("recents") or []),
              str(b_r)[:160])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(alice_dir, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
