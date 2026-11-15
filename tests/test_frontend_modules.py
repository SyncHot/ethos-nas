"""Guard: the frontend ES-module graph must be complete after every change.

Static, no server and no browser needed. A single missing file or a renamed
export breaks the *whole* module graph — the browser aborts the failed module,
`app.js` never evaluates and the desktop renders empty with no drawer icons and
no user menu (incident 2026-11-15: `apps/files.js` still imported
`apps/editor.js` after the editor moved to a Packages package).

Checks:
  1. every relative `import ... from './x.js'` resolves to an existing file;
  2. every named import is actually exported by the target module;
  3. `index.html` and `sw.js` do not reference `.js` files that no longer exist;
  4. no module imports a desktop app that moved into a package.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
INDEX = ROOT / "frontend" / "index.html"
SW = ROOT / "frontend" / "sw.js"

# Apps whose UI now lives in a Packages package (see docs/app-store.md).
MOVED_TO_PACKAGES = {
    "editor",
    "gallery",
    "music",
    "videos",
    "video-station",
    "kanban",
    "cleaner",
    "shares",
    "git-diff",
    "surveillance",
    "docker",
    "sticky-notes",
}

IMPORT_RE = re.compile(
    r"""import\s*(?:\*\s*as\s*[\w$]+|\{([^}]*)\}|([\w$]+))\s*from\s*['"](\.[^'"]+)['"]"""
)
ANY_IMPORT_RE = re.compile(r"""(?:from|import)\s*\(?\s*['"](\.[^'"]+)['"]""")
EXPORT_DECL_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)"
)
EXPORT_LIST_RE = re.compile(r"export\s*\{([^}]+)\}")
JS_REF_RE = re.compile(r"""['"]([^'"]*\.js)['"]""")


def _modules() -> list[Path]:
    return sorted(p for p in STATIC.rglob("*.js") if p.is_file())


def _exports(path: Path, src: str) -> set[str]:
    names = {m.group(1) for m in EXPORT_DECL_RE.finditer(src)}
    for match in EXPORT_LIST_RE.finditer(src):
        for part in match.group(1).split(","):
            alias = part.strip().split(" as ")[-1].strip()
            if alias:
                names.add(alias)
    if re.search(r"\bexport\s+default\b", src):
        names.add("default")
    return names


def _check_imports(errors: list[str]) -> int:
    modules = _modules()
    export_index = {p.resolve(): _exports(p, p.read_text(encoding="utf-8")) for p in modules}
    checked = 0

    for path in modules:
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)

        for match in ANY_IMPORT_RE.finditer(src):
            checked += 1
            if not (path.parent / match.group(1)).resolve().exists():
                errors.append(f"{rel}: missing module -> {match.group(1)}")

        for match in IMPORT_RE.finditer(src):
            target = (path.parent / match.group(3)).resolve()
            if not target.exists():
                continue  # already reported above
            available = export_index.get(target, set())
            if match.group(1):
                for part in match.group(1).split(","):
                    name = part.strip().split(" as ")[0].strip()
                    if name and name not in available:
                        errors.append(
                            f"{rel}: '{name}' is not exported by {match.group(3)}"
                        )
            elif match.group(2) and "default" not in available:
                errors.append(f"{rel}: {match.group(3)} has no default export")

    return checked


def _check_moved_apps(errors: list[str]) -> None:
    for path in _modules():
        rel = path.relative_to(ROOT)
        for match in ANY_IMPORT_RE.finditer(path.read_text(encoding="utf-8")):
            stem = Path(match.group(1)).stem
            if stem in MOVED_TO_PACKAGES:
                errors.append(
                    f"{rel}: imports '{match.group(1)}', which moved to a package — "
                    "call the provider hook (window.__ethosEditorOpen etc.) instead"
                )


def _check_html_and_sw(errors: list[str]) -> None:
    for doc, base in ((INDEX, INDEX.parent), (SW, SW.parent)):
        if not doc.exists():
            continue
        for match in JS_REF_RE.finditer(doc.read_text(encoding="utf-8")):
            ref = match.group(1)
            if ref.startswith(("http://", "https://", "//")):
                continue
            if not (base / ref).resolve().exists():
                errors.append(f"{doc.relative_to(ROOT)}: references missing '{ref}'")


def _check_sw_not_precaching_removed(errors: list[str]) -> None:
    if not SW.exists():
        return
    for match in JS_REF_RE.finditer(SW.read_text(encoding="utf-8")):
        if Path(match.group(1)).stem in MOVED_TO_PACKAGES:
            errors.append(f"sw.js precaches '{match.group(1)}', removed from core")


def main() -> int:
    errors: list[str] = []
    checked = _check_imports(errors)
    _check_moved_apps(errors)
    _check_html_and_sw(errors)
    _check_sw_not_precaching_removed(errors)

    if errors:
        print("frontend module graph is BROKEN:")
        for err in errors:
            print(f"  - {err}")
        print("\nA broken graph makes app.js abort, so the desktop renders empty.")
        return 1

    print(f"frontend module graph OK ({checked} relative imports checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
