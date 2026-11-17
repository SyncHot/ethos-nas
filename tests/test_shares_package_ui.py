#!/usr/bin/env python3
"""Playwright coverage for the native Samba Shares package with mocked host APIs."""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT.parent / "ethos-packages" / "ethos-shares"
DATA = Path(tempfile.mkdtemp(prefix="ethos-shares-package-ui-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "shares-package-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    if not (PACKAGE / "frontend/app-shell.js").is_file():
        print(f"ethos-packages checkout not found at {PACKAGE} — skipping")
        shutil.rmtree(DATA, ignore_errors=True)
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping")
        shutil.rmtree(DATA, ignore_errors=True)
        return 0

    from backend.routers.auth import _create_token
    from backend.routers import shares as shares_router
    from backend.services import packages_store
    from backend.main import app
    import uvicorn

    installed = DATA / "packages" / "apps" / "ethos-shares"
    (installed / "frontend").mkdir(parents=True)
    (installed / "frontend" / "app-shell.js").write_text(
        (PACKAGE / "frontend" / "app-shell.js").read_text(encoding="utf-8"), encoding="utf-8")
    (installed / "icon.svg").write_text((PACKAGE / "icon.svg").read_text(encoding="utf-8"), encoding="utf-8")
    packages_store.add_package({
        "id": "ethos-shares", "name": "Samba Shares", "version": "1.3.0",
        "icon": "📁", "description": "Samba shares UI test", "trust": "pinned",
        "ui": {"mode": "native", "entry": "frontend/app-shell.js"},
        "files": [
            {"path": "frontend/app-shell.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "icon.svg", "url": "local", "sha256": "0" * 64, "mode": 420},
        ],
    })

    shares = []
    accounts = {
        "users": [{"username": "alice", "samba_enabled": True},
                  {"username": "bob", "samba_enabled": False}],
        "groups": [{"name": "storage"}],
    }
    calls = {"add": [], "update": [], "password": [], "delete_account": [], "delete_share": []}
    originals = {name: getattr(shares_router, name) for name in (
        "get_shares", "get_status", "get_samba_accounts", "add_share", "update_share",
        "delete_share", "set_samba_password", "delete_samba_account",
    )}

    def fake_add_share(**kwargs):
        calls["add"].append(kwargs)
        shares.append({**kwargs, "read_only": kwargs["read_only"], "browsable": kwargs["browsable"],
                       "guest_ok": kwargs["guest_ok"], "writable": not kwargs["read_only"]})
        return {"name": kwargs["name"], "status": "added"}

    def fake_update_share(name, **kwargs):
        calls["update"].append((name, kwargs))
        for share in shares:
            if share["name"] == name:
                share.update({key: value for key, value in kwargs.items() if value is not None})
        return {"name": name, "status": "updated"}

    def fake_delete_share(name):
        calls["delete_share"].append(name)
        shares[:] = [share for share in shares if share["name"] != name]
        return {"name": name, "status": "deleted"}

    def fake_set_password(username, password):
        calls["password"].append((username, password))
        for user in accounts["users"]:
            if user["username"] == username:
                user["samba_enabled"] = True
        return {"username": username, "status": "updated"}

    def fake_delete_account(username):
        calls["delete_account"].append(username)
        for user in accounts["users"]:
            if user["username"] == username:
                user["samba_enabled"] = False
        return {"username": username, "status": "deleted"}

    shares_router.get_shares = lambda: list(shares)
    shares_router.get_status = lambda: {
        "service": "smbd", "active": True, "enabled": True, "samba_installed": True,
        "missing_tools": [], "install_hint": "",
    }
    shares_router.get_samba_accounts = lambda: accounts
    shares_router.add_share = fake_add_share
    shares_router.update_share = fake_update_share
    shares_router.delete_share = fake_delete_share
    shares_router.set_samba_password = fake_set_password
    shares_router.delete_samba_account = fake_delete_account

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline and not server.started:
            time.sleep(0.1)
        if not server.started:
            check("isolated app server starts", False)
            return 1

        token = _create_token("shares-ui-test", "shares-ui-test", "admin")
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:
                print(f"Chromium is not available — skipping ({exc})")
                return 0
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"http://127.0.0.1:{port}")
            page.wait_for_timeout(1800)
            page.evaluate("window.openApp('pkg-ethos-shares')")
            page.wait_for_selector(".sh-root", timeout=15000)
            check("native package shell renders", page.locator(".sh-root").count() == 1)
            check("shared shell rail is 218px", page.locator(".sh-side").evaluate("el => getComputedStyle(el).width") == "218px")
            check("both views are present", page.locator(".sh-nav-item").count() == 2)

            page.click('[data-testid="shares-add"]')
            page.fill('[data-testid="shares-form-name"]', "Media")
            page.fill('[data-testid="shares-form-path"]', "/mnt/media")
            page.fill('[data-testid="shares-form-create-mask"]', "0640")
            page.fill('[data-testid="shares-form-directory-mask"]', "0750")
            page.check('[data-testid="shares-form-manage-acl"]')
            page.locator('[data-testid="shares-form-read-users"]').select_option("alice")
            page.locator('[data-testid="shares-form-write-users"]').select_option("@storage")
            page.click('[data-testid="shares-form-submit"]')
            page.wait_for_selector('[data-testid="shares-row-Media"]', timeout=10000)
            check("share create sends masks and structured ACLs",
                  calls["add"] and calls["add"][0]["create_mask"] == "0640"
                  and calls["add"][0]["directory_mask"] == "0750"
                  and calls["add"][0]["read_users"] == ["alice"]
                  and calls["add"][0]["write_users"] == ["@storage"])

            page.click('[data-testid="shares-edit-Media"]')
            page.fill('[data-testid="shares-form-comment"]', "Updated comment")
            page.click('[data-testid="shares-form-submit"]')
            page.wait_for_function("document.querySelector('[data-testid=shares-row-Media]')?.innerText.includes('Updated comment')")
            check("share update keeps ACL fields wired", calls["update"] and calls["update"][0][0] == "Media")
            page.click('[data-testid="shares-edit-Media"]')
            page.check('[data-testid="shares-form-readonly"]')
            page.click('[data-testid="shares-form-submit"]')
            page.wait_for_function("document.querySelector('[data-testid=shares-row-Media]')")
            check("read-only update clears selected writers", calls["update"][-1][1]["write_users"] == [])

            page.click('[data-testid="shares-delete-Media"]')
            page.wait_for_selector('[data-testid="unified-confirm-dialog"]', timeout=5000)
            check("share deletion waits for explicit confirmation", calls["delete_share"] == [])
            page.click('[data-testid="unified-confirm-cancel"]')
            check("cancel keeps the share", len(shares) == 1 and calls["delete_share"] == [])
            page.click('[data-testid="shares-delete-Media"]')
            page.wait_for_selector('[data-testid="unified-confirm-dialog"]', timeout=5000)
            page.click('[data-testid="unified-confirm-ok"]')
            page.wait_for_selector('[data-testid="shares-empty"]', timeout=10000)
            check("share deletion uses the confirmation modal", calls["delete_share"] == ["Media"])

            page.click('.sh-nav-item[data-view="accounts"]')
            page.wait_for_selector('[data-testid="samba-user-bob"]', timeout=10000)
            page.click('[data-testid="samba-password-bob"]')
            page.fill('#sh-new-password', "a-long-test-password")
            page.click('[data-testid="modal-form-ok"]')
            page.wait_for_function("document.querySelector('[data-testid=samba-user-bob]')?.innerText.includes('Enabled')")
            check("existing Linux user can be enabled for SMB", calls["password"] == [("bob", "a-long-test-password")])

            page.click('[data-testid="samba-remove-alice"]')
            page.wait_for_selector('[data-testid="unified-confirm-dialog"]', timeout=5000)
            check("account removal waits for explicit confirmation", calls["delete_account"] == [])
            page.click('[data-testid="unified-confirm-ok"]')
            page.wait_for_function("!document.querySelector('[data-testid=samba-remove-alice]')")
            check("removing SMB credentials leaves the Linux account listed", calls["delete_account"] == ["alice"])

            page.evaluate("document.documentElement.classList.add('dark')")
            dark_bg = page.locator('.sh-root').evaluate("el => getComputedStyle(el).getPropertyValue('--sh-bg').trim()")
            check("dark palette applies", dark_bg == "#08111d", dark_bg)
            page.set_viewport_size({"width": 680, "height": 900})
            direction = page.locator('.sh-root').evaluate("el => getComputedStyle(el).flexDirection")
            check("narrow layout stacks the rail", direction == "column", direction)
            check("native UI has no page errors", not errors, "; ".join(errors[:3]))
            check("native package uses no iframe", page.locator("iframe").count() == 0)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for name, function in originals.items():
            setattr(shares_router, name, function)
        shutil.rmtree(DATA, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())