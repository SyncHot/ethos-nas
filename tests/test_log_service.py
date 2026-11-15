#!/usr/bin/env python3
"""Unified log store — unit contract (no server, no shared data).

Covers docs/logs.md invariants:
  • envelope shape + app/source attribution
  • ring cap, INFO+ persistence, DEBUG memory-only
  • rotation (1 MiB × N in production, tiny override here)
  • restart reload, dedupe `count`, credential redaction, clear
  • legacy `app_events.jsonl` import + archive

    .venv/bin/python tests/test_log_service.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.log_service import LogService  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-log-store-"))
    store_dir = tmp / "logs"
    service = LogService(store_dir, ring_size=100, max_bytes=4096, file_count=3, dedupe_window=60)

    print("\n[envelope]")
    service.record("info", "ethos-nas.routers.files", "copied 3 files")
    service.record("warning", "browser:logs", "filter failed", "stack", app="browser")
    service.record("error", "pkg-ui", "scan crashed", "traceback", app="pkg:antivirus")
    items = service.get(10)
    check("newest first", items[0]["message"] == "scan crashed", str(items[0])[:120])
    check("app is stored verbatim",
          {item["app"] for item in items} == {"core", "browser", "pkg:antivirus"})
    check("envelope keys", {"id", "ts", "level", "app", "source", "message", "detail", "count"} <= set(items[0]))
    check("level filter is hierarchical",
          all(item["level"] in ("warning", "error") for item in service.get(10, level="warning")))
    check("app filter matches package prefix",
          len(service.get(10, app="pkg")) == 1)

    print("\n[ring + persistence]")
    for index in range(150):
        service.record("info", "ring-probe", f"line {index}")
    check("ring is capped", service.stats()["ring"] == 100, str(service.stats()))
    service.record("debug", "ring-probe", "debug-only-marker")
    reloaded = LogService(store_dir, ring_size=100, max_bytes=4096, file_count=3)
    messages = [item["message"] for item in reloaded.get(500)]
    check("INFO survives a restart", any("line 149" == msg for msg in messages))
    check("DEBUG stays in memory", "debug-only-marker" not in messages)

    print("\n[rotation]")
    for index in range(40):
        service.record("info", "rotate-probe", f"rotation {index} " + "x" * 300)
    rotated = sorted(path.name for path in store_dir.glob("events.*.jsonl"))
    check("rotated generations exist", bool(rotated), str(rotated))
    check("generation count respects ETHOS_LOG_FILES", len(rotated) <= 3, str(rotated))
    check("active file stays under the cap",
          (store_dir / "events.jsonl").stat().st_size <= 4096,
          str((store_dir / "events.jsonl").stat().st_size))

    print("\n[dedupe]")
    before = service.stats()["ring"]
    service.record("error", "dedupe-probe", "same failure")
    service.record("error", "dedupe-probe", "same failure")
    after = service.get(500)
    deduped = [item for item in after if item["message"] == "same failure"]
    check("identical events collapse to one row", len(deduped) == 1, str(deduped))
    check("the row carries a repeat count", deduped[0]["count"] == 2, str(deduped[0]))
    check("the duplicate did not add a ring row", service.stats()["ring"] == before,
          f"{before} -> {service.stats()['ring']}")

    print("\n[redaction]")
    service.record(
        "error",
        "secret-probe",
        "GET /x?token=abcdef123456 password=hunter2 api_key=deadbeef",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig ethics ethos_0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    secret = next(item for item in service.get(5) if item["source"] == "secret-probe")
    blob = secret["message"] + " " + secret["detail"]
    check("token values are redacted", "abcdef123456" not in blob, blob[:160])
    check("passwords are redacted", "hunter2" not in blob, blob[:160])
    check("API keys are redacted", "ethos_0123456789" not in blob, blob[:160])
    check("bearer values are redacted", "eyJhbGciOiJIUzI1NiJ9" not in blob, blob[:160])

    print("\n[legacy import]")
    legacy_tmp = Path(tempfile.mkdtemp(prefix="ethos-log-legacy-"))
    legacy_file = legacy_tmp / "app_events.jsonl"
    legacy_file.write_text(json.dumps({
        "ts": 1763000000.0, "level": "error", "source": "ethos-nas.routers.files",
        "message": "legacy row", "detail": "old",
    }) + "\n", encoding="utf-8")
    imported = LogService(legacy_tmp / "logs", ring_size=100, max_bytes=4096, file_count=3)
    imported.import_legacy(legacy_file)
    check("legacy rows are imported",
          any(item["message"] == "legacy row" for item in imported.get(10)))
    check("legacy file is archived, not deleted",
          (legacy_tmp / "logs" / "legacy-app-events.jsonl").exists()
          and not legacy_file.exists())

    print("\n[clear]")
    cleared = service.clear()
    check("clear reports the removed count", cleared > 0, str(cleared))
    check("ring is empty", service.get(10) == [])
    check("active file is truncated", (store_dir / "events.jsonl").stat().st_size == 0)

    failed = [item for item in _results if not item[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
