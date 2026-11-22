"""Static guards — File Manager upload status (shared progress center + chip).

Uploads no longer own a floating panel: every row is a task in
`core/progress.js`, together with copy/move/delete/trash, package jobs and
Docker installs. These checks pin the delegation (start/update/finish), the
retry/error plumbing, the job→task cleanup and the status-bar wiring.

Run:  .venv/bin/python tests/test_upload_status_ui.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENGINE = "frontend/static/core/upload-engine.js"
PROGRESS = "frontend/static/core/progress.js"
FILES_JS = "frontend/static/apps/files.js"
INDEX_HTML = "frontend/index.html"
# Panel styles live in the shared stylesheet since the CSS extraction.
STYLES = "frontend/static/css/progress.css"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Best-effort body extraction for a top-level function (brace counting)."""
    match = re.search(rf"function {name}\s*\(", src)
    if not match:
        return ""
    start = src.index("{", match.end())
    depth = 0
    for idx in range(start, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    return src[start:]


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    engine = _read(ENGINE)
    progress = _read(PROGRESS)
    files_js = _read(FILES_JS)
    index_html = _read(INDEX_HTML)
    styles = _read(STYLES)

    # ── 1. Delegation to the shared task center ────────────────────────────
    check("engine imports the shared task helpers",
          re.search(r"import \{[^}]*startTask[^}]*\} from '\./progress\.js'", engine) is not None)
    check("engine no longer builds its own panel",
          "uploadProgressPanel" not in engine and "upload-progress-panel" not in engine)
    check("startUpload creates a row for every file",
          "_beginTask(job)" in _function_body(engine, "startUpload"))
    begin = _function_body(engine, "_beginTask")
    check("each upload is a task with a stable test id prefix",
          "'uploads'" in begin and "testIdPrefix: 'upload'" in begin)
    check("upload rows use a text title (no HTML interpolation)",
          "title: job.filename" in begin)
    check("upload retry is wired to the row", "retry: () => retryUpload(job.jobId)" in begin)
    check("clearing a row drops the job (status chip stays honest)",
          "onDismiss: () => { _jobs.delete(job.jobId); }" in begin)

    # ── 2. State machine drives update/finish ──────────────────────────────
    for status in ("'queued'", "'uploading'", "'finalizing'", "'complete'", "'failed'"):
        check(f"job status {status} is tracked", status in engine)
    check("running states update the task", "job.task.update({" in _function_body(engine, "_syncTask"))
    finish = _function_body(engine, "_finishTask")
    check("success finishes the row as done", "finish('done'" in finish)
    check("failure finishes the row as failed with the reason", "finish('failed'" in finish)
    check("retry revives the existing row",
          "status: 'running'" in _function_body(engine, "retryUpload"))
    check("finished rows are not auto-dismissed",
          "setTimeout(() => removeUploadProgress" not in engine)

    # ── 3. Errors carry the server detail ──────────────────────────────────
    check("readErrorDetail imported from core/api.js",
          re.search(r"import \{[^}]*readErrorDetail[^}]*\} from '\./api\.js'", engine) is not None)
    check("all three chunk endpoints surface their detail",
          engine.count("readErrorDetail(resp") >= 3, f"count={engine.count('readErrorDetail(resp')}")

    # ── 4. Events consumed by the File Manager ─────────────────────────────
    check("engine dispatches ethos:uploads-changed",
          "new CustomEvent('ethos:uploads-changed'" in engine)
    check("engine still dispatches ethos:uploads-settled",
          "new CustomEvent('ethos:uploads-settled'" in engine)
    check("status changes bypass the progress throttle", "immediate" in _function_body(engine, "_emitUploadsChanged"))
    for fn in ("showUploadPanel", "getUploadStatus", "clearFinishedUploads"):
        check(f"engine exports {fn}", re.search(rf"export function {fn}\b", engine) is not None)
    check("clearFinishedUploads uses the shared clear",
          "clearFinishedTasks('uploads')" in _function_body(engine, "clearFinishedUploads"))
    check("showUploadPanel opens the shared panel",
          "showTaskPanel()" in _function_body(engine, "showUploadPanel"))

    # ── 5. Shared component renders the row safely + accessibly ────────────
    apply_row = _function_body(progress, "_applyRow")
    check("row title set via textContent", "nameEl.textContent = task.title;" in apply_row)
    check("progress bar exposes aria-valuenow", "setAttribute('aria-valuenow'" in apply_row)
    check("indeterminate state is supported", "is-indeterminate" in apply_row)
    check("retry button only shows for failures",
          "task.status === 'failed'" in apply_row and "task-progress-retry" in progress)
    panel = _function_body(progress, "_ensurePanel")
    check("panel body is a live region",
          "setAttribute('role', 'status')" in panel and "setAttribute('aria-live', 'polite')" in panel)
    check("panel clears finished rows on close",
          "clearFinishedTasks(null, { globalOnly: true })" in progress)

    # ── 6. File Manager status-bar chip ────────────────────────────────────
    check("status bar ships the upload chip slot",
          'data-testid="fm-statusbar-upload"' in files_js)
    check("files.js imports the chip helpers",
          re.search(r"import \{[^}]*getUploadStatus[^}]*showUploadPanel[^}]*\} from '\.\./core/upload-engine\.js'", files_js) is not None)
    check("chip click re-opens the progress panel",
          "addEventListener('click', () => showUploadPanel())" in files_js)
    check("chip renderer exists", re.search(r"function _fmRenderUploadChip\b", files_js) is not None)
    check("files.js listens for ethos:uploads-changed",
          "window.addEventListener('ethos:uploads-changed'" in files_js)

    # ── 7. Styles + placeholder ────────────────────────────────────────────
    for cls in (".fm-status-upload", ".task-progress-retry", ".task-progress-head", ".task-progress-summary"):
        check(f"CSS defines {cls}", cls in styles or cls in index_html)
    check("index.html ships the shared panel placeholder", 'id="taskProgressPanel"' in index_html)
    check("no upload-specific panel CSS remains",
          ".upload-progress-" not in index_html and ".upload-progress-" not in styles)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
