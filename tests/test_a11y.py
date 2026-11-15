#!/usr/bin/env python3
"""Accessibility invariants (TODO.md P3-11).

Static guard — no server needed:

    .venv/bin/python tests/test_a11y.py

The live audit was run with axe-core 4.10 (WCAG 2 A/AA) against an isolated
instance with Files, Settings, Process Manager, Docker, Cron, Kanban,
Storage and Music open: **0 violations in both the light and the dark theme**.
This file guards the things that regress silently (dialog semantics, the
Escape handler, the live region, the palette values, unnamed form controls).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def contrast(fg: str, bg: str) -> float:
    def lum(hex_color: str) -> float:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    html = (ROOT / "frontend" / "index.html").read_text()
    modal = (STATIC / "core" / "modal.js").read_text()
    confirm = (STATIC / "core" / "unified-confirm.js").read_text()
    manager = (STATIC / "core" / "window-manager.js").read_text()

    # ── shared dialogs ────────────────────────────────────────────────────
    for needle, why in [
        ("setAttribute('role', 'dialog')", "modal has role=dialog"),
        ("setAttribute('aria-modal', 'true')", "modal is aria-modal"),
        ("aria-labelledby", "modal is labelled by its title"),
        ("_focusables(box)", "modal traps Tab"),
        ("_lastFocused", "modal restores focus on close"),
        ("addEventListener('keydown', _kd, true)", "modal handles Escape in capture phase"),
        ("aria-label=\"Close dialog\"", "modal close button is labelled"),
    ]:
        check(why, needle in modal, needle)

    check("confirm dialog is an alertdialog", "setAttribute('role', 'alertdialog')" in confirm)
    check("confirm Escape is not once-only",
          "{ once: true }" not in re.sub(r"//[^\n]*", "", confirm),
          "still binds the handler with { once: true }")
    check("confirm restores focus", "opener.focus()" in confirm)

    # ── live region, focus rings, theme ───────────────────────────────────
    check("toasts are announced (aria-live)", 'aria-live="polite"' in html and 'role="status"' in html)
    check("keyboard focus is visible", ":focus-visible" in html)
    check("reduced motion is respected", "prefers-reduced-motion" in html)

    # `--bg` was missing, so every app fell back to a hardcoded dark colour and
    # the *light* theme rendered dark windows with dark text (1.49:1).
    check("--bg is defined for both themes", html.count("--bg:") >= 3, f"{html.count('--bg:')} definitions")

    def theme_var(block: str, name: str) -> str | None:
        m = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
        return m.group(1) if m else None

    light_block = html.split(":root {", 1)[1].split("}", 1)[0]
    dark_block = html.split(":root.dark {", 1)[1].split("}", 1)[0]
    for name, minimum in (("--text-2", 4.5), ("--text-3", 4.5)):
        light = theme_var(light_block, name)
        dark = theme_var(dark_block, name)
        check(f"light {name} ≥ {minimum}:1 on white",
              bool(light) and contrast(light, "#ffffff") >= minimum, f"{light}")
        check(f"dark {name} ≥ {minimum}:1 on --surface-2",
              bool(dark) and contrast(dark, "#1a2332") >= minimum, f"{dark}")

    # ── window chrome ─────────────────────────────────────────────────────
    check("window controls are labelled", manager.count('aria-label="Close window"') >= 1
          and 'aria-label="Minimize window"' in manager)
    check("taskbar items are keyboard operable", "setAttribute('role', 'button')" in manager
          and "onkeydown" in manager)

    # ── form controls have names ──────────────────────────────────────────
    unnamed = []
    for path in sorted((STATIC / "apps").glob("*.js")):
        for m in re.finditer(r"<select\b[^>]*>", path.read_text()):
            tag = m.group(0)
            if "aria-label" not in tag and "aria-labelledby" not in tag:
                unnamed.append(f"{path.name}: {tag[:50]}")
    check("every <select> has an accessible name", not unnamed, "; ".join(unnamed[:3]))

    # ── app shell accessibility (docs/app-ui.md) ──────────────────────────
    shell_js = (STATIC / "core" / "ui-shell.js").read_text()
    ui_css = (STATIC / "css" / "ethos-ui.css").read_text()
    check("app shell marks the active section", "aria-current" in shell_js)
    check("app shell announces status changes",
          'role="status"' in shell_js and 'aria-live="polite"' in shell_js)
    check("app shell keeps a visible focus ring", ":focus-visible" in ui_css)

    settings_src = (STATIC / "apps" / "settings.js").read_text()
    no_hook = [tag for tag in re.findall(r"<button\b[^>]*>", settings_src)
               if "data-testid" not in tag and "data-action" not in tag and "data-wp" not in tag]
    check("Settings buttons have a stable hook", not no_hook, "; ".join(no_hook[:3]))
    unnamed_inputs = [tag for tag in re.findall(r"<input\b[^>]*>", settings_src)
                      if "aria-label" not in tag and "id=" not in tag]
    check("Settings inputs have an accessible name", not unnamed_inputs, "; ".join(unnamed_inputs[:3]))

    # ── chips carry white text on AA-safe colours ─────────────────────────
    # The Kanban board is an `ethos-kanban` package now, so the source only
    # exists in a sibling checkout; CI has no packages checkout and skips it.
    kanban = (Path(__file__).resolve().parent.parent.parent
              / "ethos-packages" / "ethos-kanban" / "frontend" / "app.js")
    if kanban.is_file():
        kanban_src = kanban.read_text()
        m = re.search(r"LABEL_COLORS = \[([^\]]+)\]", kanban_src)
        colors = re.findall(r"#[0-9a-fA-F]{6}", m.group(1)) if m else []
        low = [c for c in colors if contrast("#ffffff", c) < 4.5]
        check("kanban label chips pass AA with white text", colors and not low, ", ".join(low) or "no colours")
    else:
        print("  [SKIP] kanban label chips (ethos-kanban package not checked out)")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
