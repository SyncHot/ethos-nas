#!/usr/bin/env python3
"""test_gallery_rescan.py — gallery rescan must complete when no folder is walkable.

Regression test (T-0010): _do_scan() created its cursor (`cur`) only inside
`if walked:`, but the final index SELECT used it unconditionally. A rescan
with zero walkable folders (fresh install with no folders configured, or all
folders missing from disk e.g. unmounted USB) crashed the worker with
"cannot access local variable 'cur' where it is not associated with a value",
logged 'Gallery rescan failed', and never updated the index.

Self-contained: starts its own isolated instance on a free port with a fresh
data dir — never touches production state on :9000.

Run:  .venv/bin/python tests/test_gallery_rescan.py
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
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()

failures = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        global failures
        failures += 1
    return ok


def _url(base: str, path: str) -> str:
    """Build a request URL; only loopback http is ever used here."""
    assert base.startswith("http://127.0.0.1"), f"unexpected base url: {base}"
    return base + path


def find_free_port(start: int = 9101, span: int = 100) -> int:
    for port in range(start, start + span):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError(f"no free port in {start}..{start + span - 1}")


def start_instance(port: int, data_dir: str, log_fh) -> subprocess.Popen:
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": data_dir}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env, stdout=log_fh, stderr=subprocess.STDOUT)


def wait_healthy(base: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(_url(base, "/api/system/status"), timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # 401 = up, unauthenticated
        except Exception:
            time.sleep(0.5)
    return False


def api(base: str, method: str, path: str, token: str, body=None):
    req = urllib.request.Request(
        _url(base, path), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, None


def wait_scan_done(base: str, token: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        _, resp = api(base, "GET", "/api/gallery/rescan-status", token)
        last = resp if isinstance(resp, dict) else {}
        if not last.get("scanning"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    port = find_free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="ethos-gallery-rescan-data-")
    log_fd, log_path = tempfile.mkstemp(prefix="ethos-gallery-rescan-log-")
    os.close(log_fd)

    with open(log_path, "wb") as log_fh:
        proc = start_instance(port, data_dir, log_fh)
        print(f"isolated instance on :{port} (data {data_dir})")
        try:
            if not check("instance healthy", wait_healthy(base)):
                print("instance did not come up — aborting")
                return 1

            _, d = api(base, "POST", "/api/auth/login", "",
                       {"username": USER, "password": PASS})
            if not d or "token" not in d:
                check("login", False, f"HTTP response {d}")
                return 1
            token = d["token"]

            # ── Scenario A: zero walkable folders (fresh install) ─────────
            print("\nScenario A — rescan with no folders configured")
            _, cfg = api(base, "GET", "/api/gallery/config", token)
            if not check("fresh config has no folders",
                         (cfg or {}).get("folders") == [], str(cfg)):
                return 1
            code, _ = api(base, "POST", "/api/gallery/rescan", token)
            if not check("POST /rescan accepted", code == 200, f"HTTP {code}"):
                return 1
            st = wait_scan_done(base, token)
            if not check("scan finished (scanning=false)",
                         st.get("scanning") is False, str(st)):
                return 1
            if not check("index updated (scanned_at > 0)",
                         (st.get("scanned_at") or 0) > 0, str(st)):
                return 1
            with open(log_path, errors="replace") as f:
                log = f.read()
            bad_lines = [line for line in log.splitlines()
                         if "Gallery rescan failed" in line]
            if not check("no 'Gallery rescan failed' in server log",
                         not bad_lines, str(bad_lines[-1:])):
                return 1

            # ── Scenario B: real folder with files still indexes normally ─
            print("\nScenario B — rescan with a real folder (fix must not break normal scans)")
            lib = tempfile.mkdtemp(prefix="ethos-gallery-rescan-lib-")
            try:
                for name in ("a.jpg", "b.jpg"):
                    with open(os.path.join(lib, name), "wb") as f:
                        f.write(b"\xff\xd8\xff\xe0" + b"0" * 16)  # tiny JPEG-ish blob
                _, saved = api(base, "POST", "/api/gallery/config", token,
                               {"folders": [{"path": lib}]})
                if not check("config saved with folder",
                             (saved or {}).get("ok") is True, str(saved)):
                    return 1
                code, _ = api(base, "POST", "/api/gallery/rescan", token)
                if not check("POST /rescan accepted", code == 200, f"HTTP {code}"):
                    return 1
                st = wait_scan_done(base, token)
                if not check("scan finished (scanning=false)",
                             st.get("scanning") is False, str(st)):
                    return 1
                if not check("both files indexed (items=2)",
                             st.get("items") == 2, str(st)):
                    return 1
                with open(log_path, errors="replace") as f:
                    log = f.read()
                if not check("no 'Gallery rescan failed' in server log",
                             "Gallery rescan failed" not in log):
                    return 1
            finally:
                shutil.rmtree(lib, ignore_errors=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(data_dir, ignore_errors=True)
    if os.path.exists(log_path):
        os.unlink(log_path)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
