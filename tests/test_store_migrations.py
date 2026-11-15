#!/usr/bin/env python3
"""Store schema versions and the migration hook (TODO.md P5-05).

Runs in-process against a temp dir — no server needed.

    .venv/bin/python tests/test_store_migrations.py
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import json_store  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-migrate-"))
    try:
        # 1. Fresh store gets the declared version stamped on first save.
        path = tmp / "widgets.json"
        store = json_store.get(path, dict, version=2)
        store.save({"items": []})
        check("save stamps the schema version", json.loads(path.read_text())["version"] == 2)

        # 2. v1 → v2 migrates, persists and is logged via the new shape.
        path2 = tmp / "legacy.json"
        path2.write_text(json.dumps({"version": 1, "name": "old"}))

        def migrate_v2(old: int, new: int, data: dict) -> dict:
            data["renamed"] = data.pop("name")
            data["migrated_from"] = old
            return data

        store2 = json_store.get(path2, dict, version=2, migrate=migrate_v2)
        loaded = store2.load()
        check("migrator ran", loaded.get("renamed") == "old" and "name" not in loaded, str(loaded))
        check("migrator result is persisted", json.loads(path2.read_text()).get("version") == 2)
        check("pre-migration backup kept",
              any(p.name.startswith("legacy.v1-") for p in tmp.iterdir()),
              str([p.name for p in tmp.iterdir()]))
        check("second load does not re-migrate", store2.load() == loaded)

        # 3. A failing migrator leaves the file untouched.
        path3 = tmp / "broken.json"
        original = {"version": 1, "keep": True}
        path3.write_text(json.dumps(original))

        def boom(old: int, new: int, data: dict) -> dict:
            raise RuntimeError("nope")

        store3 = json_store.get(path3, dict, version=2, migrate=boom)
        check("failed migration returns the default", store3.load() == {})
        check("failed migration leaves data untouched", json.loads(path3.read_text()) == original)

        # 4. Downgrade guard: a newer file is never used or overwritten.
        path4 = tmp / "future.json"
        path4.write_text(json.dumps({"version": 9, "precious": True}))
        store4 = json_store.get(path4, dict, version=2)
        check("newer version is refused", store4.load() == {})
        check("newer file left intact", json.loads(path4.read_text())["precious"] is True)

        # 5. Missing migrator is tolerated but warns (no crash, version bumped on next save).
        path5 = tmp / "shapeless.json"
        path5.write_text(json.dumps({"version": 1, "value": 7}))
        store5 = json_store.get(path5, dict, version=2)
        check("no migrator still loads the data", store5.load().get("value") == 7)

        # 6. Registry report lists versions for /system/doctor.
        report = {Path(e["path"]).name: e for e in json_store.store_registry_report()}
        check("registry reports the version", report.get("widgets.json", {}).get("version") == 2)
        check("registry reports migrators", report.get("legacy.json", {}).get("migrator") is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
