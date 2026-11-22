#!/usr/bin/env python3
"""A dead package service must not leak Python/socket internals into the UI.

`GET /api/packages/<id>/proxy/...` answers 503 when the loopback service is not
running. The native window prints `detail` verbatim, so it has to be a sentence
an operator can act on — not `<urlopen error [Errno 111] Connection refused>`.
The real exception still goes to the log store.

No login and no Docker: the package is seeded, the port has no listener.
"""
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
DATA = Path(tempfile.mkdtemp(prefix="ethos-pkg-proxy-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA / "data"),
    "ETHOS_JWT_SECRET": "pkg-proxy-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))
from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

PKG_ID = "dead-service-demo"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    results: list[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))

    dead_port = free_port()  # nothing listens here
    pkg_dir = DATA / "data" / "packages" / "apps" / PKG_ID / "frontend"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "app.js").write_text("export function render() {}\n", encoding="utf-8")
    packages_store.add_package({
        "id": PKG_ID, "name": "Dead Service Demo", "version": "1.0.0",
        "icon": "💀", "description": "proxy error contract", "trust": "pinned",
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": dead_port},
        "files": [{"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420}],
    })

    app_port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
         "--port", str(app_port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{app_port}"
    token = _create_token("proxy-test", "proxy-test", "admin")
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base + "/api/system/status", timeout=2)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    break
            except OSError:
                time.sleep(0.3)
        if proc.poll() is not None:
            print(proc.stderr.read()[-2000:], file=sys.stderr)
            return 1

        req = urllib.request.Request(
            f"{base}/api/packages/{PKG_ID}/proxy/api/health",
            headers={"Authorization": "Bearer " + token})
        status, detail = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status, detail = resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            status, detail = exc.code, exc.read().decode()

        check("dead service answers 503", status == 503, f"HTTP {status}")
        body = {}
        try:
            body = json.loads(detail or "{}")
        except ValueError:
            pass
        message = str(body.get("detail") or "")
        check("detail is a sentence, not an exception",
              bool(message) and "urlopen" not in message and "Errno" not in message
              and "Traceback" not in message, message)
        check("detail says what to do", "Package Center" in message, message)
        check("detail names the package", PKG_ID in message, message)

        logs_req = urllib.request.Request(f"{base}/api/logs?limit=50",
                                          headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(logs_req, timeout=20) as resp:
            logs_data = json.loads(resp.read() or b"{}")
        rows = (logs_data.get("items") or logs_data.get("logs")
                or logs_data.get("entries") or [])
        events = DATA / "data" / "logs" / "events.jsonl"
        text = events.read_text(encoding="utf-8") if events.is_file() else ""
        check("the real cause is still logged",
              any("unreachable" in json.dumps(row).lower() for row in rows)
              or "unreachable" in text.lower(),
              f"{len(rows)} API row(s), {len(text)} log byte(s)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DATA, ignore_errors=True)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
