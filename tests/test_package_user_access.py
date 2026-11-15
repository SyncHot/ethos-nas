#!/usr/bin/env python3
"""Package `user_access` policy tests (no server, no network).

`user_access: true` lets every signed-in account (not only admins) reach a
package's native-UI proxy, so the schema keeps it narrow: native UI only, with a
loopback port, and only from a pinned source (checked in package_repo).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.package_schema import PackageError, validate_manifest  # noqa: E402

HEX = "a" * 64
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _rejects(fn):
    try:
        fn()
        return False
    except PackageError:
        return True


def _manifest(**overrides) -> dict:
    manifest = {
        "id": "user-app",
        "name": "User App",
        "version": "1.0.0",
        "description": "Native UI shared by every account",
        "icon": "🧪",
        "user_access": True,
        "runtime": "venv",
        "files": [
            {"url": "https://example.test/server/main.py", "sha256": HEX, "path": "server/main.py"},
            {"url": "https://example.test/frontend/app.js", "sha256": HEX, "path": "frontend/app.js"},
        ],
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": 8098},
        "python": {"entry": "server/main.py", "port": 8098, "requirements": [], "wheels": []},
    }
    manifest.update(overrides)
    return manifest


def main() -> int:
    print("user_access")
    normalized = validate_manifest(_manifest())
    check("flag survives normalization", normalized["user_access"] is True)
    check("default is False", validate_manifest(_manifest(user_access=False))["user_access"] is False)

    check("requires a native UI", _rejects(lambda: validate_manifest(_manifest(
        ui={"mode": "window", "port": 8098, "path": "/"}))))
    check("requires a UI", _rejects(lambda: validate_manifest(_manifest(ui=None))))
    check("requires a loopback port", _rejects(lambda: validate_manifest(_manifest(
        ui={"mode": "native", "entry": "frontend/app.js"},
        python={"entry": "server/main.py", "requirements": [], "wheels": []}))))

    print("stdlib-only venv packages")
    stdlib = validate_manifest(_manifest(user_access=False))
    check("empty requirements allowed", stdlib["python"]["requirements"] == [])
    check("empty wheelhouse allowed", stdlib["python"]["wheels"] == [])
    check("requirements without wheels rejected", _rejects(lambda: validate_manifest(_manifest(
        user_access=False,
        python={"entry": "server/main.py", "port": 8098, "requirements": ["yt-dlp"]}))))

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
