"""Static guards — desktop Trash shortcut wiring.

The Trash icon is only usable if the id is registered in ALL layout layers:
frontend/static/core/registry.js (APP_REGISTRY), desktop-layout.js
(DEFAULT_ORDER) and backend/routers/packages.py (APP_REGISTRY_KEYS). The server
silently drops ids it does not know, so a missing backend entry makes the icon
disappear after a reload. This test needs no server.

Run:  .venv/bin/python tests/test_desktop_trash.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _list_block(src: str, name: str) -> str:
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", src, re.S)
    return m.group(1) if m else ""


def _ids(block: str) -> set[str]:
    return set(re.findall(r'"([a-z0-9-]+)"', block))


def _mime(src: str) -> str:
    m = re.search(r"FM_DND_MIME\s*=\s*'([^']+)'", src)
    return m.group(1) if m else ""


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    registry = _read("frontend/static/core/registry.js")
    layout = _read("frontend/static/core/desktop-layout.js")
    packages = _read("backend/routers/packages.py")
    app_js = _read("frontend/static/app.js")
    files_js = _read("frontend/static/apps/files.js")
    icons_js = _read("frontend/static/core/app-icons.js")
    index_html = _read("frontend/index.html")
    trash_path = ROOT / "frontend/static/core/desktop-trash.js"

    # ── 1. The id is registered everywhere the layout is validated ──────────
    backend_block = _list_block(packages, "APP_REGISTRY_KEYS")
    default_block = _list_block(layout, "DEFAULT_ORDER")
    check("backend APP_REGISTRY_KEYS contains trash", "trash" in _ids(backend_block))
    check("desktop-layout DEFAULT_ORDER contains trash", "trash" in _ids(default_block))
    check(
        "backend keys and DEFAULT_ORDER list the same ids",
        _ids(backend_block) == _ids(default_block),
        f"only-backend={sorted(_ids(backend_block) - _ids(default_block))} "
        f"only-frontend={sorted(_ids(default_block) - _ids(backend_block))}",
    )
    check("APP_REGISTRY has a trash entry", bool(re.search(r"^\s*trash:\s*\{", registry, re.M)))
    check("trash has an SVG glyph", bool(re.search(r"^\s*trash:\s*'<", icons_js, re.M)))

    # ── 2. Desktop icon: badge + bottom-right default cell ─────────────────
    check("icon markup carries the badge element", "desktopTrashBadge" in registry)
    check("badge CSS exists", ".desktop-trash-badge" in index_html)
    check("drop highlight CSS exists", ".trash-drop-target" in index_html)
    check("placement reserves the bottom-right cell for trash", "id !== 'trash'" in registry)

    # ── 3. Trash behaviour module + wiring ────────────────────────────────
    check("core/desktop-trash.js exists", trash_path.is_file())
    trash_js = trash_path.read_text(encoding="utf-8") if trash_path.is_file() else ""
    for fn in ("openTrashFromDesktop", "emptyTrashFromDesktop", "refreshTrashBadge", "initDesktopTrash"):
        check(f"desktop-trash exports {fn}", bool(re.search(rf"export (async )?function {fn}\b", trash_js)))
    check("registry reuses the trash actions", all(
        name in registry for name in ("openTrashFromDesktop", "emptyTrashFromDesktop", "refreshTrashBadge")
    ))
    check("context menu offers Empty Trash", "icon-ctx-trash-empty" in registry)
    check("app.js intercepts openApp('trash')", "appId === 'trash'" in app_js)
    check("app.js seeds the icon once", "ensureTrashPinned" in app_js)
    check("app.js initialises the module", "initDesktopTrash()" in app_js)

    # ── 4. Drag & drop contract + the app-pin false positive ──────────────
    check(
        "Trash drop target uses the Files MIME",
        _mime(trash_js) != "" and _mime(trash_js) == _mime(files_js),
        f"trash={_mime(trash_js)!r} files={_mime(files_js)!r}",
    )
    check("desktop drop zone ignores file drags", "application/x-ethos-fm-items" in registry)

    # ── 5. Badge refresh trigger from Files events ───────────────────────
    check("file events notify the badge", "ethos:trash-changed" in files_js)
    check("badge listens for the event", "ethos:trash-changed" in trash_js)

    # ── 6. Per-user recycle bin + admin all-users view ────────────────────
    service_js = (ROOT / "backend/services/trash_service.py")
    check("per-user trash service exists", service_js.is_file())
    check("admin all-users toggle wired", "fmTrashToggleScope" in files_js
          and "fm-trash-scope-toggle" in files_js)
    check("admin all-users request uses all=1", "'/api/files/trash?all=1'" in files_js)
    check("admin row actions pass owner=", "?owner=" in files_js)
    check("trash scoped per window", "_trashScope" in files_js)
    check("trash design doc exists", (ROOT / "docs/trash.md").is_file())

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All desktop Trash guards passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
