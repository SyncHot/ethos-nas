#!/usr/bin/env python3
"""Shared UI states (TODO.md P3-12).

Static + a node render check (no server needed):

    .venv/bin/python tests/test_ui_states.py

Guards that the three states live in one module, that no app still renders the
old ad-hoc placeholders, and that the helpers escape their inputs.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def static_checks() -> None:
    states = (STATIC / "core" / "states.js").read_text()
    for name in ("skeletonRows", "emptyState", "errorState", "wireStateActions", "resultCount"):
        check(f"states.js exports {name}()", f"export function {name}(" in states, name)

    apps = sorted((STATIC / "apps").glob("*.js"))
    adopters = [p.name for p in apps if "core/states.js" in p.read_text()]
    check("the state helpers are adopted broadly", len(adopters) >= 10, f"{len(adopters)}: {', '.join(adopters)}")

    legacy = []
    for path in apps:
        text = path.read_text()
        if re.search(r'<div class="spinner"></div>\s*Loading', text):
            legacy.append(f"{path.name}: spinner+Loading")
        if re.search(r'text-align:center;padding:40px;">Loading', text.replace(" ", "")):
            legacy.append(f"{path.name}: bare Loading block")
    check("no ad-hoc loading placeholders left", not legacy, "; ".join(legacy))

    check("error state escapes the server detail", "escapeHtml(String(detail))" in states)
    check("skeleton is announced to screen readers", 'role="status"' in states and "aria-busy" in states)
    check("empty/error states expose an action hook", "data-state-action" in states)
    check("reduced-motion users do not get the shimmer", "prefers-reduced-motion" in
          (ROOT / "frontend" / "index.html").read_text())


def runtime_check() -> None:
    node = shutil.which("node")
    if not node:
        check("node available", False, "node not on PATH")
        return
    script = """
globalThis.document = {
  createElement: () => {
    let t = '';
    return {
      set textContent(v) { t = String(v); },
      get innerHTML() { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); },
    };
  },
};
const m = await import('./frontend/static/core/states.js');
const out = {
  rows: (m.skeletonRows(3).match(/ethos-skeleton-row/g) || []).length,
  tiles: (m.skeletonRows(2, { variant: 'tiles' }).match(/ethos-skel"/g) || []).length,
  empty: m.emptyState({ icon: 'folder', title: 'No jobs', hint: 'Add one', action: { label: 'New', id: 'create' } }),
  err: m.errorState({ detail: '<script>x</script>' }),
  count: m.resultCount(50, 1204),
};
console.log(JSON.stringify({ rows: out.rows, tiles: out.tiles,
  action: out.empty.includes('data-state-action="create"'), title: out.empty.includes('No jobs'),
  svg: out.empty.includes('<svg'), escaped: out.err.includes('&lt;script&gt;'),
  retry: out.err.includes('data-state-action="retry"'), count: out.count }));
"""
    proc = subprocess.run([node, "--input-type=module", "-e", script], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        check("states render", False, proc.stderr.strip()[:300])
        return
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    check("skeletonRows renders n rows", out["rows"] == 3, str(out["rows"]))
    check("skeletonRows tiles variant", out["tiles"] == 4, str(out["tiles"]))
    check("emptyState renders an action button", out["action"])
    check("emptyState keeps the title", out["title"])
    check("emptyState draws a glyph", out["svg"])
    check("errorState escapes raw HTML in detail", out["escaped"])
    check("errorState offers retry", out["retry"])
    check("resultCount formats the totals", "Showing 50 of 1204" in out["count"], out["count"])


def main() -> int:
    static_checks()
    runtime_check()
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
