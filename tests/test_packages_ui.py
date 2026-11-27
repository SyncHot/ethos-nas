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
        "description": "Test package with a window UI", "icon": "📦", "category": "tools",
        "author": "Test", "icon_url": "https://example.com/demo.svg",
        "ui": {"mode": "window", "scheme": "http", "port": 8099, "path": "/"},
        "files": [{
            "url": f"http://127.0.0.1:{port}/demo-pkg/hello.txt",
            "sha256": sha(pkg / "hello.txt"), "path": "hello.txt", "mode": 420,
        }],
    }
    manifest_path = pkg / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # Second package: its manifest requires a value only the admin can give (a
    # login password with no default), so installing it must ask first.
    setup = repo / "setup-pkg"
    setup.mkdir(parents=True, exist_ok=True)
    (setup / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: example/app:1\n", encoding="utf-8")
    setup_manifest = {
        "schema": 2, "id": "setup-pkg", "name": "Setup Package", "version": "1.0.0",
        "description": "Test package that needs a password", "icon": "🔐", "category": "tools",
        "author": "Test",
        "compose": {"url": f"http://127.0.0.1:{port}/setup-pkg/docker-compose.yml",
                    "sha256": sha(setup / "docker-compose.yml")},
        "ui": {"mode": "window", "scheme": "http", "port": 8098, "path": "/",
               "expose": "lan", "auth": "package"},
        "settings": [
            {"key": "admin_user", "env": "ADMIN_USER", "label": "Login",
             "default": "ethos", "required": True, "max_length": 40},
            {"key": "admin_password", "env": "ADMIN_PASSWORD",
             "label": "Password (at least 8 characters)", "type": "password",
             "required": True, "min_length": 8, "max_length": 128},
        ],
    }
    setup_manifest_path = setup / "manifest.json"
    setup_manifest_path.write_text(json.dumps(setup_manifest), encoding="utf-8")

    index = {
        "schema": 2, "name": "Test Repo",
        "packages": [{
            "id": "demo-pkg", "name": "Demo Package", "version": "1.0.0",
            "description": "Files-only test package", "icon": "📦", "category": "tools",
            "author": "Test",
            "manifest_url": f"http://127.0.0.1:{port}/demo-pkg/manifest.json",
            "manifest_sha256": sha(manifest_path),
            "channel": "stable",
        }, {
            "id": "setup-pkg", "name": "Setup Package", "version": "1.0.0",
            "description": "Needs a password at install time", "icon": "🔐", "category": "tools",
            "author": "Test",
            "manifest_url": f"http://127.0.0.1:{port}/setup-pkg/manifest.json",
            "manifest_sha256": sha(setup_manifest_path),
            "channel": "stable",
        }],
    }
    (repo / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return repo


def bump_repo(version: str) -> None:
    """Publish a newer version so the catalog reports an update."""
    repo = TMP / "repo"
    manifest_path = repo / "demo-pkg" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index_path = repo / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["packages"][0]["version"] = version
    index["packages"][0]["manifest_sha256"] = sha(manifest_path)
    index_path.write_text(json.dumps(index), encoding="utf-8")


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
            print((proc.stderr.read() if proc.stderr else "")[-2000:], file=sys.stderr)
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

            # A manifest can require a value only the admin can give (the Web
            # Browser's login password). The card's Install must ask for it
            # instead of firing blind and failing with the raw label.
            captured: list[dict] = []

            def _capture_install(route):
                captured.append(json.loads(route.request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"job_id": "setup-job"}))

            def _fake_job(route):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "id": "setup-job", "kind": "install", "package_id": "setup-pkg",
                    "status": "success", "progress": 100, "step": "done",
                    "step_index": 1, "step_total": 1, "log": ["done"],
                }))

            page.route("**/api/packages/install", _capture_install)
            page.route("**/api/packages/jobs/setup-job", _fake_job)

            page.locator('[data-testid="pkg-install-setup-pkg"]').click()
            password = page.locator('[data-testid="pkg-setting-admin_password"]')
            password.wait_for(timeout=10000)
            check("a required setting opens the setup dialog before installing", not captured)
            check("the setup field is a password input", password.get_attribute("type") == "password")
            check("the setup dialog lists every declared setting",
                  page.locator('[data-testid="pkg-setting-admin_user"]').count() == 1)
            page.locator('[data-testid="modal-form-ok"]').click()
            page.wait_for_function(
                "() => document.body.innerText.includes('Password (at least 8 characters) is required.')",
                timeout=5000,
            )
            check("an empty required setting is refused before any job starts", not captured)
            password.fill("hunter22")
            page.locator('[data-testid="modal-form-ok"]').click()
            for _ in range(60):
                if captured:
                    break
                page.wait_for_timeout(100)
            body = captured[0] if captured else {}
            settings = body.get("settings") or {}
            check("the install carries the typed settings",
                  settings.get("admin_password") == "hunter22"
                  and settings.get("admin_user") == "ethos", json.dumps(body))
            check("the setup install starts a job", body.get("package_id") == "setup-pkg", json.dumps(body))
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-title\"]')?.textContent?.includes('Setup Package')",
                timeout=10000,
            )
            check("the setup job renders in the shared progress row", True)
            # Let the (intercepted) job settle so its banner does not keep the
            # other packages' buttons disabled.
            page.wait_for_function(
                "() => { const b = document.querySelector('[data-testid=\"pkg-install-demo-pkg\"]');"
                " return !!b && !b.disabled; }",
                timeout=20000,
            )
            page.unroute("**/api/packages/install")
            page.unroute("**/api/packages/jobs/setup-job")

            install = page.locator('[data-testid="pkg-install-demo-pkg"]')
            install.wait_for(timeout=20000)
            install.click()

            bar = page.locator('[data-testid="pkg-job-progress"]')
            bar.wait_for(timeout=10000)
            check("progress bar is a progressbar", bar.get_attribute("role") == "progressbar",
                  str(bar.get_attribute("role")))
            # The shared row may still show the previous job for one frame.
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-title\"]')?.textContent?.includes('Demo Package')",
                timeout=10000,
            )
            title = page.locator('[data-testid="pkg-job-title"]').inner_text()
            check("install title", "Installing" in title and "Demo Package" in title, title)
            step = page.locator('[data-testid="pkg-job-step"]').inner_text().strip()
            check("step label rendered", bool(step), step)

            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-percent\"]')?.textContent === '100%'",
                timeout=20000,
            )
            check("install progress reaches 100%", True)

            # Update all: publish 1.1.0, refresh, then update through the toolbar.
            bump_repo("1.1.0")
            page.locator('[data-testid="pkg-refresh-btn"]').click()
            update_all = page.locator('[data-testid="pkg-update-all"]')
            update_all.wait_for(state="visible", timeout=20000)
            check("update all offered with a count", "(1)" in update_all.inner_text(),
                  update_all.inner_text())
            update_all.click()
            page.locator('[data-testid="ethos-modal-ok"]').click()
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-title\"]')?.textContent?.includes('Updating')",
                timeout=20000,
            )
            check("update all starts an update job", True)
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-job-percent\"]')?.textContent === '100%'",
                timeout=30000,
            )
            check("update all progress reaches 100%", True)
            page.wait_for_function(
                "document.querySelector('[data-testid=\"pkg-update-all\"]')?.hidden === true",
                timeout=20000,
            )
            check("update all hides when nothing is pending", True)

            page.locator('[data-testid="pkg-tab-installed"]').click()
            row = page.locator('[data-testid="pkg-row-demo-pkg"]')
            row.wait_for(timeout=20000)
            check("installed row uses icon_url", row.locator("img").count() == 1)
            check("update all installed the new version", "1.1.0" in row.inner_text(), row.inner_text())

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
