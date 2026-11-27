"""Static guards — one progress component for every app.

The rule (AGENTS.md → Progress, `.github/instructions/frontend.instructions.md`):
long-running actions report through `core/progress.js`; apps never hand-roll a
progress card, floating panel or `role="progressbar"` markup. Finished rows
persist until the user clears/closes the panel, failures carry a reason and can
offer Retry. The panel also publishes its own footprint (`--task-panel-h`,
`data-task-panel-state`) so no app surface hides under it.

No server needed.

Run:  .venv/bin/python tests/test_progress_ui.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend/static"

PROGRESS = "frontend/static/core/progress.js"
INDEX_HTML = "frontend/index.html"
# Panel styles live in the shared stylesheet since the CSS extraction; index.html
# only ships the #taskProgressPanel placeholder and the <link>.
STYLES = "frontend/static/css/progress.css"
INSTRUCTIONS = ".github/instructions/frontend.instructions.md"
AGENTS = "AGENTS.md"
DOC = "docs/progress-ui.md"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Best-effort body extraction for a top-level function (brace counting).

    Skips the parameter list first: `function startTask(opts = {}) {` would
    otherwise "close" on the default object literal and return `{}`.
    """
    match = re.search(rf"function {name}\s*\(", src)
    if not match:
        return ""
    idx = match.end() - 1
    depth = 0
    while idx < len(src):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                idx += 1
                break
        idx += 1
    start = src.find("{", idx)
    if start < 0:
        return ""
    depth = 0
    for pos in range(start, len(src)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    return src[start:]


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    progress = _read(PROGRESS)
    index_html = _read(INDEX_HTML)
    styles = _read(STYLES)
    instructions = _read(INSTRUCTIONS)
    agents = _read(AGENTS)

    # ── 1. Public API ──────────────────────────────────────────────────────
    for fn in ("startTask", "updateTask", "finishTask", "clearFinishedTasks",
               "showTaskPanel", "getTaskStatus", "getAppTaskStatus"):
        check(f"progress.js exports {fn}", re.search(rf"export function {fn}\b", progress) is not None)
    check("startTask returns an update/finish handle",
          "update(patch = {})" in progress and "finish(status =" in progress)

    # ── 2. Behaviour requirements ──────────────────────────────────────────
    check("indeterminate state when percent is null", "is-indeterminate" in progress)
    check("finished rows are bounded, not auto-erased", "MAX_FINISHED_TASKS" in progress)
    check("panel supports clear + close", "task-progress-clear" in progress and "task-progress-close" in progress)
    check("retry callback supported", "typeof task.retry === 'function'" in progress)
    check("status changes are emitted immediately", "if (immediate)" in progress)
    check("event is ethos:tasks-changed", "ethos:tasks-changed" in progress)
    check("inline host renders the same row", "_renderInline" in progress and "task-progress-item-inline" in progress)
    check("row title is rendered as text", "nameEl.textContent = task.title;" in progress)
    check("progressbar exposes aria-valuenow", "setAttribute('aria-valuenow'" in progress)
    check("panel body is a live region", "setAttribute('aria-live', 'polite')" in progress)

    # ── 3. Single implementation: nobody else builds progress bars ─────────
    offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        if path.name == "progress.js":
            continue
        text = path.read_text(encoding="utf-8")
        if 'role="progressbar"' in text or "role='progressbar'" in text:
            offenders.append(str(path.relative_to(ROOT)))
    check("only core/progress.js renders role=progressbar", not offenders, ", ".join(offenders))

    legacy = []
    for path in sorted(STATIC.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for marker in ("upload-progress-", "fm-op-progress-card", "_renderOpProgressCard"):
            if marker in text:
                legacy.append(f"{path.relative_to(ROOT)}: {marker}")
    check("no bespoke panel/card markup remains", not legacy, "; ".join(legacy))

    # ── 4. Every long-running action uses the component ────────────────────
    # The Docker Manager UI is an `ethos-docker` package now, so its progress
    # wiring is verified by the package's own checks, not from this checkout.
    for rel, prefix, host in (
        ("frontend/static/apps/files.js", "fm-op", None),
        ("frontend/static/core/upload-engine.js", "upload", None),
        ("frontend/static/apps/packages.js", "pkg-job", "host: banner"),
    ):
        src = _read(rel)
        check(f"{rel} imports startTask", re.search(r"import \{[^}]*startTask[^}]*\} from '\.\.?/core?/?progress\.js'", src) is not None
              or "from '../core/progress.js'" in src or "from './progress.js'" in src)
        check(f"{rel} uses testIdPrefix '{prefix}'", f"testIdPrefix: '{prefix}'" in src)
        if host:
            check(f"{rel} renders inline via {host}", host in src)

    # ── 5. Styles and placeholder ──────────────────────────────────────────
    for cls in (".task-progress-panel", ".task-progress-item", ".task-progress-bar",
                ".task-progress-retry", ".task-progress-summary", "taskIndeterminate"):
        check(f"CSS defines {cls}", cls in styles)
    check("index.html ships #taskProgressPanel", 'id="taskProgressPanel"' in index_html)
    check("index.html links the progress stylesheet", "/static/css/progress.css" in index_html)
    # Panel visibility: × sticks (progress ticks must not resurrect the panel),
    # a brand-new task re-opens it, and an empty queue never shows a blank box.
    check("close button keeps the panel closed",
          "_panelDismissed = true" in _function_body(progress, "_hidePanel")
          and "if (!_panelDismissed)" in _function_body(progress, "_ensurePanel"))
    check("a new task re-opens the panel",
          "_panelDismissed = false" in _function_body(progress, "startTask"))
    check("empty queue does not open a blank panel",
          "if (!_tasks.size)" in _function_body(progress, "showTaskPanel")
          and "_panelDismissed = false" in _function_body(progress, "showTaskPanel"))

    # ── 6. Footprint contract (panel is fixed above every window) ─────────
    # The panel never lowers its z-index: it publishes the space it occupies and
    # the shell reserves it, so the last row of any app stays clickable.
    metrics_body = _function_body(progress, "_syncPanelMetrics")
    check("panel publishes --task-panel-h / --task-panel-w",
          "'--task-panel-h'" in metrics_body and "'--task-panel-w'" in metrics_body
          and "document.documentElement" in metrics_body)
    check("panel publishes data-task-panel-state hidden|idle|active",
          "taskPanelState" in metrics_body and "'hidden'" in metrics_body
          and "'idle'" in metrics_body and "'active'" in metrics_body)
    check("footprint is measured in a rAF and re-measured on resize",
          "requestAnimationFrame" in _function_body(progress, "_schedulePanelMetrics")
          and "addEventListener('resize', _schedulePanelMetrics" in progress)

    collapse_body = _function_body(progress, "_syncCollapsed")
    check("an idle panel collapses (no running task)",
          "_hasRunningTasks()" in collapse_body and "'is-collapsed'" in collapse_body
          and "_panelExpanded" in collapse_body)
    check("a running task never collapses the panel",
          "if (running) _panelExpanded = false;" in collapse_body)
    check("header button toggles the collapse",
          "_togglePanelBody" in _function_body(progress, "_ensurePanel")
          and "_panelExpanded = !_panelExpanded" in _function_body(progress, "_togglePanelBody")
          and "aria-expanded" in collapse_body
          and "task-progress-toggle" in progress)
    check("collapse is announced and wired to the body",
          "aria-controls" in progress and "taskProgressBody" in progress)
    check("CSS collapses the panel body",
          ".task-progress-panel.is-collapsed" in styles
          and ".is-collapsed .task-progress-body { display: none" in styles)
    check("windows reserve the panel footprint",
          re.search(r"\.window-body\s*\{[^}]*var\(--task-panel-h", index_html) is not None)
    check("phone desktop grid reserves the panel footprint",
          re.search(r"\.desktop-icons\s*\{[^}]*var\(--task-panel-h", index_html) is not None)

    # ── 7. Rules are documented (the user-facing part of this change) ──────
    check("frontend instructions document the progress rule",
          "## Progress UI" in instructions and "core/progress.js" in instructions)
    check("AGENTS.md hard rules mention core/progress.js",
          "**Progress.**" in agents and "core/progress.js" in agents)
    check("docs/progress-ui.md exists", (ROOT / DOC).is_file())
    doc = _read(DOC) if (ROOT / DOC).is_file() else ""
    check("doc lists the enforced rules", "tests/test_progress_ui.py" in doc)
    check("doc documents the footprint contract",
          "--task-panel-h" in doc and "data-task-panel-state" in doc)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
