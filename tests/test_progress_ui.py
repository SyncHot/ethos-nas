"""Static guards — one progress component for every app.

The rule (AGENTS.md → Progress, `.github/instructions/frontend.instructions.md`):
long-running actions report through `core/progress.js`; apps never hand-roll a
progress card, floating panel or `role="progressbar"` markup. Finished rows
persist until the user clears/closes the panel, failures carry a reason and can
offer Retry.

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
INSTRUCTIONS = ".github/instructions/frontend.instructions.md"
AGENTS = "AGENTS.md"
DOC = "docs/progress-ui.md"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    progress = _read(PROGRESS)
    index_html = _read(INDEX_HTML)
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
    for rel, prefix, host in (
        ("frontend/static/apps/files.js", "fm-op", None),
        ("frontend/static/core/upload-engine.js", "upload", None),
        ("frontend/static/apps/packages.js", "pkg-job", "host: banner"),
        ("frontend/static/apps/docker.js", "docker-engine", "host: box"),
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
        check(f"CSS defines {cls}", cls in index_html)
    check("index.html ships #taskProgressPanel", 'id="taskProgressPanel"' in index_html)
    check("old upload panel CSS is gone", ".upload-progress-" not in index_html)

    # ── 6. Rules are documented (the user-facing part of this change) ──────
    check("frontend instructions document the progress rule",
          "## Progress UI" in instructions and "core/progress.js" in instructions)
    check("AGENTS.md hard rules mention core/progress.js",
          "**Progress.**" in agents and "core/progress.js" in agents)
    check("docs/progress-ui.md exists", (ROOT / DOC).is_file())
    doc = _read(DOC) if (ROOT / DOC).is_file() else ""
    check("doc lists the enforced rules", "tests/test_progress_ui.py" in doc)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
