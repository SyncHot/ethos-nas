"""Static guards — File Manager delete/trash UX and backend delete primitives.

Pins the repair of "deleting files/folders works badly":

  * deletion resolves exact paths (not names) — search results cannot hit the
    wrong file; the context menu passes the clicked path
  * one op-tracked batch request per action (no N parallel trash calls), with a
    confirmation dialog, read-only guard and double-submit guard
  * failures show the server reason (permission denied / not found / partial)
  * the listing/search/trash view refreshes without leaving the current view
  * symlinks (including dangling ones) and partially removable trees are
    handled and reported by the backend

No server needed.

Run:  .venv/bin/python tests/test_delete_ux.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES_JS = "frontend/static/apps/files.js"
DESKTOP_TRASH_JS = "frontend/static/core/desktop-trash.js"
FILES_SERVICE = "backend/services/files_service.py"
TRASH_SERVICE = "backend/services/trash_service.py"
FILE_OPS = "backend/routers/file_ops.py"
FILES_ROUTER = "backend/routers/files.py"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\s*\(", src)
    if not match:
        return ""
    # Skip the parameter list first: default values may contain object literals
    # (`{ deferMs = 0 } = {}`), which a naive first-brace search would mistake
    # for the function body.
    idx = match.end() - 1
    depth = 0
    while idx < len(src):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                break
        idx += 1
    start = src.index("{", idx)
    depth = 0
    for idx in range(start, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    return src[start:]


def _py_function_body(src: str, name: str) -> str:
    """Body of a Python ``def``/``async def`` (brace counting is enough here)."""
    match = re.search(rf"^(?:async )?def {name}\(.*?\n(?=\S)", src, re.M | re.S)
    if not match:
        return ""
    return match.group(0)


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    files_js = _read(FILES_JS)
    desktop_js = _read(DESKTOP_TRASH_JS)
    service = _read(FILES_SERVICE)
    trash = _read(TRASH_SERVICE)
    ops = _read(FILE_OPS)
    router = _read(FILES_ROUTER)

    # ── 1. Path-based resolution (search duplicates) ────────────────────────
    resolver = _function_body(files_js, "_fmResolveSelectedPaths")
    check("_fmResolveSelectedPaths exists", bool(resolver))
    check("resolver reads the row's data-path", "data-path" in resolver and "getAttribute('data-path')" in resolver)
    check("resolver skips the '..' row", "name === '..'" in resolver)
    for fn in ("fmDeleteSelected", "fmHardDeleteSelected"):
        check(f"{fn} uses the path resolver", "_fmResolveSelectedPaths(state, root)" in _function_body(files_js, fn))
    check("context menu keeps the clicked path",
          "const exactPath" in files_js and "fmDeleteSelected(exactPath)" in files_js
          and "fmHardDeleteSelected(exactPath)" in files_js)
    check("copy/cut/zip use the path resolver",
          files_js.count("_fmResolveSelectedPaths(state, root)") >= 4)

    # ── 2. One op-tracked batch request, confirmation, guards ───────────────
    trash_fn = _function_body(files_js, "fmTrashPaths")
    check("trash uses the batch op endpoint", "'/api/files/trash/batch'" in trash_fn)
    check("trash tracks the op", "_trackFileOp(opId, list.length, 'trash'" in trash_fn)
    check("trash confirms before moving", "showModalConfirm('Move to Trash'" in trash_fn)
    check("trash honours read-only folders", "_canWrite === false" in trash_fn)
    check("trash has a double-submit guard", "_deleteBusy" in trash_fn)
    check("no per-item trash POST remains", "apiFetch('/api/files/trash', {" not in files_js)

    hard_fn = _function_body(files_js, "fmHardDeleteSelected")
    check("hard delete confirms", "showModalConfirm('Permanently Delete'" in hard_fn)
    check("hard delete tracks the op", "'hard_delete'" in hard_fn)
    check("hard delete guards read-only folders", "_canWrite === false" in hard_fn)
    check("hard delete guards double submit", "_deleteBusy" in hard_fn)

    # ── 3. Failure reasons instead of a bare count ──────────────────────────
    check("delete reason mapper exists", bool(_function_body(files_js, "_fmDeleteReason")))
    for token in ("permission_denied", "not_found", "error: "):
        check(f"reason mapper handles {token!r}", token in _function_body(files_js, "_fmDeleteReason"))
    check("op result summary reports reasons", "_fmReportOpResult" in files_js)
    check("readErrorDetail is used for HTTP failures", files_js.count("readErrorDetail(resp") >= 4)

    # ── 4. Refresh without leaving the current view ─────────────────────────
    refresh = _function_body(files_js, "_fmRefreshAfterDelete")
    check("refresh keeps the trash view", "fmOpenTrash()" in refresh)
    check("refresh keeps the search view", "fmSearch()" in refresh)
    check("refresh uses a silent refetch for listings", "_silentRefetch(root)" in refresh)
    check("delete actions no longer call fmLoad()",
          "fmLoad(" not in trash_fn and "fmLoad(" not in hard_fn)

    # ── 4b. Progress goes through the shared task center ────────────────
    track = _function_body(files_js, "_trackFileOp")
    check("file ops use the shared task center", "startTask({" in track and "testIdPrefix: 'fm-op'" in track)
    check("progress rows can be deferred", "deferMs" in track and "setTimeout(ensureTask, deferMs)" in track)
    check("op verbs cover trash", "trash: { running: 'Moving to Trash'" in files_js)
    check("op verbs cover purge", "purge: { running: 'Deleting from Trash'" in files_js)
    check("no bespoke progress card remains", "_renderOpProgressCard" not in files_js)
    check("task shows the failure reason", "_fmDeleteReason(firstFailed)" in files_js)

    # ── 5. Trash view: bulk select + op-backed purge/empty ──────────────────
    check("trash rows have selection checkboxes", 'data-trash-select=' in files_js)
    check("trash toolbar has a bulk delete button", 'data-testid="fm-trash-delete-selected"' in files_js)
    check("trash selection is wired without globals", "_fmWireTrashSelection(contentEl)" in files_js)
    check("bulk purge uses the items op", "'/api/files/trash/items'" in files_js)
    check("empty trash uses the all op", "'/api/files/trash/all'" in files_js)
    check("trash purge tracks a 'purge' op", "_trackFileOp(opId, items.length, 'purge'" in files_js)
    check("empty trash tracks a 'purge' op", "_trackFileOp(opId, total || 1, 'purge'" in files_js)
    check("trash view is refreshed after purging", "fmOpenTrash();" in _function_body(files_js, "_fmPurgeTrashItems"))

    # ── 7. Desktop Trash drop delegates ────────────────────────────────────
    check("desktop drop delegates to fmTrashPaths", "window.fmTrashPaths" in desktop_js)
    check("desktop drop fallback uses the batch endpoint", "/api/files/trash/batch" in desktop_js)
    check("desktop drop no longer fires N requests",
          "Promise.all(items.map(it => apiFetch('/api/files/trash'" not in desktop_js)
    check("files.js exposes fmTrashPaths for the desktop", "window.fmTrashPaths = fmTrashPaths" in files_js)

    # ── 8. File events: search rows + no duplicate delete toast ────────────
    check("optimistic removal covers search rows",
          "'.fm-file-item, .fm-list-row, .fm-search-result'" in files_js)
    check("delete events do not double-toast",
          "['delete_soft', 'delete_hard', 'restore'].includes(ev.action)" in files_js)

    # ── 9. Backend primitives ──────────────────────────────────────────────
    worker = _py_function_body(service, "_delete_worker")
    check("hard delete uses lexists", "os.path.lexists(p)" in worker)
    check("hard delete unlinks symlinks instead of following them", "os.path.islink(p)" in worker)
    check("hard delete collects partial-tree failures", "_rmtree_collect(p)" in worker)
    check("partial failures carry a reason", '"reason": problems[0]' in worker)
    check("rmtree uses an onexc handler", "shutil.rmtree(path, onexc=_onexc)" in service)

    check("trash service exposes reserve_dest", bool(re.search(r"def reserve_dest\b", trash)))
    check("trash service exposes finalize_move", bool(re.search(r"def finalize_move\b", trash)))
    check("move_to_trash cleans partial copies", "dest.unlink(missing_ok=True)" in trash)
    check("trash symlinks are not reported as directories", "not os.path.islink(src)" in trash)
    check("trash authz is shared", bool(re.search(r"def actor_from_claims\b", trash))
          and bool(re.search(r"def target_owner\b", trash)))

    check("trash worker is op-backed", bool(re.search(r"def _trash_worker\b", service)))
    check("purge worker is op-backed", bool(re.search(r"def _purge_worker\b", service)))
    check("trash op uses a collision-free reservation", "trash_service.reserve_dest(p, op[\"owner\"])" in service)

    for route in ('@router.post("/trash/batch")', '@router.delete("/trash/items")', '@router.delete("/trash/all")'):
        check(f"route {route} exists", route in ops)
    check("move-to-trash accepts dangling symlinks", "os.path.lexists(abs_path)" in router)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
