#!/usr/bin/env python3
"""Native package UI contract: asset auth, proxy, and in-window module render.

Boots an isolated EthOS instance and a tiny mock package service, seeds one
installed native package, then verifies that the frontend loads the module as an
ES module (no iframe) and that ``ctx.proxy`` reaches the package service.
"""
import functools
import http.server
import json
import os
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
DATA = Path(tempfile.mkdtemp(prefix="ethos-native-ui-"))
os.environ.update({"ETHOS_DATA_DIR": str(DATA), "ETHOS_JWT_SECRET": "native-ui-test",
                   "ETHOS_PACKAGES_DEFAULT_SOURCE": "0"})
sys.path.insert(0, str(ROOT))
from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

MODULE = """
export async function render(container, ctx) {
    const data = await ctx.proxy('api/health');
    const box = document.createElement('div');
    box.id = 'native-proxy-status';
    box.textContent = 'Native ' + data.status;
    container.appendChild(box);
    const extras = document.createElement('div');
    extras.id = 'native-ctx';
    extras.textContent = [
        typeof ctx.progress?.startTask,
        typeof ctx.modal?.confirm,
        typeof ctx.escape?.html,
    ].join(',');
    container.appendChild(extras);
}
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    app_port, proxy_port = free_port(), free_port()
    results: list[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = json.dumps({"status": "ready", "path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    pkg_dir = DATA / "packages" / "apps" / "native-demo" / "frontend"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "app.js").write_text(MODULE, encoding="utf-8")
    packages_store.add_package({
        "id": "native-demo", "name": "Native Demo", "version": "1.0.0",
        "icon": "🧩", "description": "Native UI test", "trust": "pinned",
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": proxy_port},
        "files": [{"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420}],
    })

    token = _create_token("native-ui", "native-ui", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(app_port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{app_port}"

    try:
        end = time.time() + 60
        while time.time() < end:
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

        asset = "/api/packages/native-demo/ui/frontend/app.js"
        try:
            urllib.request.urlopen(base + asset, timeout=5)
            check("native asset requires auth", False, "unauthenticated request succeeded")
        except urllib.error.HTTPError as exc:
            check("native asset requires auth", exc.code == 401, f"HTTP {exc.code}")

        req = urllib.request.Request(base + asset, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            source = resp.read().decode()
        check("native asset serves module", "export async function render" in source)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_timeout(2500)
            page.evaluate("window.openApp('pkg-native-demo')")
            status = page.locator("#native-proxy-status")
            status.wait_for(timeout=15000)
            check("native module rendered", status.inner_text() == "Native ready", status.inner_text())
            ctx_kinds = page.locator("#native-ctx").inner_text()
            check("native ctx exposes progress/modal/escape",
                  ctx_kinds == "function,function,function", ctx_kinds)
            check("native UI is not an iframe", page.locator("iframe").count() == 0)
            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
