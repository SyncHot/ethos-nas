#!/usr/bin/env python3
"""Core `ui.ribbon` contract + Playwright mount test.

Part A validates the manifest schema (no server). Part B boots an isolated
EthOS instance with a fake pinned native package that declares a ribbon entry,
then verifies the core mounts it into `#ethos-ribbon-host`, wires `ctx.proxy`
to the package service and applies the desktop gutter.
"""

from __future__ import annotations

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
DATA = Path(tempfile.mkdtemp(prefix="ethos-ribbon-ui-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "ribbon-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402
from backend.services.package_schema import PackageError, validate_manifest  # noqa: E402

RIBBON_MODULE = """
export async function mount(host, ctx) {
    const health = await ctx.proxy('api/health');
    const box = document.createElement('div');
    box.id = 'ribbon-demo-status';
    box.setAttribute('data-testid', 'ribbon-demo-status');
    box.textContent = 'Ribbon ' + health.status;
    box.style.cssText = 'position:absolute;right:0;top:50%;width:40px;height:40px;background:#fde047;';
    host.appendChild(box);
    ctx.setDesktopGutter(44);
    return () => { box.remove(); };
}
"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def manifest(ribbon):
    files = [
        {"url": "https://example.com/app.js", "sha256": "0" * 64, "path": "frontend/app.js"},
        {"url": "https://example.com/ribbon.js", "sha256": "0" * 64, "path": "frontend/ribbon.js"},
    ]
    ui = {"mode": "native", "entry": "frontend/app.js"}
    if ribbon is not None:
        ui["ribbon"] = ribbon
    return {"id": "ribbon-demo", "name": "Ribbon Demo", "version": "1.0.0",
            "description": "demo", "icon": "🧩", "runtime": "files",
            "files": files, "ui": ui}


def schema_checks() -> None:
    print("\n[static]")
    normalized = validate_manifest(manifest({"entry": "frontend/ribbon.js", "title": "Demo"}))
    check("schema accepts ui.ribbon", normalized["ui"]["ribbon"]["entry"] == "frontend/ribbon.js")
    check("schema keeps the ribbon title", normalized["ui"]["ribbon"]["title"] == "Demo")
    for bad, label in (
        ({}, "missing entry"),
        ({"entry": "frontend/missing.js"}, "entry not in files[]"),
        ({"entry": "frontend/app.txt"}, "non-JS entry"),
    ):
        try:
            validate_manifest(manifest(bad))
            check(f"schema rejects {label}", False, "accepted")
        except PackageError:
            check(f"schema rejects {label}", True)
    window = manifest(None)
    window["ui"] = {"mode": "window", "port": 8099, "ribbon": {"entry": "frontend/ribbon.js"}}
    try:
        validate_manifest(window)
        check("schema rejects a ribbon on a window UI", False, "accepted")
    except PackageError:
        check("schema rejects a ribbon on a window UI", True)


def main() -> int:
    schema_checks()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\nPlaywright is not installed — skipping the mount test")
        return 0 if all(ok for _name, ok, _detail in results) else 1

    app_port, proxy_port = free_port(), free_port()

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

    pkg_dir = DATA / "packages" / "apps" / "ribbon-demo" / "frontend"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "app.js").write_text("export function render() {}\n", encoding="utf-8")
    (pkg_dir / "ribbon.js").write_text(RIBBON_MODULE, encoding="utf-8")
    packages_store.add_package({
        "id": "ribbon-demo", "name": "Ribbon Demo", "version": "1.0.0",
        "icon": "🧩", "description": "Ribbon host test", "trust": "pinned",
        "user_access": True,
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": proxy_port,
               "ribbon": {"entry": "frontend/ribbon.js", "title": "Demo"}},
        "files": [
            {"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/ribbon.js", "url": "local", "sha256": "0" * 64, "mode": 420},
        ],
    })

    token = _create_token("ribbon-ui", "ribbon-ui", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(app_port)],
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
            check("isolated instance started", False)
            return 1

        print("\n[live]")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            status = page.locator("#ribbon-demo-status")
            status.wait_for(timeout=15000)
            check("ribbon module mounted", status.inner_text() == "Ribbon ready", status.inner_text())
            z_index = page.evaluate(
                "getComputedStyle(document.getElementById('ethos-ribbon-host')).zIndex")
            check("host uses the shell z-index", z_index == "950", z_index)
            gutter = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--desktop-right-gutter').trim()")
            check("ribbon reserves the desktop gutter", gutter == "44px", gutter)
            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()

    failed = [item for item in results if not item[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
