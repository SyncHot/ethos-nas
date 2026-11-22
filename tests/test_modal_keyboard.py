#!/usr/bin/env python3
"""Modal keyboard contract (Playwright).

Regression for "New Folder closes without creating anything": the dialog's
Enter handler used the dismiss path, which resolved the pending promise with
``null`` before the typed value could be returned. Enter must submit the
value, Escape must still cancel.

Run: .venv/bin/python tests/test_modal_keyboard.py
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
TMP = Path(tempfile.mkdtemp(prefix="ethos-modal-kb-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(TMP / "data"),
    "ETHOS_JWT_SECRET": "modal-kb-test-secret",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    "ETHOS_UPDATE_CHECK": "0",
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


def wait_ready(base: str, proc: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/system/status", timeout=2)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 401:      # up, unauthenticated
                return
        except OSError:
            pass
        if proc.poll() is not None:
            raise SystemExit("instance died on start")
        time.sleep(0.3)
    raise SystemExit("instance did not become ready")


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = _create_token("modal-kb", "modal-kb", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )

    try:
        wait_ready(base, proc)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1200, "height": 800})
            ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_function("typeof window.showPrompt === 'function'", timeout=20000)

            # Enter in a prompt returns the typed value (New Folder / New File).
            page.evaluate("""() => {
                window.__modalResult = 'pending';
                window.showPrompt('New Folder', 'Enter folder name:').then((v) => { window.__modalResult = v; });
            }""")
            page.fill('[data-testid="ethos-modal-input"]', 'Audiobooks')
            page.keyboard.press('Enter')
            page.wait_for_function("window.__modalResult !== 'pending'", timeout=5000)
            value = page.evaluate("window.__modalResult")
            check("prompt Enter returns the typed value", value == "Audiobooks", repr(value))
            check("prompt closes after Enter", page.locator(".ethos-modal-box").count() == 0)

            # Enter in a confirm resolves true, not null.
            page.evaluate("""() => {
                window.__modalResult = 'pending';
                window.showModalConfirm('Restart App', 'Restart?', { confirmLabel: 'Restart' })
                    .then((v) => { window.__modalResult = v; });
            }""")
            page.keyboard.press("Enter")
            page.wait_for_function("window.__modalResult !== 'pending'", timeout=5000)
            value = page.evaluate("window.__modalResult")
            check("confirm Enter resolves true", value is True, repr(value))
            check("confirm closes after Enter", page.locator(".ethos-modal-box").count() == 0)

            # Escape still cancels with null.
            page.evaluate("""() => {
                window.__modalResult = 'pending';
                window.showPrompt('New Folder', 'Enter folder name:').then((v) => { window.__modalResult = v; });
            }""")
            page.keyboard.press("Escape")
            page.wait_for_function("window.__modalResult !== 'pending'", timeout=5000)
            value = page.evaluate("window.__modalResult")
            check("Escape cancels with null", value is None, repr(value))
            check("Escape closes the dialog", page.locator(".ethos-modal-box").count() == 0)

            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
