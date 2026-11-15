#!/usr/bin/env python3
"""Unit tests for backend/services/download_tasks.py (no server).

Run:  .venv/bin/python tests/test_download_tasks.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import download_tasks as dt  # noqa: E402

_failures = 0


class FakeStore:
    def __init__(self, data):
        self.data = data
        self.saved = []

    def load(self):
        return self.data

    def save(self, data):
        self.saved.append(data)


class FakeLock:
    def __init__(self):
        self.entries = 0

    def __enter__(self):
        self.entries += 1

    def __exit__(self, *exc):
        return False


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    print("download_tasks")
    check("history cap is 500", dt.MAX_TASK_HISTORY == 500)

    # ── load: validation, requeue, ordering ─────────────────────────────────
    loaded = dt.load(FakeStore([
        {"id": "b", "url": "u", "status": "downloading", "speed": 42, "created_at": 2},
        {"id": "a", "url": "u", "status": "debrid_selecting", "created_at": 1},
        {"id": "bad", "status": "waiting"},
        "nope",
        {"id": "c", "url": "u", "status": "completed", "speed": "7", "created_at": 3},
    ]))
    check("invalid records are dropped", [t["id"] for t in loaded] == ["a", "b", "c"], str(loaded))
    check("in-flight work is re-queued",
          loaded[0]["status"] == "waiting" and loaded[1]["status"] == "waiting")
    check("re-queued speed is reset", loaded[1]["speed"] == 0)
    check("speed is coerced to int", loaded[2]["speed"] == 7)
    check("tasks are ordered by created_at", loaded[0]["created_at"] < loaded[2]["created_at"])

    # ── load: terminal history cap ──────────────────────────────────────────
    big = [{"id": f"c{i}", "url": "u", "status": "completed", "created_at": i} for i in range(600)]
    big.append({"id": "live", "url": "u", "status": "waiting", "created_at": 1000})
    capped = dt.load(FakeStore(big))
    terminal = [t for t in capped if t["status"] == "completed"]
    check("terminal history is capped", len(terminal) == dt.MAX_TASK_HISTORY, str(len(terminal)))
    check("newest terminal records are kept", terminal[-1]["id"] == "c599")
    check("in-flight tasks are never capped", any(t["id"] == "live" for t in capped))

    # ── write: secret stripping + cap ───────────────────────────────────────
    store = FakeStore([])
    tasks = [
        {"id": "live", "status": "waiting", "_debrid_key": "secret", "_events": object()},
        {"id": "done", "status": "completed", "_debrid_key": "secret"},
    ]
    dt.write(tasks, store)
    saved = store.saved[-1]
    check("write persists the list", len(saved) == 2, str(saved))
    check("provider keys are not persisted",
          all("_debrid_key" not in t and "_events" not in t for t in saved))
    check("statuses are preserved", [t["id"] for t in saved] == ["live", "done"])

    many = ([{"id": f"l{i}", "status": "waiting"} for i in range(3)]
            + [{"id": f"c{i}", "status": "error"} for i in range(600)])
    store2 = FakeStore([])
    dt.write(many, store2)
    saved2 = store2.saved[-1]
    check("write keeps all in-flight and 500 terminal",
          len(saved2) == 503, str(len(saved2)))

    # ── save: lock + store ──────────────────────────────────────────────────
    lock = FakeLock()
    store3 = FakeStore([])
    dt.save(tasks, lock, store3)
    check("save takes the lock", lock.entries == 1)
    check("save writes through", len(store3.saved) == 1)

    # ── load: corrupt/missing store defaults to [] ──────────────────────────
    class BoomStore:
        def load(self):
            raise ValueError("corrupt")

    check("unreadable store yields an empty list", dt.load(BoomStore()) == [])

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
