#!/usr/bin/env python3
"""Static guard — one window shell for every core desktop app.

The rule (AGENTS.md → App windows, `.github/instructions/app-ui.instructions.md`,
`docs/app-ui.md`): every app is built with `mountShell()` from
`frontend/static/core/ui-shell.js` and the `.ui-*` classes in
`frontend/static/css/ethos-ui.css`, matching the native package windows. Apps
must not inject `<style>`, hardcode colours, or use the legacy
`glass-card`/`app-tab` blocks.

`PENDING` is the migration allowlist: files listed there are still on the old
layout. It only ever shrinks — the test fails if an entry disappears or a new
app ships without the shell.

No server needed.

Run:  .venv/bin/python tests/test_app_ui.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
APPS = STATIC / "apps"

CSS = "frontend/static/css/ethos-ui.css"
SHELL = "frontend/static/core/ui-shell.js"
INDEX_HTML = "frontend/index.html"
INSTRUCTIONS = ".github/instructions/app-ui.instructions.md"
AGENTS = "AGENTS.md"
DOC = "docs/app-ui.md"
SKILL = ".github/skills/app-ui/SKILL.md"

# Apps that must already use the shell.
SHELL_APPS = {
    "settings.js": "rail",
    "storage.js": "rail",
    "network.js": "rail",
    "access-control.js": "rail",
    "packages.js": "rail",
    "logs.js": "rail",
    "overview.js": "single",
    "processes.js": "single",
    "services.js": "single",
    "smart.js": "single",
    "systemd.js": "single",
    "cron.js": "single",
    "search.js": "single",
    "share-links.js": "single",
}

# Migration allowlist (TODO.md P3-15). Empty as of 2026-11-15: every core app
# uses the shell; Files/Terminal/Login are documented layout exceptions.
PENDING: set[str] = set()

# Documented layout exceptions — tokens only, never the rail.
LAYOUT_EXCEPTIONS = {"files.js", "terminal.js", "login.js", "app-drawer.js", "fm-controls.js"}

SETTINGS_SECTIONS = ("overview", "updates", "maintenance", "users", "security", "appearance", "about")
SETTINGS_TESTIDS = (
    "settings-caches-card",
    "core-update-card",
    "core-update-status",
    "core-update-check",
    "core-update-apply",
    "health-config-row",
    "health-cfg-save",
    "apk-link-moments",
    "apk-link-music",
    "apk-link-video",
    "settings-users-add",
    "settings-users-refresh",
    "settings-users-table",
    "settings-users-helper-note",
)

HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RAW_COLOUR = re.compile(r"color\s*:\s*(?:#|white\b|black\b)", re.IGNORECASE)
INLINE_STYLE = re.compile(r"<style[\s>]|createElement\(['\"]style['\"]\)")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    # ── 1. Foundation files ────────────────────────────────────────────────
    css_path = ROOT / CSS
    shell_path = ROOT / SHELL
    check("shared stylesheet exists", css_path.is_file(), CSS)
    check("shared shell helper exists", shell_path.is_file(), SHELL)
    if not (css_path.is_file() and shell_path.is_file()):
        return 1

    css = _read(CSS)
    shell = _read(SHELL)
    index_html = _read(INDEX_HTML)

    check("index.html links the app shell stylesheet", "/static/css/ethos-ui.css" in index_html)
    for selector in (".ui-shell", ".ui-rail", ".ui-main", ".ui-panel", ".ui-nav-item",
                     ".ui-pill", ".ui-table", ".ui-summary", ".ui-tag", ".ui-bar",
                     ".ui-fields", ".ui-empty"):
        check(f"stylesheets define {selector}", selector in css)
    for metric in ("width: 218px", "padding: 24px 28px 32px", "border-radius: 15px",
                   "max-width: 780px"):
        check(f"stylesheet keeps the package metric '{metric}'", metric in css)

    check("ui-shell exports mountShell", re.search(r"export function mountShell\b", shell) is not None)
    check("shell exposes section/status/pill handles",
          all(token in shell for token in ("setActive", "setStatus", "setPill", "onSection")))
    check("shell keeps aria-current on the active section", "aria-current" in shell)
    check("shell adds data-testid hooks", "data-testid" in shell)
    check("shell does not create window globals", "window." not in shell)

    # ── 2. The rule is documented everywhere ──────────────────────────────
    for rel, label in ((INSTRUCTIONS, "instruction"), (DOC, "spec"), (SKILL, "skill")):
        check(f"app-ui {label} exists", (ROOT / rel).is_file(), rel)
    check("AGENTS.md has the App windows hard rule", "**App windows.**" in _read(AGENTS))
    check("AGENTS.md skill table links app-ui", "| App window layout / styling / design system | `app-ui` |" in _read(AGENTS))
    check("frontend rules point at the shell", "Window layout (mandatory)" in _read(
        ".github/instructions/frontend.instructions.md"))

    # ── 3. Allowlist is live ───────────────────────────────────────────────
    app_files = {path.name for path in APPS.glob("*.js")}
    stale = sorted(name for name in PENDING if name not in app_files)
    check("PENDING only lists existing apps", not stale, ", ".join(stale))
    unknown = sorted(name for name in SHELL_APPS if name not in app_files)
    check("SHELL_APPS only lists existing apps", not unknown, ", ".join(unknown))

    # ── 4. Migrated apps follow the contract ──────────────────────────────
    style_offenders = [name for name in sorted(app_files)
                       if INLINE_STYLE.search((APPS / name).read_text(encoding="utf-8"))]
    check("no app injects <style> at runtime", not style_offenders, ", ".join(style_offenders))

    migrated = sorted(name for name in SIMPLE_APPS if name not in PENDING)
    for name in migrated:
        text = (APPS / name).read_text(encoding="utf-8")
        check(f"{name} imports mountShell", "from '../core/ui-shell.js'" in text)
        check(f"{name} has no injected <style>", not INLINE_STYLE.search(text))
        check(f"{name} has no glass-card", "glass-card" not in text)
        check(f"{name} has no app-tab strip", "app-tab" not in text)
        check(f"{name} has no hardcoded hex colour", not HEX_COLOUR.search(text),
              ", ".join(HEX_COLOUR.findall(text)[:3]))
        check(f"{name} has no raw white/black text colour", not RAW_COLOUR.search(text))

    # Exceptions never grow: every non-exception app must either use the shell
    # or be on the allowlist.
    for name in sorted(app_files):
        if name in LAYOUT_EXCEPTIONS or name in PENDING:
            continue
        check(f"{name} is covered by the shell contract", name in SHELL_APPS, name)

    # ── 5. Settings is the reference implementation ────────────────────────
    settings = (APPS / "settings.js").read_text(encoding="utf-8")
    check("Settings uses mountShell", "mountShell(" in settings)
    for section in SETTINGS_SECTIONS:
        check(f"Settings has the {section} section", f"id: '{section}'" in settings)
    for testid in SETTINGS_TESTIDS:
        check(f"Settings keeps data-testid {testid}", testid in settings)
    check("Settings has no window._ethos* globals", "window._ethos" not in settings)
    check("Settings has no inline onclick handlers", "onclick=" not in settings)
    check("Settings has no inline style attributes", 'style="' not in settings)
    check("Settings has no hardcoded hex colour", not HEX_COLOUR.search(settings),
          ", ".join(HEX_COLOUR.findall(settings)[:3]))
    check("Settings has no raw white/black text colour", not RAW_COLOUR.search(settings))

    print()
    if failures:
        print(f"{len(failures)} app-UI check(s) failed")
        return 1
    print("All app-UI checks passed")
    return 0


SIMPLE_APPS = tuple(
    name for name in sorted(SHELL_APPS)
    if name not in {"files.js", "terminal.js", "login.js", "app-drawer.js", "fm-controls.js"}
)


if __name__ == "__main__":
    sys.exit(main())
