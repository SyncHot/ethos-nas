#!/usr/bin/env python3
"""Cross-package UI sweep: open every native package window and report defects.

Seeds the whole catalog from a local copy of the packages repository into an
isolated EthOS instance (no Docker, no package services), then drives each
native window with Playwright and records:

  * uncaught page errors (hard failure),
  * console errors (hard failure only for packages without a service — a 503
    from `ctx.proxy` is expected when the service is not running here),
  * an unstyled or empty window root (finding: the module shipped no CSS),
  * theme tokens actually applied (background painted vs transparent).

The catalog defaults to `~/apps/compose/ethos-packages`; point `ETHOS_QA_REPO`
elsewhere to sweep a checkout under review. Screenshots go to
`$ETHOS_QA_SHOTS` when set (never committed).

Run: `.venv/bin/python tests/test_packages_qa_ui.py`
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
REPO = Path(os.environ.get("ETHOS_QA_REPO", "~/apps/compose/ethos-packages")).expanduser()
DATA = Path(tempfile.mkdtemp(prefix="ethos-qa-ui-"))
SHOTS = Path(os.environ["ETHOS_QA_SHOTS"]).expanduser() if os.environ.get("ETHOS_QA_SHOTS") else None
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "qa-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))
from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

# Windows allowed to differ from the shared shell (documented exceptions).
LEGACY = {"tic-tac-toe", "ethos-welcome"}

PROBE = """
() => {
    const win = document.querySelector('[data-testid="window-' + window.__qaApp + '"]');
    if (!win) return { missing: true };
    const body = win.querySelector('.window-body');
    // Modules inject their stylesheet as the first child; that is not the UI root.
    const root = body && [...body.children].find((el) => !/^(STYLE|SCRIPT|LINK)$/.test(el.tagName));
    if (!root) return { empty: true };
    const cs = getComputedStyle(root);
    const painted = cs.backgroundImage !== 'none'
        || (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent');
    const buttons = [...root.querySelectorAll('button,a')];
    const hooked = buttons.filter((el) => [...el.attributes].some((a) => a.name.startsWith('data-')));
    return {
        empty: false,
        rootClass: root.className || root.tagName,
        painted,
        background: cs.backgroundColor,
        font: cs.fontFamily.slice(0, 40),
        textLen: (root.innerText || '').trim().length,
        testids: root.querySelectorAll('[data-testid]').length,
        buttons: buttons.length,
        hooked: hooked.length,
        height: Math.round(root.getBoundingClientRect().height),
    };
}
"""

NAV_PROBE = """
(id) => {
    const win = document.querySelector('[data-testid="window-' + id + '"]');
    const root = win && win.querySelector('.window-body');
    if (!root) return [];
    const nodes = root.querySelectorAll(
        'nav button, [role="tab"], [data-tab], [class*="nav-item"], [class*="side"] button');
    return [...new Set(nodes)].map((el, i) => ({
        i,
        label: (el.dataset.testid || el.textContent || '').trim().slice(0, 28),
    })).slice(0, 10);
}
"""

NAV_CLICK = """
([id, index]) => {
    const win = document.querySelector('[data-testid="window-' + id + '"]');
    const root = win && win.querySelector('.window-body');
    if (!root) return false;
    const nodes = [...new Set(root.querySelectorAll(
        'nav button, [role="tab"], [data-tab], [class*="nav-item"], [class*="side"] button'))];
    const el = nodes[index];
    if (!el || el.disabled) return false;
    el.click();
    return true;
}
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def native_packages() -> list[Path]:
    out = []
    for pkg_dir in sorted(p for p in REPO.iterdir() if p.is_dir()):
        manifest_path = pkg_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (manifest.get("ui") or {}).get("mode") == "native":
            out.append(pkg_dir)
    return out


def seed(pkg_dir: Path) -> dict:
    """Install a package locally from the checkout, exactly like an install job."""
    manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    pkg_id = str(manifest["id"])
    target = packages_store.package_dir(pkg_id)
    target.mkdir(parents=True, exist_ok=True)
    files = []
    for item in manifest.get("files") or []:
        rel = str((item or {}).get("path") if isinstance(item, dict) else item)
        source = pkg_dir / rel
        if not rel or not source.is_file():
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        files.append({"path": rel, "url": "local", "sha256": "0" * 64,
                      "mode": int((item or {}).get("mode") or 0o644) if isinstance(item, dict) else 0o644})
    record = {
        "id": pkg_id,
        "name": manifest.get("name") or pkg_id,
        "version": manifest.get("version") or "0.0.0",
        "icon": manifest.get("icon") or "🧩",
        "icon_url": manifest.get("icon_url") or "",
        "description": manifest.get("description") or "",
        "trust": "pinned",
        "user_access": bool(manifest.get("user_access")),
        "ui": manifest.get("ui") or {},
        "files": files,
    }
    if manifest.get("python"):
        record["python"] = manifest["python"]
    packages_store.add_package(record)
    return {"id": pkg_id, "name": record["name"], "service": bool(
        (manifest.get("ui") or {}).get("port") or (manifest.get("python") or {}).get("port"))}


def main() -> int:
    if not (REPO / "index.json").is_file():
        print(f"SKIP: no package repository at {REPO} (set ETHOS_QA_REPO)")
        return 0
    dirs = native_packages()
    if not dirs:
        print(f"SKIP: no native packages in {REPO}")
        return 0
    seeded = [seed(pkg_dir) for pkg_dir in dirs]
    print(f"seeded {len(seeded)} native package(s) from {REPO}")

    app_port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
         "--port", str(app_port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{app_port}"
    token = _create_token("qa-sweep", "qa-sweep", "admin")
    findings: list[dict] = []
    failures = 0

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

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            page_errors: list[str] = []
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            click_nav = os.environ.get("ETHOS_QA_CLICK_NAV", "1") != "0"
            page.goto(base)
            page.wait_for_function("typeof window.openApp === 'function'", timeout=20000)
            theme = os.environ.get("ETHOS_QA_THEME", "light")
            if theme == "dark":
                page.evaluate("() => { document.documentElement.classList.remove('light');"
                              "               document.documentElement.classList.add('dark'); }")

            print(f"\n{'package':24}{'painted':9}{'text':7}{'testids':7}{'hooks':10}{'views':6}root")
            for pkg in seeded:
                app_id = f"pkg-{pkg['id']}"
                page_errors.clear()
                console_errors.clear()
                page.evaluate("id => { window.__qaApp = id; window.openApp(id); }", app_id)
                probe: dict = {}
                try:
                    page.wait_for_function(
                        "id => { const w = document.querySelector('[data-testid=\"window-' + id + '\"]');"
                        "        return w && w.querySelector('.window-body')"
                        "            && w.querySelector('.window-body').firstElementChild; }",
                        arg=app_id, timeout=15000)
                    page.wait_for_timeout(1200 if pkg["service"] else 700)
                    probe = page.evaluate(PROBE)
                except Exception as exc:  # noqa: BLE001 - a timeout is a finding
                    probe = {"empty": True, "error": str(exc).splitlines()[0][:120]}

                # Walk every rail/tab view: a view that throws on render is a real defect
                # and the first screen never shows it.
                views = 0
                if click_nav and not probe.get("empty"):
                    for item in page.evaluate(NAV_PROBE, app_id):
                        if page.evaluate(NAV_CLICK, [app_id, item["i"]]):
                            views += 1
                            page.wait_for_timeout(350)

                expected_proxy = pkg["service"]
                # 503 is "the service or host tool behind this view is not available here"
                # (no package services, no Samba on a throwaway instance) — informational.
                unavailable = [e for e in console_errors if "503" in e]
                real_console = [e for e in console_errors
                                if "503" not in e
                                and not (expected_proxy and "Failed to load resource" in e)]
                painted = bool(probe.get("painted"))
                text_len = int(probe.get("textLen") or 0)
                print(f"{pkg['id']:24}{str(painted):9}{text_len:<7}"
                      f"{str(probe.get('testids', '-')):>7}  "
                      f"{str(probe.get('hooked', 0)) + '/' + str(probe.get('buttons', 0)):9}"
                      f"{views:<6}"
                      f"{str(probe.get('rootClass') or probe.get('error') or '-')[:40]}")

                if page_errors:
                    failures += 1
                    findings.append({"id": pkg["id"], "level": "error", "kind": "pageerror",
                                     "detail": page_errors[:3]})
                if real_console:
                    level = "warn" if expected_proxy else "error"
                    if level == "error":
                        failures += 1
                    findings.append({"id": pkg["id"], "level": level, "kind": "console",
                                     "detail": real_console[:3]})
                if unavailable:
                    findings.append({"id": pkg["id"], "level": "info", "kind": "unavailable",
                                     "detail": unavailable[:2]})
                if probe.get("empty"):
                    failures += 1
                    findings.append({"id": pkg["id"], "level": "error", "kind": "no-content",
                                     "detail": probe.get("error") or "empty window body"})
                elif not painted and pkg["id"] not in LEGACY:
                    findings.append({"id": pkg["id"], "level": "warn", "kind": "unstyled",
                                     "detail": f"root {probe.get('rootClass')} paints no background"})
                if not text_len and not probe.get("empty"):
                    findings.append({"id": pkg["id"], "level": "warn", "kind": "no-text",
                                     "detail": "window rendered without any text"})
                if (text_len and not probe.get("testids") and not probe.get("hooked")
                        and pkg["id"] not in LEGACY):
                    findings.append({"id": pkg["id"], "level": "warn", "kind": "no-testid",
                                     "detail": "no data-testid or action attribute on any "
                                               "interactive element"})
                if SHOTS:
                    SHOTS.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(SHOTS / f"{pkg['id']}.png"))
                page.evaluate("id => { const w = document.querySelector('[data-testid=\"window-' + id + '\"]');"
                              "        if (w) w.querySelector('.close-btn')?.click(); }", app_id)
                page.wait_for_timeout(150)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DATA, ignore_errors=True)

    out = Path("/tmp/ethos-qa-ui.json")
    out.write_text(json.dumps({"findings": findings}, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(findings)} finding(s), {failures} hard failure(s) — wrote {out}")
    for item in findings:
        print(f"  {item['level']:5} {item['id']:22} {item['kind']}: {str(item['detail'])[:110]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
