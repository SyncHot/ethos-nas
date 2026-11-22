#!/usr/bin/env python3
"""SMART history store — schema v2 ring, filtering and the alert deltas.

Pure service test (no server): writes into a throw-away ETHOS_DATA_DIR.

Run:  .venv/bin/python tests/test_smart_history.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-smart-hist-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
sys.path.insert(0, str(ROOT))

from backend.services import health_monitor_service as hms  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _write_store(payload: dict) -> None:
    (DATA_DIR / "smart_history.json").write_text(json.dumps(payload), encoding="utf-8")


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def main() -> int:
    try:
        print("smart history — store")
        check("missing store loads as v2", hms._load_smart_history() == {"version": 2, "devices": {}})

        # Version 1 payload (attrs + prev_attrs only) must keep working.
        _write_store({"version": 1, "devices": {"sda": {
            "last_seen": _iso(timedelta(hours=1)),
            "attrs": {"Reallocated_Sector_Ct": 2},
            "prev_attrs": {"Reallocated_Sector_Ct": 1},
        }}})
        loaded = hms._load_smart_history()
        check("v1 store upgrades in memory", loaded["version"] == 2 and "sda" in loaded["devices"])
        trends = hms.smart_trends()
        entry = (trends or {}).get("devices", [{}])[0]
        check("v1 store still reports the alert delta",
              entry.get("changes") == ["Reallocated_Sector_Ct"], str(entry))

        history = hms.smart_history(24)
        device = (history.get("devices") or [{}])[0]
        check("v1 store has no samples yet", device.get("samples") == [], str(device))

        print("smart history — ring")
        now = datetime.now(timezone.utc)
        samples = []
        for index in range(hms._SMART_SAMPLES_MAX + 20):
            samples.append({"ts": _iso(timedelta(minutes=5 * (hms._SMART_SAMPLES_MAX + 20 - index))),
                            "temp": 30 + index % 5,
                            "attrs": {"Wear_Leveling_Count": 90}})
        _write_store({"version": 2, "devices": {"/dev/sda": {
            "last_seen": _iso(timedelta(minutes=1)),
            "attrs": {}, "prev_attrs": None, "samples": samples,
        }}})
        all_samples = hms.smart_history(168)["devices"][0]["samples"]
        check("history keeps every stored sample", len(all_samples) == len(samples), str(len(all_samples)))

        recent = hms.smart_history(24)["devices"][0]["samples"]
        check("24 h window filters older samples", 0 < len(recent) < len(samples), str(len(recent)))

        filtered = hms.smart_history(24, "/dev/sda")
        check("device filter returns that device", filtered["devices"][0]["device"] == "/dev/sda")
        check("device filter excludes others", hms.smart_history(24, "/dev/sdb")["devices"] == [])

        print("smart history — append")
        rec = {"samples": [{"ts": _iso(timedelta(minutes=10)), "temp": 31, "attrs": {}}]}
        for index in range(hms._SMART_SAMPLES_MAX + 5):
            rec["samples"] = hms._next_samples(rec, now + timedelta(minutes=index), {"A": index}, 32)
        check("ring is bounded", len(rec["samples"]) == hms._SMART_SAMPLES_MAX, str(len(rec["samples"])))
        check("newest sample is last", rec["samples"][-1]["temp"] == 32)
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All SMART history checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
