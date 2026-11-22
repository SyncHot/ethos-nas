#!/usr/bin/env python3
"""Frontend escaping invariants (TODO.md P3-02).

Guards the three rules that keep HTML injection out of the SPA:

1. `escapeAttr()` neutralises `& < > " '` — the old version missed `&` and `'`.
2. Attribute values are escaped with `escapeAttr`, never with the text-only
   `escapeHtml` (which leaves quotes intact).
3. Values interpolated into an inline handler's JS string (`onclick="fn('…')"`)
   go through `jsAttr`, not `escapeAttr` — HTML unescaping would otherwise hand
   the quote back to the JS parser.

    .venv/bin/python tests/test_frontend_escaping.py
"""

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


def runtime_checks() -> None:
    node = shutil.which("node")
    if not node:
        check("node available", False, "node not on PATH — skipping runtime checks")
        return
    script = """
// Minimal DOM stub: textContent setter + innerHTML getter, like a browser
// (escapes & < > — and deliberately NOT quotes, which is the whole point of
// having a separate escapeAttr).
globalThis.document = {
  createElement: () => {
    let text = '';
    return {
      set textContent(v) { text = String(v); },
      get innerHTML() {
        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
};
const { escapeAttr, jsAttr, escapeHtml } = await import('./frontend/static/core/utils.js');
const cases = [
  [escapeAttr('a&b"c\\'d<e>'), 'a&amp;b&quot;c&#39;d&lt;e&gt;'],
  [escapeAttr("&quot;"), '&amp;quot;'],
  [escapeAttr(0), '0'],
  [escapeAttr(null), ''],
  [escapeAttr('plain'), 'plain'],
];
let bad = [];
for (const [got, want] of cases) if (got !== want) bad.push(`${JSON.stringify(got)} != ${JSON.stringify(want)}`);
// jsAttr must survive both the HTML attribute and the JS string layer.
const payload = `x'); alert(1); //<script>`;
const attr = jsAttr(payload);
if (attr.includes("'") && !attr.includes('&#39;')) bad.push('jsAttr leaked a raw quote');
if (attr.includes('<') || attr.includes('>')) bad.push('jsAttr leaked angle brackets');
console.log(JSON.stringify({ bad, attr, html: escapeHtml('<b>&') }));
"""
    proc = subprocess.run([node, "--input-type=module", "-e", script], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        check("escaping runtime", False, proc.stderr.strip()[:300])
        return
    import json
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    check("escapeAttr escapes &, quotes and brackets", not out["bad"], "; ".join(out["bad"]))
    check("escapeHtml still escapes text markup", out["html"] == "&lt;b&gt;&amp;", out["html"])


def static_checks() -> None:
    files = sorted(STATIC.rglob("*.js"))
    attr_text_escape = re.compile(r'="[^"]*\$\{[^}]*escapeHtml\(')
    handler_attr_escape = re.compile(r"""on[a-z]+="[^"]*'\$\{escapeAttr\(""")

    bad_attr = []
    bad_handler = []
    for path in files:
        text = path.read_text()
        if attr_text_escape.search(text):
            bad_attr.append(path.name)
        if handler_attr_escape.search(text):
            bad_handler.append(path.name)
    check("no escapeHtml() inside attribute values", not bad_attr, ", ".join(bad_attr))
    check("inline handlers use jsAttr, not escapeAttr", not bad_handler, ", ".join(bad_handler))

    # One implementation only: the shared helper, not per-app copies.
    local_copies = []
    old_variant = re.compile(r"replace\(/&/g, '&amp;'\)\.replace\(/\"/g, '&quot;'\)")
    for path in files:
        if path.name == "utils.js":
            continue
        text = path.read_text()
        if re.search(r"function escapeAttr\(", text) or old_variant.search(text):
            local_copies.append(path.name)
    check("escapeAttr is defined once (core/utils.js)", not local_copies, ", ".join(local_copies))

    # Every name imported from a core module must actually be exported there —
    # a dangling import is a runtime ReferenceError the browser shows at boot.
    core_exports: dict[str, set[str]] = {}
    for core in (STATIC / "core").glob("*.js"):
        text = core.read_text()
        names = set(re.findall(r"export\s+(?:async\s+)?function\s+(\w+)", text))
        names |= set(re.findall(r"export\s+const\s+(\w+)", text))
        names |= set(re.findall(r"export\s+let\s+(\w+)", text))
        names |= set(re.findall(r"export\s+class\s+(\w+)", text))
        for group in re.findall(r"export\s*\{([^}]*)\}", text):
            for item in group.split(","):
                item = item.strip()
                if item:
                    names.add(item.split(" as ")[-1].strip())
        core_exports[core.name] = names

    dangling = []
    import_re = re.compile(r"import \{([^}]*)\} from '(\.[^']*)';")
    core_dir = (STATIC / "core").resolve()
    for path in files:
        for names_blob, target in import_re.findall(path.read_text()):
            # Resolve the specifier: `./api.js` in music/ is NOT core/api.js.
            resolved = (path.parent / target).resolve()
            if resolved.parent != core_dir:
                continue
            base = resolved.name
            if base not in core_exports:
                continue
            for item in names_blob.split(","):
                name = item.strip().split(" as ")[0].strip()
                if name and name not in core_exports[base]:
                    dangling.append(f"{path.name}: {name} from {base}")
    check("no dangling imports from core modules", not dangling, "; ".join(dangling[:5]))


def main() -> int:
    runtime_checks()
    static_checks()
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
