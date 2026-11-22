"""Task-list persistence for Downloads.

Extracted from `backend/routers/downloads.py` (TODO.md P2-01). The router keeps
the in-memory `_tasks` list (workers and endpoints mutate it directly); these
helpers only serialize/deserialize it through a `json_store.Store`.
"""

import logging
import threading

logger = logging.getLogger("ethos-nas.services.download_tasks")

# Terminal (completed/error/canceled) records kept in the store and on load.
MAX_TASK_HISTORY = 500
TERMINAL_STATUSES = ("completed", "error", "canceled")
IN_FLIGHT_STATUSES = ("downloading", "waiting", "debrid_downloading", "debrid_selecting")
_SECRET_FIELDS = ("_events", "_debrid_key")
# Per-file transient values: multihoster links may carry a session Cookie in
# `_link_url_headers`; it must never land in downloads_tasks.json.
_FILE_SECRET_FIELDS = ("_link_url_headers",)


def write(tasks: list, store) -> None:
    """Persist `tasks`; caller must hold the save lock.

    Provider credentials are deliberately not persisted: a task carries
    `_debrid_key` in memory only and the pump rehydrates it from the config at
    start time, so a rotated key never lingers in a second store.
    """
    try:
        serializable = []
        terminal_seen = 0
        for t in reversed(tasks):
            # Keep every in-flight task and the newest MAX_TASK_HISTORY terminal
            # records; the file was otherwise unbounded.
            if t.get("status") in TERMINAL_STATUSES:
                terminal_seen += 1
                if terminal_seen > MAX_TASK_HISTORY:
                    continue
            item = {k: v for k, v in t.items() if k not in _SECRET_FIELDS}
            files = item.get("_files")
            if isinstance(files, list):
                item["_files"] = [
                    {k: v for k, v in f.items() if k not in _FILE_SECRET_FIELDS}
                    for f in files if isinstance(f, dict)
                ]
            serializable.append(item)
        serializable.reverse()
        store.save(serializable)
    except Exception as exc:
        logger.warning("Download tasks persist failed: %s", exc)


def save(tasks: list, lock, store) -> None:
    with lock:
        write(tasks, store)


def load(store) -> list:
    """Load the task list, re-queueing interrupted work and capping history."""
    try:
        # Through json_store: locked, atomic, corruption-quarantining — the raw
        # open()+json.load bypassed all of that and could not be capped.
        data = store.load()
        items = data if isinstance(data, list) else (data or {}).get("tasks", [])
        clean = []
        for t in items:
            if not isinstance(t, dict) or not t.get("id") or not t.get("url"):
                continue
            # A restart interrupted in-flight work: re-queue partial downloads.
            if t.get("status") in IN_FLIGHT_STATUSES:
                t["status"] = "waiting"
                t["speed"] = 0
            t["speed"] = int(t.get("speed") or 0)
            clean.append(t)
        clean.sort(key=lambda x: x.get("created_at", 0))
        # Cap the terminal history so an old task list cannot grow forever.
        terminal = [t for t in clean if t.get("status") in TERMINAL_STATUSES]
        if len(terminal) > MAX_TASK_HISTORY:
            keep = {t["id"] for t in terminal[-MAX_TASK_HISTORY:]}
            clean = [t for t in clean
                     if t.get("status") not in TERMINAL_STATUSES or t["id"] in keep]
        return clean
    except Exception as exc:
        logger.warning("Download tasks unreadable (%s) — starting empty", exc)
        return []
