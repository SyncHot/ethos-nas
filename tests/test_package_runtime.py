#!/usr/bin/env python3
"""Package runtime tests: schema, offline wheelhouse, venv build, supervision.

No server and no network: the wheelhouse is generated locally and pip runs with
``--no-index``. Boots a real package process to prove the loopback contract.
"""

import base64
import hashlib
import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolate state before any backend import resolves settings.DATA_DIR.
_OWN_DATA = not os.environ.get("ETHOS_DATA_DIR")
if _OWN_DATA:
    os.environ["ETHOS_DATA_DIR"] = tempfile.mkdtemp(prefix="ethos-test-runtime-")

from backend.services import (  # noqa: E402
    package_client, package_runtime, package_wheels, packages_store,
)
from backend.services.package_schema import (  # noqa: E402
    PackageError,
    check_min_ethos_version,
    validate_manifest,
)

HEX = "a" * 64
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _rejects(fn):
    try:
        fn()
        return False
    except (PackageError, package_client.PackageUnavailable, package_client.PackageCallError):
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


WHEEL_NAME = "ethos_probe-1.0.0-py3-none-any.whl"


def _build_wheel(directory: Path) -> Path:
    """Create a minimal, valid pure-Python wheel (no build backend needed)."""
    target = directory / WHEEL_NAME
    dist_info = "ethos_probe-1.0.0.dist-info"
    members = {
        "ethos_probe/__init__.py": 'VALUE = "1.0.0"\n',
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: ethos-probe\nVersion: 1.0.0\n"
            "Summary: offline test dependency\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: ethos-test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    record = []
    with zipfile.ZipFile(target, "w") as archive:
        for name, text in members.items():
            data = text.encode()
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{name},sha256={digest},{len(data)}")
        record.append(f"{dist_info}/RECORD,,")
        archive.writestr(f"{dist_info}/RECORD", "\n".join(record) + "\n")
    return target


def _venv_manifest(**overrides) -> dict:
    manifest = {
        "id": "demo-venv",
        "name": "Demo Venv",
        "version": "1.0.0",
        "description": "Venv runtime demo",
        "icon": "🧪",
        "runtime": "venv",
        "files": [
            {"url": "https://example.test/server/main.py", "sha256": HEX, "path": "server/main.py"},
            {"url": "https://example.test/frontend/app.js", "sha256": HEX, "path": "frontend/app.js"},
        ],
        "python": {
            "entry": "server/main.py",
            "port": 8093,
            "requirements": ["ethos-probe==1.0.0"],
            "wheels": [{
                "url": f"https://example.test/wheels/{WHEEL_NAME}",
                "sha256": HEX,
                "arch": "any",
                "filename": WHEEL_NAME,
            }],
        },
        "ui": {"mode": "native", "entry": "frontend/app.js"},
    }
    manifest.update(overrides)
    return manifest


# ── Schema ──────────────────────────────────────────────────────────────────

def test_schema():
    print("Schema")
    normalized = validate_manifest(_venv_manifest())
    check("venv runtime kept", normalized["runtime"] == "venv")
    check("python block normalized", normalized["python"]["entry"] == "server/main.py")
    check("wheel arch kept", normalized["python"]["wheels"][0]["arch"] == "any")
    check("compose runtime is the default",
          validate_manifest({
              "id": "compose-pkg", "name": "C", "version": "1.0.0", "description": "d", "icon": "📦",
              "compose": {"url": "https://example.test/docker-compose.yml", "sha256": HEX},
          })["runtime"] == "compose")
    check("python block rejected on compose runtime", _rejects(lambda: validate_manifest({
        "id": "compose-pkg", "name": "C", "version": "1.0.0", "description": "d", "icon": "📦",
        "compose": {"url": "https://example.test/docker-compose.yml", "sha256": HEX},
        "python": _venv_manifest()["python"],
    })))
    check("venv requires python.port", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "port": 0}))))
    check("venv may have no ui at all", validate_manifest(_venv_manifest(ui=None))["python"]["port"] == 8093)
    check("venv rejects compose together with python", _rejects(lambda: validate_manifest(_venv_manifest(
        compose={"url": "https://example.test/docker-compose.yml", "sha256": HEX}))))
    check("unknown runtime rejected", _rejects(lambda: validate_manifest(_venv_manifest(runtime="wasm"))))
    check("entry must be shipped", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "entry": "server/missing.py"}))))
    check("requirements reject URLs", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"],
                "requirements": ["ethos-probe @ https://evil.test/x.whl"]}))))
    check("requirements reject pip options", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "requirements": ["-e ."]}))))
    check("requirements reject whitespace specs", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "requirements": ["ethos-probe >= 1"]}))))
    check("requirements accept comma specs", validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"],
                "requirements": ["ethos-probe>=1.0,<2"]}))["python"]["requirements"]
          == ["ethos-probe>=1.0,<2"])
    check("sdist wheels rejected", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "wheels": [{
            "url": "https://example.test/ethos_probe-1.0.0.tar.gz",
            "sha256": HEX, "arch": "any", "filename": "ethos_probe-1.0.0.tar.gz"}]}))))
    check("wheel filename cannot escape", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "wheels": [{
            "url": "https://example.test/x.whl", "sha256": HEX,
            "arch": "any", "filename": "../x.whl"}]}))))
    check("unknown wheel arch rejected", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "wheels": [{
            "url": "https://example.test/x.whl", "sha256": HEX,
            "arch": "riscv64", "filename": "x-1.0-py3-none-any.whl"}]}))))
    check("empty wheelhouse rejected", _rejects(lambda: validate_manifest(_venv_manifest(
        python={**_venv_manifest()["python"], "wheels": []}))))

    check("min version blocks older core", _rejects(lambda: check_min_ethos_version(
        {"min_ethos_version": "99.0.0"}, "2.0.0")))
    try:
        check_min_ethos_version({"min_ethos_version": "2.0.0"}, "2.0.0")
        check("min version allows equal core", True)
    except PackageError:
        check("min version allows equal core", False)


# ── Wheelhouse ──────────────────────────────────────────────────────────────

def test_wheelhouse():
    print("Wheelhouse")
    arch = package_wheels.host_arch()
    check("host arch known", arch in ("x86_64", "aarch64"), arch)
    cfg = {"wheels": [
        {"filename": WHEEL_NAME, "arch": "any"},
        {"filename": "pkg-1.0-cp314-cp314-manylinux_2_17_aarch64.whl", "arch": "aarch64"},
        {"filename": "pkg-1.0-cp314-cp314-manylinux_2_17_x86_64.whl", "arch": "x86_64"},
    ]}
    usable = {w["filename"] for w in package_wheels.usable_wheels(cfg)}
    check("pure wheel is always usable", WHEEL_NAME in usable)
    check("only host-arch wheels survive", len(usable) == 2, str(usable))


# ── Build + supervise ───────────────────────────────────────────────────────

def test_client():
    print("Client")
    check("unknown package is unavailable", package_client.is_available("not-installed") is False)
    check("service port needs an installed package",
          _rejects(lambda: package_client.service_port("not-installed")))
    check("calls fail closed", _rejects(lambda: package_client.post_json("not-installed", "/x", {})))
    check("health probe fails closed", _rejects(lambda: package_client.get_json("not-installed", "/health")))


ENTRY = """\
import http.server, json, os
import ethos_probe

state = os.environ["ETHOS_PACKAGE_DATA_DIR"]
with open(os.path.join(state, "probe.txt"), "w", encoding="utf-8") as fh:
    fh.write(ethos_probe.VALUE)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"value": ethos_probe.VALUE}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.HTTPServer(("127.0.0.1", int(os.environ["ETHOS_PACKAGE_PORT"])), Handler).serve_forever()
"""


def test_runtime():
    print("Runtime")
    import asyncio

    pkg_id = "ethos-runtime-probe"
    port = _free_port()
    package_dir = packages_store.package_dir(pkg_id)
    wheel_dir = package_dir / package_wheels.WHEELHOUSE_DIRNAME
    state = package_runtime.data_dir(pkg_id)
    record = {
        "id": pkg_id, "name": "Runtime Probe", "version": "1.0.0", "runtime": "venv",
        "python": {"entry": "server/main.py", "port": port, "requirements": ["ethos-probe==1.0.0"]},
        "healthcheck": {"path": "/health"},
    }

    async def scenario():
        package_dir.mkdir(parents=True, exist_ok=True)
        wheel_dir.mkdir(parents=True, exist_ok=True)
        _build_wheel(wheel_dir)
        (package_dir / "server").mkdir(parents=True, exist_ok=True)
        (package_dir / "server" / "main.py").write_text(ENTRY, encoding="utf-8")

        check("status is stopped before start", package_runtime.status(pkg_id) == "stopped")
        await package_runtime.build(package_dir, record, "test-job")
        check("venv python created", package_runtime.venv_python(package_dir).exists())
        check("wheel installed offline", (package_runtime.venv_python(package_dir).parent /
                                          "pip").exists())

        await package_runtime.start(pkg_id, record)
        check("status is running", package_runtime.status(pkg_id) == "running")
        check("entry imported the wheel", (state / "probe.txt").read_text(encoding="utf-8") == "1.0.0")

        pid_path = package_runtime._pid_file(pkg_id)
        check("pidfile written on start", pid_path.exists())
        first_pid = int(pid_path.read_text(encoding="utf-8"))
        check("pidfile names the running process",
              first_pid == package_runtime._HANDLES[pkg_id].process.pid, str(first_pid))

        # Simulate `/api/system/restart` (os.execv): the new image has no
        # handles for the still-running children.
        package_runtime._HANDLES.clear()
        await package_runtime.start(pkg_id, record)
        check("start recovers from an in-place restart",
              package_runtime.status(pkg_id) == "running")
        check("stale process was replaced",
              int(pid_path.read_text(encoding="utf-8")) != first_pid)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()
        check("health endpoint answers", response.status == 200 and "1.0.0" in body, body)

        await package_runtime.stop(pkg_id)
        check("status is stopped after stop", package_runtime.status(pkg_id) == "stopped")
        check("pidfile removed after stop", not pid_path.exists())
        released = False
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    released = True
                    break
            await asyncio.sleep(0.1)
        check("port released after stop", released)

    try:
        asyncio.run(scenario())
    finally:
        shutil.rmtree(package_dir, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)


def test_stale_process_scan():
    """Recovery without a pidfile: find the child by its env marker.

    This is the path used after a crash of the previous image, when the old
    child was reparented and no `run/<id>.pid` existed yet.
    """
    print("Stale process scan")
    pkg_id = "ethos-scan-probe"
    package_dir = packages_store.package_dir(pkg_id)
    entry = package_dir / "server" / "idle.py"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("import time\nwhile True:\n    time.sleep(0.5)\n", encoding="utf-8")
    env = {**os.environ, "ETHOS_PACKAGE_ID": pkg_id}
    proc = subprocess.Popen([sys.executable, str(entry)], env=env)
    try:
        time.sleep(0.3)
        found = package_runtime._find_package_processes(pkg_id, package_dir)
        check("scan finds the process by its env marker", proc.pid in found, str(found))
        check("cleanup without a pidfile stops it", package_runtime.cleanup_stale(pkg_id) is True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        check("process is gone", proc.poll() is not None)
        check("no pidfile to clean up", not package_runtime._pid_file(pkg_id).exists())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(package_dir, ignore_errors=True)


def main() -> int:
    try:
        test_schema()
        test_wheelhouse()
        test_client()
        test_runtime()
        test_stale_process_scan()
    finally:
        if _OWN_DATA:
            shutil.rmtree(os.environ.get("ETHOS_DATA_DIR", ""), ignore_errors=True)
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
