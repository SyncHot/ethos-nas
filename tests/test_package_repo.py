#!/usr/bin/env python3
"""Signed repository tests: index signature, manifest hash, tamper rejection."""
import asyncio
import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-pkg-repo-"))
os.environ["ETHOS_DATA_DIR"] = str(TMP / "data")
os.environ["ETHOS_JWT_SECRET"] = "pkg-repo-test"
os.environ["ETHOS_PACKAGES_ALLOW_INSECURE"] = "1"
os.environ["ETHOS_PACKAGES_ALLOW_PRIVATE"] = "1"
sys.path.insert(0, str(ROOT))

from backend.services import package_repo, packages_store  # noqa: E402
from backend.services.package_schema import PackageError  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> int:
    port = free_port()
    repo = TMP / "repo"
    pkg = repo / "demo"
    pkg.mkdir(parents=True)
    (pkg / "app.txt").write_text("signed payload\n", encoding="utf-8")
    manifest = {
        "schema": 2, "id": "demo", "name": "Demo", "version": "2.0.0",
        "description": "Signed test", "icon": "📦", "category": "tools",
        "files": [{
            "url": f"http://127.0.0.1:{port}/demo/app.txt",
            "sha256": sha(pkg / "app.txt"), "path": "app.txt",
        }],
    }
    manifest_path = pkg / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    index = {
        "schema": 2, "name": "Signed repo",
        "packages": [{
            "id": "demo", "name": "Demo", "version": "2.0.0",
            "description": "Signed test", "icon": "📦", "category": "tools",
            "manifest_url": f"http://127.0.0.1:{port}/demo/manifest.json",
            "manifest_sha256": sha(manifest_path), "channel": "stable",
        }],
    }
    index_path = repo / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")

    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    (repo / "index.json.sig").write_bytes(base64.b64encode(key.sign(index_path.read_bytes())))

    handler = functools.partial(Quiet, directory=str(repo))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        package_repo.add_source(
            f"http://127.0.0.1:{port}/index.json", name="Signed",
            public_key=pub_b64, trust="pinned",
        )
        catalog = asyncio.run(package_repo.browse())
        check("signed catalog loads", [p["id"] for p in catalog["packages"]] == ["demo"], str(catalog["errors"]))

        entry = catalog["packages"][0]
        fetched = asyncio.run(package_repo.fetch_manifest(entry))
        check("manifest verifies", fetched["id"] == "demo")

        # Tamper the index; force a refresh with no cache.
        index_path.write_text(json.dumps({**index, "name": "Tampered"}), encoding="utf-8")
        packages_store.save_cache({})
        catalog = asyncio.run(package_repo.browse(force=True))
        check("tampered index rejected", not catalog["packages"] and bool(catalog["errors"]), str(catalog["errors"]))

        # Tamper the manifest but keep the index hash: fetch_manifest must reject.
        index_path.write_bytes(index_path.read_bytes())  # restore signed bytes is not possible after rewrite
        manifest_path.write_text(json.dumps({**manifest, "version": "9.9.9"}), encoding="utf-8")
        try:
            asyncio.run(package_repo.fetch_manifest(entry))
            ok = False
        except PackageError:
            ok = True
        check("tampered manifest rejected", ok)
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
