#!/usr/bin/env python3
"""Scheduled config snapshots must survive unreadable state files.

A 0600 file left in `backend/data` by a dev shell made `export_bytes()` raise
`PermissionError`; every scheduled snapshot then failed (and the archive stayed
stale). The exporter now skips unreadable files and records them in the
manifest.

Runs in-process with a throwaway data dir:

    .venv/bin/python tests/test_config_snapshot_robustness.py
"""

import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Importing backend.config reads the env — point it at a throwaway directory.
_DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-config-snapshot-"))
os.environ["ETHOS_DATA_DIR"] = str(_DATA_DIR)

from backend.config import settings  # noqa: E402
from backend.services import config_archive as ca  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    data = Path(settings.DATA_DIR)
    data.mkdir(parents=True, exist_ok=True)

    readable = data / "readable.json"
    readable.write_text('{"ok": true}', encoding="utf-8")
    unreadable = data / "unreadable.json"
    unreadable.write_text('{"secret": true}', encoding="utf-8")
    os.chmod(unreadable, 0o000)

    try:
        payload = ca.export_bytes()
    finally:
        os.chmod(unreadable, 0o600)

    check("snapshot still succeeds", bool(payload))
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read(ca.MANIFEST_NAME))
    check("readable file included", "readable.json" in names, str(sorted(names)))
    check("unreadable file omitted", "unreadable.json" not in names)
    skipped = {item.get("name") for item in manifest.get("skipped", [])}
    check("manifest records skipped file", "unreadable.json" in skipped, str(manifest.get("skipped")))

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
