#!/usr/bin/env python3
"""Packages API integration (no Docker required).

Boots an isolated EthOS instance and a local HTTP package repository, adds an
unverified source, installs a files-only package through the job API, verifies
the installed record, then uninstalls it.

Run: .venv/bin/python tests/test_packages_api.py
"""
import functools
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-packages-api-"))
os.environ["ETHOS_DATA_DIR"] = str(TMP / "data")
os.environ["ETHOS_JWT_SECRET"] = "pkg-api-test-secret"
os.environ["ETHOS_PACKAGES_ALLOW_INSECURE"] = "1"
os.environ["ETHOS_PACKAGES_ALLOW_PRIVATE"] = "1"
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_repo(port: int) -> Path:
    repo = TMP / "repo"
    pkg = repo / "demo-pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "hello.txt").write_text("hello package\n", encoding="utf-8")
    manifest = {
        "schema": 2, "id": "demo-pkg", "name": "Demo Package", "version": "1.0.0",
        "description": "Files-only test package", "icon": "📦", "category": "tools",
        "author": "Test", "icon_url": "https://example.com/demo.svg",
        "files": [{
            "url": f"http://127.0.0.1:{port}/demo-pkg/hello.txt",
            "sha256": sha(pkg / "hello.txt"), "path": "hello.txt", "mode": 420,
        }],
    }
    manifest_path = pkg / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index = {
        "schema": 2, "name": "Test Repo",
        "packages": [{
            "id": "demo-pkg", "name": "Demo Package", "version": "1.0.0",
            "description": "Files-only test package", "icon": "📦", "category": "tools",
            "author": "Test",
            "manifest_url": f"http://127.0.0.1:{port}/demo-pkg/manifest.json",
            "manifest_sha256": sha(manifest_path),
            "channel": "stable",
        }],
    }
    (repo / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return repo


def start_http(repo: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repo))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_app(port: int, env_extra: dict) -> subprocess.Popen:
    env = {**os.environ, "ETHOS_PORT": str(port), **env_extra}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def wait_up(proc, port: int, deadline: float = 60) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system/status", timeout=2)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    app_port, repo_port = free_port(), free_port()
    repo = TMP / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    httpd = start_http(repo, repo_port)
    make_repo(repo_port)
    proc = start_app(app_port, {
        "ETHOS_DATA_DIR": str(TMP / "data"),
        "ETHOS_JWT_SECRET": "pkg-api-test-secret",
        "ETHOS_PACKAGES_ALLOW_INSECURE": "1",
        "ETHOS_PACKAGES_ALLOW_PRIVATE": "1",
    })
    base = f"http://127.0.0.1:{app_port}"
    token = _create_token("pkg-test", "pkg-test", "admin")

    def request(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        check("instance up", wait_up(proc, app_port))
        if proc.poll() is not None:
            print(proc.stderr.read()[-2000:], file=sys.stderr)
            return 1

        status, body = request("GET", "/api/packages")
        check("list installed", status == 200 and body.get("items") == [], f"{status} {body}")

        status, body = request("POST", "/api/packages/registry/sources", {
            "url": f"http://127.0.0.1:{repo_port}/index.json",
            "name": "Test repo", "trust": "unverified",
        })
        check("add source", status == 200, f"{status} {body}")

        status, body = request("GET", "/api/packages/registry/browse")
        ids = [p.get("id") for p in body.get("packages", [])]
        check("browse catalog", status == 200 and "demo-pkg" in ids, f"{status} {body.get('errors')}")

        status, body = request("POST", "/api/packages/install", {"package_id": "demo-pkg"})
        check("install job created", status == 200 and body.get("job_id"), f"{status} {body}")
        job_id = body.get("job_id")
        job = {}
        for _ in range(80):
            status, job = request("GET", f"/api/packages/jobs/{job_id}")
            if job.get("status") in ("success", "failed", "cancelled"):
                break
            time.sleep(0.25)
        check("install job succeeded", job.get("status") == "success", str(job.get("error") or job))
        check("install job reports progress", job.get("progress") == 100 and job.get("step_total", 0) > 0,
              str(job))

        status, body = request("GET", "/api/packages")
        installed = {p.get("id"): p for p in body.get("items", [])}
        check("package recorded", "demo-pkg" in installed)
        check("declared file deployed", (TMP / "data/packages/apps/demo-pkg/hello.txt").is_file())

        status, body = request("GET", "/api/packages/desktop-icons")
        icon_entry = next((p for p in body if p.get("id") == "demo-pkg"), {})
        check("desktop icons expose icon_url",
              icon_entry.get("icon_url") == "https://example.com/demo.svg", str(body))

        status, body = request("GET", "/api/packages/demo-pkg/status")
        check("files-only status", status == 200 and body.get("status") == "not_configured", f"{status} {body}")

        status, body = request("GET", "/api/packages/demo-pkg/uninstall-preview")
        check("uninstall preview answers", status == 200 and body.get("id") == "demo-pkg", f"{status} {body}")
        check("files-only preview has no compose artefacts",
              body.get("has_compose") is False and body.get("volumes") == [] and body.get("images") == [],
              str(body))

        status, body = request("DELETE", "/api/packages/demo-pkg?remove_volumes=1&remove_images=1")
        check("uninstall job created", status == 200 and body.get("job_id"), f"{status} {body}")
        uninstall_id = body.get("job_id")
        for _ in range(80):
            status, job = request("GET", f"/api/packages/jobs/{uninstall_id}")
            if job.get("status") in ("success", "failed"):
                break
            time.sleep(0.25)
        check("uninstall succeeded", job.get("status") == "success", str(job.get("error") or job))
        check("uninstall job reports progress", job.get("progress") == 100,
              str(job))
        status, body = request("GET", "/api/packages")
        check("package removed", body.get("items") == [], str(body))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
