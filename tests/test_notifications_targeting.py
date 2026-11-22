#!/usr/bin/env python3
"""Targeted notification audience tests (service + WS frame filtering).

The Notification Center store is shared, but an event may name its audience.
Only recipients see/list/count it; broadcasts keep the legacy global behaviour.

Run: .venv/bin/python tests/test_notifications_targeting.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-notif-target-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
sys.path.insert(0, str(ROOT))

from backend.services import notifications_service as svc  # noqa: E402
from backend.routers import notifications_ws  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _ids(user: str) -> list[str]:
    events, _unread = svc.list_events(limit=50, user=user)
    return [event["id"] for event in events]


def main() -> int:
    try:
        broadcast = svc.emit_event("info", "Disk warning", "all users")
        alice_evt = svc.emit_event("info", "Shared with you", "from bob", users=["alice"])
        bob_evt = svc.emit_event("info", "Shared with you", "from carol", users=["bob"])

        check("broadcast event has an empty audience",
              (broadcast or {}).get("users") == [], str(broadcast))
        check("targeted event stores its audience",
              (alice_evt or {}).get("users") == ["alice"], str(alice_evt))

        alice_ids = _ids("alice")
        bob_ids = _ids("bob")
        check("alice sees broadcast + her own event",
              broadcast["id"] in alice_ids and alice_evt["id"] in alice_ids, str(alice_ids))
        check("alice does not see bob's event", bob_evt["id"] not in alice_ids, str(alice_ids))
        check("bob sees broadcast + his own event",
              broadcast["id"] in bob_ids and bob_evt["id"] in bob_ids, str(bob_ids))
        check("bob does not see alice's event", alice_evt["id"] not in bob_ids, str(bob_ids))

        check("unread is per user", svc.unread_count("alice") == 2, str(svc.unread_count("alice")))
        check("legacy unread counts the whole store", svc.unread_count() == 3, str(svc.unread_count()))

        check("a non-recipient cannot mark an event read",
              svc.mark_read(alice_evt["id"], "bob") is False)
        check("unread unchanged after the denied mark",
              svc.unread_count("bob") == 2, str(svc.unread_count("bob")))
        check("the recipient can mark the event read",
              svc.mark_read(alice_evt["id"], "alice") is True)
        check("alice badge drops to 1", svc.unread_count("alice") == 1, str(svc.unread_count("alice")))
        check("bob badge is unchanged", svc.unread_count("bob") == 2, str(svc.unread_count("bob")))

        alice_events, _ = svc.list_events(limit=50, user="alice")
        alice_row = next((e for e in alice_events if e["id"] == alice_evt["id"]), {})
        check("the event is returned read for alice", alice_row.get("read") is True)

        # Dedupe must not merge events with different audiences.
        dup_a = svc.emit_event("info", "Shared with you", "x", source="sharing",
                               users=["alice"], dedupe_key="share:one")
        dup_b = svc.emit_event("info", "Shared with you", "x", source="sharing",
                               users=["bob"], dedupe_key="share:one")
        check("dedupe keeps separate audiences", dup_a["id"] != dup_b["id"],
              f"{dup_a['id']} / {dup_b['id']}")
        dup_a2 = svc.emit_event("info", "Shared with you", "x", source="sharing",
                                users=["alice"], dedupe_key="share:one")
        check("dedupe still collapses the same audience", dup_a2["id"] == dup_a["id"])

        # WebSocket frame filtering uses the same audience.
        payload = {"type": "event", "event": alice_evt}
        check("WS sends a targeted event to the recipient",
              notifications_ws._hub._frame_for(payload, "alice") is not None)
        check("WS drops a targeted event for a non-recipient",
              notifications_ws._hub._frame_for(payload, "bob") is None)
        check("WS sends broadcasts to everyone",
              notifications_ws._hub._frame_for({"type": "event", "event": broadcast}, "bob") is not None)
        frame = notifications_ws._hub._frame_for({"type": "read", "event": {"id": "x"}}, "bob")
        check("WS personalises the unread count", frame and "unread" in frame, str(frame))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} notification targeting check(s) failed")
        return 1
    print(f"All {len(results)} notification targeting checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
