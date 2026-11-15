#!/usr/bin/env python3
"""Unit tests for services/json_store.py (TODO.md P0-10 / P4-09).

No server needed: this exercises the storage layer directly.

Run:
    .venv/bin/python tests/test_stores.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.json_store import Store  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-store-test-"))
    try:
        # 1. Missing file → default, no crash, nothing created on read.
        store = Store(tmp / "a.json", dict)
        check("missing file returns default", store.load() == {})
        check("read does not create the file", not (tmp / "a.json").exists())

        # 2. Round-trip + permissions.
        store.save({"hello": "world"})
        check("round-trip", store.load() == {"hello": "world"})
        check("file mode is 0600", oct(os.stat(tmp / "a.json").st_mode & 0o777) == "0o600",
              oct(os.stat(tmp / "a.json").st_mode & 0o777))
        check("saved file is valid JSON", json.loads((tmp / "a.json").read_text()) == {"hello": "world"})

        # 3. Corruption → quarantine, default returned, bad bytes preserved.
        bad = tmp / "bad.json"
        bad.write_text('{"boards": [1, 2')
        check("corrupt file returns default", Store(bad, dict).load() == {})
        quarantined = list(tmp.glob("bad.corrupt-*.json"))
        check("corrupt file was quarantined", len(quarantined) == 1, str(quarantined))
        check("quarantined file keeps the damaged bytes",
              quarantined and quarantined[0].read_text().startswith('{"boards"'))
        check("original path is gone after quarantine", not bad.exists())

        # 4. Concurrency: every increment must survive (atomic read-modify-write).
        conc = tmp / "counter.json"
        cstore = Store(conc, dict)
        cstore.save({"n": 0})

        def worker() -> None:
            for _ in range(50):
                cstore.update(lambda d: d.__setitem__("n", d["n"] + 1))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("20 threads x 50 updates == 1000", cstore.load()["n"] == 1000, str(cstore.load()["n"]))

        # 5. Version guard: a file from a newer build is never overwritten.
        versioned = tmp / "v.json"
        versioned.write_text(json.dumps({"version": 99, "data": "future"}))
        vstore = Store(versioned, dict, version=1)
        check("future version load returns default", vstore.load() == {})
        check("future version file untouched", json.loads(versioned.read_text())["version"] == 99)
        vstore.save({"after": True})
        check("save stamps the current version", json.loads(versioned.read_text())["version"] == 1)

        # 6. update() persists even when the mutator returns None.
        ustore = Store(tmp / "u.json", dict)
        ustore.update(lambda d: d.__setitem__("k", 1))
        check("update() persists mutations", ustore.load() == {"k": 1})

        # 7. Deep-copy isolation: callers cannot mutate the store by accident.
        iso = Store(tmp / "iso.json", dict)
        iso.save({"list": [1]})
        snapshot = iso.load()
        snapshot["list"].append(2)
        check("load() returns a deep copy", iso.load() == {"list": [1]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
