#!/usr/bin/env python3
"""Static guards — one unified log system for EthOS and its packages.

The rule (AGENTS.md → Logging, `.github/instructions/logging.instructions.md`):
apps and packages never render their own log viewer, keep a second store or open
`/api/logs` / `/ws/logs` directly. They use `core/logger.js`, `core/log-stream.js`
and (packages) `ctx.log`; the backend writes through `services/log_service.py`.

No server needed.

Run:  .venv/bin/python tests/test_logging_ui.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"

CORE_LOGGER = "frontend/static/core/logger.js"
CORE_STREAM = "frontend/static/core/log-stream.js"
LOGS_APP = "frontend/static/apps/logs.js"
APP_JS = "frontend/static/app.js"
ERROR_REPORTER = "frontend/static/core/error-reporter.js"
WINDOW_MANAGER = "frontend/static/core/window-manager.js"
INSTRUCTIONS = ".github/instructions/logging.instructions.md"
AGENTS = "AGENTS.md"
DOC = "docs/logs.md"
SKILL = ".github/skills/logging-ui/SKILL.md"
BACKEND_STORE = "backend/services/log_service.py"
BACKEND_API = "backend/routers/logs.py"
BACKEND_WS = "backend/routers/logs_ws.py"
PACKAGES_AGENTS = Path(__file__).resolve().parent.parent.parent / "ethos-packages" / "AGENTS.md"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    logger = _read(CORE_LOGGER)
    stream = _read(CORE_STREAM)
    logs_app = _read(LOGS_APP)
    app_js = _read(APP_JS)
    reporter = _read(ERROR_REPORTER)
    window_manager = _read(WINDOW_MANAGER)
    index_html = _read("frontend/index.html")

    # ── 1. Public API ──────────────────────────────────────────────────────
    for fn in ("log", "logDebug", "logInfo", "logWarn", "logError", "setLogContext", "flushLogs"):
        check(f"core/logger.js exports {fn}",
              re.search(rf"export (async )?(function|const) {fn}\b", logger) is not None)
    for fn in ("fetchLogs", "streamLogs", "fetchLogFiles", "fetchJournal", "clearLogs"):
        check(f"core/log-stream.js exports {fn}",
              re.search(rf"export (async )?(function|const) {fn}\b", stream) is not None)

    # ── 2. One client for the API + socket ─────────────────────────────────
    offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"['\"`]/api/logs", text) or re.search(r"['\"`]/ws/logs", text):
            if path.name not in ("log-stream.js", "logger.js"):
                offenders.append(str(path.relative_to(ROOT)))
    check("only the logger and log-stream know the Logs API and socket", not offenders, ", ".join(offenders))
    check("the reporter only uses the ingest endpoint", "/api/logs/ingest" in logger
          and "/api/logs?" not in logger and "/ws/logs" not in logger)
    check("log-stream uses the shared socket helper",
          re.search(r"connectWs\(", stream) is not None and "'./ws.js'" in stream)
    check("no raw WebSocket consumer in the viewer", "new WebSocket" not in logs_app)

    # ── 3. One viewer, no bespoke panels ───────────────────────────────────
    panel_offenders = []
    for path in sorted(STATIC.rglob("*.js")):
        if path.name == "logs.js":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"id=[\"']logOutput[\"']|_containerLogTarget|log-history", text):
            panel_offenders.append(str(path.relative_to(ROOT)))
    check("no app builds its own log panel or handoff global", not panel_offenders,
          ", ".join(panel_offenders))
    check("logs.js is the only renderer", logs_app.count("export async function renderLogsPage") == 1
          and sum(1 for p in STATIC.rglob("*.js") if "export async function renderLogsPage" in p.read_text(encoding="utf-8")) == 1)
    check("logs.js exposes the deep-link entry point",
          re.search(r"export function openLogs\(", logs_app) is not None)
    check("viewer renders entries with textContent",
          "message.textContent = entry.message" in logs_app and "pre.textContent = " in logs_app)
    check("viewer caps the rendered rows", "MAX_ROWS = 2000" in logs_app and "_trimRows" in logs_app)
    for anchor in ("logs-rail", "logs-list", "logs-search", "logs-pause", "logs-export", "logs-clear"):
        check(f"viewer exposes data-testid={anchor}", f'data-testid="{anchor}"' in logs_app)

    # ── 4. Browser reporter funnels through the logger ─────────────────────
    check("error reporter imports the shared logger", "from './logger.js'" in reporter)
    check("error reporter has no endpoint of its own", "/api/logs" not in reporter and "fetch(" not in reporter)
    check("window manager attributes events to the focused app",
          "setLogContext" in window_manager and "_syncLogContext" in window_manager)
    check("reporter is loaded before app.js",
          index_html.index("core/error-reporter.js") < index_html.index('src="/static/app.js'))

    # ── 5. Package contract ────────────────────────────────────────────────
    check("native package ctx exposes log()", re.search(r"^\s*log: \(level, message", app_js, re.M) is not None)
    check("native package ctx exposes logger{}", "logger: {" in app_js and "warn: (message" in app_js)
    check("package events send package_id", "packageId: pkg.id" in app_js)

    # ── 6. Backend invariants ──────────────────────────────────────────────
    check("unified store module exists", (ROOT / BACKEND_STORE).is_file())
    store_src = _read(BACKEND_STORE)
    check("store rotates the file", "_rotate_locked" in store_src and "events.{index - 1}.jsonl" in store_src)
    check("store redacts credentials", "def redact(" in store_src and "redacted" in store_src)
    check("store dedupes repeating events", "_dedupe_window" in store_src and '"count"' in store_src)
    main_src = _read("backend/main.py")
    check("main.py initialises the store under DATA_DIR/logs",
          'settings.DATA_DIR / "logs"' in main_src and "backend.services.log_service" in main_src)
    api_src = _read(BACKEND_API)
    check("ingest verifies package_id", "packages_store.get_package" in api_src
          and 'app = f"pkg:{event.package_id}"' in api_src)
    check("ingest is not admin-gated", 'router.post("/ingest")' in api_src)
    check("clear is admin-gated", 'router.delete("", dependencies=_ADMIN)' in api_src)
    ws_src = _read(BACKEND_WS)
    check("WS closes with 4001/4003", "CLOSE_UNAUTHORISED = 4001" in ws_src
          and "CLOSE_FORBIDDEN = 4003" in ws_src)
    check("WS frames are JSON", "json.dumps" in ws_src and "send_text(line" not in ws_src
          and "send_text(f\"" not in ws_src)

    # ── 7. Rules + docs so future agents find the system ───────────────────
    check("docs/logs.md exists", (ROOT / DOC).is_file())
    check("instruction file exists", (ROOT / INSTRUCTIONS).is_file())
    if (ROOT / INSTRUCTIONS).is_file():
        instructions = _read(INSTRUCTIONS)
        check("instructions cover backend and frontend",
              "backend/**/*.py" in instructions and "frontend/static/**/*.js" in instructions)
        check("instructions point at the canonical modules",
              "core/logger.js" in instructions and "ctx.log" in instructions)
    check("AGENTS.md links the spec and the skill", "docs/logs.md" in _read(AGENTS)
          and "logging-ui" in _read(AGENTS))
    check("logging-ui skill exists", (ROOT / SKILL).is_file())
    check("spec documents the package contract", "ctx.log" in _read(DOC) and "/ws/logs" in _read(DOC))

    if PACKAGES_AGENTS.exists():
        packages = PACKAGES_AGENTS.read_text(encoding="utf-8")
        check("packages AGENTS.md forbids package-local log UI",
              "## Logging (mandatory)" in packages and "ctx.log" in packages
              and "review blocker" in packages)

    failed = [item for item in _results if not item[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
