#!/usr/bin/env python3
"""Packages install/uninstall progress UI (Playwright).

Boots an isolated EthOS instance and a local files-only package source, then
verifies that jobs render a live progress bar with the step label, reach 100%,
and refresh the Installed table after completion.

Run: .venv/bin/python tests/test_packages_ui.py
"""
import functools
import hashlib
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
TMP = Path(tempfile.mkdtemp(prefix="ethos-packages-ui-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(TMP / "data"),
    "ETHOS_JWT_SECRET": "pkg-ui-test-secret",
    "ETHOS_PACKAGES_ALLOW_INSECURE": "1",
    "ETHOS_PACKAGES_ALLOW_PRIVATE": "1",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
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


def main() -> int:
    app_port, repo_port = free_port(), free_port()
    repo = make_repo(repo_port)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repo))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", repo_port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    token = _create_token("pkg-ui", "pkg-ui", "admin")
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

        source_req = urllib.request.Request(
            base + "/api/packages/registry/sources",
            data=json.dumps({
                "url": f"http://127.0.0.1:{repo_port}/index.json",
                "name": "Test repo", "trust": "unverified",
            }).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(source_req, timeout=10):
            pass

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_function("typeof window.openApp === 'function'", timeout=20000)
            page.evaluate("window.openApp('packages')")

            install = page.locator('[data-testid="pkg-install-demo-pkg"]')
            install.wait_for(timeout=20000)
            install.click()

            bar = page.locator('[data-testid="pkg-job-progress"]')
            bar.wait_for(timeout=10000)
            check("progress bar is a progressbar", bar.get_attribute("role") == "progressbar",
                  str(bar.get_attribute("role")))
            title = page.locator('[data-testid="pkg-job-title"]').inner_text()
            check("install title", "Installing" in title and "Demo Package" in title, title)
            step = page.locator('[data-testid="pkg-job-step"]').inner_text().strip()
            check("step label rendered", bool(step), step)

            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-percent\"]')?.textContent === '100%'",
                timeout=20000,
            )
            check("install progress reaches 100%", True)

            page.locator('[data-testid="pkg-tab-installed"]').click()
            row = page.locator('[data-testid="pkg-row-demo-pkg"]')
            row.wait_for(timeout=20000)
            check("installed row uses icon_url", row.locator("img").count() == 1)

            page.locator('#topbar-app-drawer-btn').click()
            drawer_item = page.locator('[data-testid="drawer-item-pkg-demo-pkg"]')
            drawer_item.wait_for(timeout=10000)
            check("app drawer uses icon_url", drawer_item.locator("img").count() == 1)
            page.keyboard.press("Escape")

            page.locator('[data-testid="pkg-uninstall-demo-pkg"]').click()
            volume_box = page.locator('[data-testid="pkg-uninstall-remove-volumes"]')
            volume_box.wait_for(timeout=5000)
            check("uninstall dialog offers volume removal", volume_box.count() == 1)
            check("uninstall dialog offers image removal",
                  page.locator('[data-testid="pkg-uninstall-remove-images"]').count() == 1)
            check("uninstall keep-data options default off",
                  not volume_box.is_checked()
                  and not page.locator('[data-testid="pkg-uninstall-remove-images"]').is_checked())
            check("files-only package has nothing to delete",
                  volume_box.is_disabled()
                  and page.locator('[data-testid="pkg-uninstall-remove-images"]').is_disabled())
            page.locator('[data-testid="modal-form-ok"]').click()
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-title\"]')?.textContent?.includes('Uninstalling')",
                timeout=10000,
            )
            check("uninstall progress title", True)
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-percent\"]')?.textContent === '100%'",
                timeout=20000,
            )
            check("uninstall progress reaches 100%", True)

            page.wait_for_function(
                "!document.querySelector('[data-testid=\"pkg-row-demo-pkg\"]')",
                timeout=20000,
            )
            check("package row removed after uninstall", True)
            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        httpd.server_close()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
