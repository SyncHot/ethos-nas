#!/usr/bin/env python3
"""Static guards — one global notification system for every app and package.

The rule (AGENTS.md → Notifications, `.github/instructions/notifications.instructions.md`):
apps and packages never render their own toast, notification panel or event
history. They call `notify()` / `addNotification()` from `core/notification-center.js`
and the backend publishes through `emit_event(...)`.

No server needed.

Run:  .venv/bin/python tests/test_notification_ui.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"

CORE = "frontend/static/core/notification-center.js"
INDEX_HTML = "frontend/index.html"
APP_JS = "frontend/static/app.js"
WINDOW_MANAGER = "frontend/static/core/window-manager.js"
INSTRUCTIONS = ".github/instructions/notifications.instructions.md"
FRONTEND_INSTRUCTIONS = ".github/instructions/frontend.instructions.md"
AGENTS = "AGENTS.md"
DOC = "docs/notifications.md"
SKILL_FRONTEND = ".github/skills/frontend-app/SKILL.md"
SKILL_PACKAGE = ".github/skills/add-package/SKILL.md"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _sources() -> list[Path]:
    return sorted(STATIC.rglob("*.js"))


def main() -> int:
    core = _read(CORE)
    index_html = _read(INDEX_HTML)
    app_js = _read(APP_JS)
    window_manager = _read(WINDOW_MANAGER)

    # ── 1. Public API ──────────────────────────────────────────────────────
    for fn in ("notify", "addNotification", "dismissNotification", "showUnifiedToast",
               "openNotifications", "closeNotifications", "toggleNotificationDrawer",
               "getUnreadCount", "pollUnreadCount", "startNotificationPolling",
               "connectNotificationStream", "markAllRead", "clearAll"):
        check(f"notification-center.js exports {fn}",
              re.search(rf"export (async )?(function|const) {fn}\b", core) is not None)
    check("legacy showToast alias kept for existing call sites",
          "export const showToast = showUnifiedToast" in core)
    check("legacy reversed argument order still tolerated",
          "NOTIFICATION_TYPES.includes(msg) && !NOTIFICATION_TYPES.includes(type)" in core)

    # ── 2. One badge writer ────────────────────────────────────────────────
    check("the badge element is written in exactly one place",
          core.count("'notificationCount'") == 1, str(core.count("'notificationCount'")))
    offenders = [str(p.relative_to(ROOT)) for p in _sources()
                 if p.name != "notification-center.js" and "notificationCount" in p.read_text(encoding="utf-8")]
    check("no other module touches #notificationCount", not offenders, ", ".join(offenders))

    # ── 3. One toast/history implementation ────────────────────────────────
    offenders = [str(p.relative_to(ROOT)) for p in _sources()
                 if p.name != "notification-center.js"
                 and re.search(r"export (function|const) showToast\b|toast-container|notification-history-item|notification-drawer",
                               p.read_text(encoding="utf-8"))]
    check("only core/notification-center.js renders toasts or history rows",
          not offenders, ", ".join(offenders))

    offenders = [str(p.relative_to(ROOT)) for p in _sources()
                 if p.name != "notification-center.js"
                 and re.search(r"new Notification\(|Notification\.requestPermission", p.read_text(encoding="utf-8"))]
    check("only core/notification-center.js uses the desktop Notification API",
          not offenders, ", ".join(offenders))

    panel_offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        if path.name == "notification-center.js":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"position:\s*fixed[^;]*top:\s*calc\(var\(--topbar-h\)", text):
            panel_offenders.append(str(path.relative_to(ROOT)))
    check("no app builds a fixed notification panel", not panel_offenders,
          ", ".join(panel_offenders))

    # ── 4. Escaping ────────────────────────────────────────────────────────
    check("event text is escaped before it reaches innerHTML",
          "escapeHtml(event.title" in core and "escapeHtml(event.message" in core)
    check("data attributes use escapeAttr", "escapeAttr(event.id)" in core)

    # ── 5. Markup contract (stable test anchors, a11y) ─────────────────────
    check("bell lives in the top bar with a stable test id",
          'data-testid="notification-trigger"' in index_html
          and 'aria-controls="notificationDrawer"' in index_html)
    check("bell exposes aria-haspopup + aria-expanded",
          'aria-haspopup="dialog"' in index_html and 'aria-expanded="false"' in index_html)
    check("exactly one badge element", index_html.count('id="notificationCount"') == 1)
    check("sheet is a labelled dialog",
          'id="notificationDrawer"' in index_html and 'role="dialog"' in index_html
          and 'aria-labelledby="notificationDrawerTitle"' in index_html)
    check("toasts stay in a polite live region",
          'id="toastContainer"' in index_html and 'aria-live="polite"' in index_html
          and 'role="status"' in index_html)
    check("filters + search are exposed to tests",
          'data-testid="notif-filter-all"' in index_html
          and 'data-testid="notif-filter-unread"' in index_html
          and 'data-testid="notif-filter-errors"' in index_html
          and 'data-testid="notif-search"' in index_html)
    check("no emoji bell left in the shell", "\N{BELL}" not in index_html)
    check("taskbar no longer builds the bell",
          "notificationDrawerTrigger" not in window_manager
          and "notificationCount" not in window_manager)

    # ── 6. Package / app integration ───────────────────────────────────────
    check("app.js opens the notification stream on bootstrap",
          "startNotificationPolling()" in app_js and "startNotificationPolling" in app_js)
    for member in ("toast:", "emit:", "open:"):
        check(f"native package ctx exposes notifications.{member[:-1]}", member in app_js)
    check("package emit posts to the shared endpoint",
          "'/api/notifications'" in app_js and "package_id: pkg.id" in app_js)

    # ── 7. Rules + docs exist so future agents find the system ─────────────
    check("docs/notifications.md exists", (ROOT / DOC).is_file())
    check("instructions file exists", (ROOT / INSTRUCTIONS).is_file())
    if (ROOT / INSTRUCTIONS).is_file():
        instructions = _read(INSTRUCTIONS)
        check("instructions are path-scoped to the frontend",
              re.search(r"applyTo:\s*.*frontend/static", instructions) is not None)
        check("instructions point at the canonical module",
              "core/notification-center.js" in instructions)
    check("AGENTS.md links the spec", "docs/notifications.md" in _read(AGENTS))
    check("frontend instructions name the single history UI",
          "notification-center.js" in _read(FRONTEND_INSTRUCTIONS))
    check("frontend-app skill teaches the shared API",
          "notification-center.js" in _read(SKILL_FRONTEND)
          and "notify({" in _read(SKILL_FRONTEND))
    check("add-package skill forbids package-local notification UI",
          "ctx.notifications" in _read(SKILL_PACKAGE))
    check("spec documents the package contract",
          "ctx.notifications" in _read(DOC) and "/ws/notifications" in _read(DOC))

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
