#!/usr/bin/env python3
"""Browser-extension download channel — `app=extension` on the app endpoint.

The Updates page shows a Browser extension card fed by
`GET /api/app/version?app=extension`; the zip itself is streamed by
`GET /api/app/download?app=extension` (authenticated, like the APKs). This test
pins the contract without booting a server: newest-semver selection, metadata
(version/sha/size), the FileResponse used for the download and the 404 when no
package is deployed.

Run:  .venv/bin/python tests/test_extension_download.py
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

DATA = tempfile.mkdtemp(prefix="ethos-extension-download-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "extension-download-test",
})

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.routers import updates  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def write_zip(folder: Path, name: str, payload: bytes) -> Path:
    path = folder / name
    path.write_bytes(payload)
    return path


def main() -> int:
    print("[extension package discovery]")
    folder = Path(DATA) / "apks" / "extension"
    folder.mkdir(parents=True, exist_ok=True)
    old_dir = write_zip(folder, "ethos-nas-downloader-1.0.0.zip", b"old")
    new_zip = write_zip(folder, "ethos-nas-downloader-1.10.2.zip", b"new-package")
    write_zip(folder, "ethos-nas-downloader-1.9.9.zip", b"middle")
    write_zip(folder, "unrelated.zip", b"ignore me")
    write_zip(folder, "ethos-nas-downloader-beta.zip", b"ignore me too")

    settings.EXTENSION_ZIP_PATH = ""
    settings.EXTENSION_DIRS = [str(folder)]

    picked = updates._extension_zip()
    check("newest semantic version wins (1.10.2 > 1.9.9)", picked == new_zip, str(picked))
    check("version comes from the filename", updates._extension_version(picked) == "1.10.2")
    check("1.10.2 sorts above 1.0.0", picked != old_dir)

    print("\n[version payload]")
    info = updates._extension_info()
    check("app id is extension", info["app"] == "extension", info.get("app"))
    check("version_name is 1.10.2", info["version_name"] == "1.10.2", info.get("version_name"))
    check("sha256 matches the file",
          info["sha256"] == hashlib.sha256(b"new-package").hexdigest(), info.get("sha256"))
    check("size_bytes matches the file", info["size_bytes"] == len(b"new-package"), info.get("size_bytes"))
    check("url is the app-scoped download", info["url"] == "/api/app/download?app=extension", info.get("url"))

    print("\n[download response]")
    import asyncio
    response = asyncio.run(updates.download_apk(app="extension"))
    check("media type is zip", getattr(response, "media_type", "") == "application/zip",
          str(getattr(response, "media_type", "")))
    check("filename is the versioned zip",
          str(getattr(response, "filename", "")).endswith("ethos-nas-downloader-1.10.2.zip"),
          str(getattr(response, "filename", "")))

    print("\n[no package deployed]")
    settings.EXTENSION_DIRS = [str(Path(DATA) / "missing")]
    try:
        updates._extension_info()
        check("version endpoint 404s without a package", False)
    except HTTPException as exc:
        check("version endpoint 404s without a package", exc.status_code == 404, str(exc.status_code))
    check("discovery returns None", updates._extension_zip() is None)

    print("\n[explicit file override]")
    settings.EXTENSION_ZIP_PATH = str(new_zip)
    settings.EXTENSION_DIRS = []
    check("override wins over the folder scan", updates._extension_zip() == new_zip)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll browser-extension download checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
