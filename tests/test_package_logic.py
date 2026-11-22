#!/usr/bin/env python3
"""Package schema, version and Compose policy tests (no server)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.compose_policy import lint_compose  # noqa: E402
from backend.services.package_schema import (  # noqa: E402
    PackageError,
    safe_relative_path,
    validate_index,
    validate_manifest,
    version_gt,
)

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _ok(fn):
    try:
        fn()
        return True
    except PackageError:
        return False

HEX = "a" * 64
BASE = {
    "id": "demo-pkg", "name": "Demo", "version": "1.2.3",
    "description": "A demo", "icon": "📦", "category": "tools",
    "compose": {"url": "https://example.test/docker-compose.yml", "sha256": HEX},
}

print("Manifest")
check("valid v2 manifest", validate_manifest(dict(BASE))["id"] == "demo-pkg")
check("scripts rejected", not _ok(lambda: validate_manifest({**BASE, "scripts": {"postinst": "https://x/y.sh"}})))
check("reserved id rejected", not _ok(lambda: validate_manifest({**BASE, "id": "packages"})))
check("bad id rejected", not _ok(lambda: validate_manifest({**BASE, "id": "../etc"})))
check("missing icon rejected", not _ok(lambda: validate_manifest({k: v for k, v in BASE.items() if k != "icon"})))
check("bad compose hash rejected", not _ok(lambda: validate_manifest({**BASE, "compose": {"url": "https://x/y", "sha256": "nope"}})))
check("bad file path rejected", not _ok(lambda: validate_manifest({**BASE, "files": [
    {"url": "https://x/y", "sha256": HEX, "path": "../escape"}]})))

native = {
    "id": "demo-native", "name": "Demo Native", "version": "1.0.0",
    "description": "A native UI demo", "icon": "🧩",
    "files": [{"url": "https://x/app.js", "sha256": HEX, "path": "frontend/app.js"}],
    "ui": {"mode": "native", "entry": "frontend/app.js"},
}
native_ui = validate_manifest(native)["ui"]
check("native ui needs no port", native_ui["mode"] == "native" and native_ui["port"] is None)
check("native ui keeps entry", native_ui["entry"] == "frontend/app.js")
check("native ui entry must be shipped", not _ok(lambda: validate_manifest({
    **native, "ui": {"mode": "native", "entry": "frontend/missing.js"}})))
check("window ui still needs a port", not _ok(lambda: validate_manifest({
    **native, "ui": {"mode": "window", "path": "/"}})))
check("native entry must be js", not _ok(lambda: validate_manifest({
    **native, "ui": {"mode": "native", "entry": "frontend/app.html"}})))
window_ui = {"mode": "window", "port": 8090, "path": "/"}
check("window ui defaults to loopback exposure",
      validate_manifest({**native, "ui": dict(window_ui)})["ui"]["expose"] == "loopback")
check("lan exposure needs an auth declaration",
      not _ok(lambda: validate_manifest({**native, "ui": {**window_ui, "expose": "lan"}})))
check("lan exposure with its own login accepted",
      validate_manifest({**native, "ui": {**window_ui, "expose": "lan", "auth": "package"}})["ui"]["auth"]
      == "package")
check("lan exposure may declare no login",
      validate_manifest({**native, "ui": {**window_ui, "expose": "lan", "auth": "none"}})["ui"]["expose"]
      == "lan")
check("auth without lan exposure rejected",
      not _ok(lambda: validate_manifest({**native, "ui": {**window_ui, "auth": "package"}})))
check("unknown exposure rejected",
      not _ok(lambda: validate_manifest({**native, "ui": {**window_ui, "expose": "wan"}})))
check("native ui cannot be exposed on the lan",
      not _ok(lambda: validate_manifest({**native, "ui": {"mode": "native", "entry": "frontend/app.js",
                                                       "expose": "lan", "auth": "package"}})))
check("capabilities default to compose",
      validate_manifest(dict(BASE))["capabilities"] == ["compose"])
check("kvm capability accepted",
      validate_manifest({**BASE, "capabilities": ["compose", "kvm"]})["capabilities"]
      == ["compose", "kvm"])
check("unknown capability rejected",
      not _ok(lambda: validate_manifest({**BASE, "capabilities": ["gpu"]})))
check("capabilities must be a list",
      not _ok(lambda: validate_manifest({**BASE, "capabilities": "kvm"})))
check("compose packages require docker",
      validate_manifest(dict(BASE))["system_requires"] == ["docker"])
check("declared docker is not duplicated",
      validate_manifest({**BASE, "system_requires": ["docker"]})["system_requires"] == ["docker"])
check("samba stays after the implicit docker",
      validate_manifest({**BASE, "system_requires": ["samba"]})["system_requires"]
      == ["docker", "samba"])
check("files-only packages need no docker",
      validate_manifest(dict(native))["system_requires"] == [])
check("duplicate system requirements collapsed",
      validate_manifest({**BASE, "system_requires": ["samba", "samba"]})["system_requires"]
      == ["docker", "samba"])
check("unknown system requirement rejected",
      not _ok(lambda: validate_manifest({**BASE, "system_requires": ["nvidia"]})))
check("arbitrary apt name rejected",
      not _ok(lambda: validate_manifest({**BASE, "system_requires": ["nginx"]})))
check("system_requires must be a list",
      not _ok(lambda: validate_manifest({**BASE, "system_requires": "docker"})))
print("Index")
index = {"schema": 2, "name": "Test", "packages": [{
    "id": "demo-pkg", "name": "Demo", "version": "1.0.0", "description": "d", "icon": "📦",
    "manifest_url": "https://example.test/manifest.json", "manifest_sha256": HEX,
}]}
check("valid index", validate_index(index)["packages"][0]["id"] == "demo-pkg")
check("duplicate ids rejected", not _ok(lambda: validate_index({**index, "packages": index["packages"] * 2})))
check("reserved id in index rejected", not _ok(lambda: validate_index({**index, "packages": [
    {**index["packages"][0], "id": "trash"}]})))

print("Versions and paths")
check("1.2.0 > 1.1.9", version_gt("1.2.0", "1.1.9"))
check("1.0.0 == 1.0.0", not version_gt("1.0.0", "1.0.0"))
check("2.0.0 > 1.99.99", version_gt("2.0.0", "1.99.99"))
check("safe relative path", safe_relative_path("app/server.py") == "app/server.py")
check("absolute path rejected", not _ok(lambda: safe_relative_path("/etc/passwd")))
check("traversal rejected", not _ok(lambda: safe_relative_path("a/../../b")))

print("Compose policy")
good = {"services": {"app": {"image": "nginx:1.27", "volumes": ["data:/data"]}},
        "volumes": {"data": {}}}
errors, warnings = lint_compose(good)
check("valid compose accepted", not errors, str(errors))
for name, service in {
    "privileged": {"image": "x:1", "privileged": True},
    "host network": {"image": "x:1", "network_mode": "host"},
    "host pid": {"image": "x:1", "pid": "host"},
    "cap_add": {"image": "x:1", "cap_add": ["SYS_ADMIN"]},
    "devices": {"image": "x:1", "devices": ["/dev/sda:/dev/sda"]},
    "build": {"build": ".", "image": "x:1"},
    "docker socket": {"image": "x:1", "volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
    "outside bind": {"image": "x:1", "volumes": ["/etc:/host-etc"]},
}.items():
    errors, _ = lint_compose({"services": {"app": service}})
    check(f"compose rejects {name}", bool(errors), str(errors))
check("latest image warns", bool(lint_compose({"services": {"a": {"image": "nginx:latest"}}})[1]))

print("Compose policy — share writes")

def _share_warnings(service):
    return lint_compose({"services": {"app": {"image": "x:1", **service}}})[1]

check("rw share bind without an owner warns",
      any("PUID/PGID" in w for w in _share_warnings({"volumes": ["/mnt:/data"]})))
check("rw share bind with PUID is quiet",
      not any("PUID/PGID" in w for w in _share_warnings({
          "volumes": ["/mnt:/data"],
          "environment": {"PUID": "${ETHOS_SERVICE_UID}", "PGID": "${ETHOS_SERVICE_GID}"}})))
check("read-only share bind is quiet",
      not any("PUID/PGID" in w for w in _share_warnings({"volumes": ["/mnt:/data:ro"]})))
check("named volumes are not share binds",
      not any("PUID/PGID" in w for w in _share_warnings({"volumes": ["data:/data"]})))
check("read-only long syntax is quiet",
      not any("PUID/PGID" in w for w in _share_warnings({
          "volumes": [{"type": "bind", "source": "/media", "target": "/media", "read_only": True}]})))

print("Compose policy — package store binds")
from backend.services.packages_store import APPS_DIR, PACKAGES_ROOT  # noqa: E402


def _bind(source, **kwargs):
    return lint_compose({"services": {"app": {"image": "x:1", "volumes": [f"{source}:/data"]}}},
                        **kwargs)[0]


_own = str(APPS_DIR / "demo")
check("package may bind its own directory",
      not _bind(_own, package_id="demo", package_dir=_own))
check("package may bind its own data dir",
      not _bind(str(PACKAGES_ROOT / "data" / "demo"), package_id="demo", package_dir=_own))
check("package may not bind another package",
      bool(_bind(str(APPS_DIR / "other"), package_id="demo", package_dir=_own)))
check("package may not bind the package store",
      bool(_bind(str(PACKAGES_ROOT / "installed.json"), package_id="demo", package_dir=_own)))
check("shared data mounts still allowed", not _bind("/mnt/SharedDrive/movies"))

print("Compose policy — kvm capability")

def _devices(devices, capabilities=None):
    return lint_compose({"services": {"app": {"image": "x:1", "devices": devices}}},
                        capabilities)[0]

check("kvm device allowed with capability", not _devices(["/dev/kvm"], ["compose", "kvm"]))
check("kvm device allowed with short syntax",
      not _devices(["/dev/kvm:/dev/kvm:rwm"], ["kvm"]))
check("kvm device rejected without capability", bool(_devices(["/dev/kvm"], ["compose"])))
check("kvm device rejected with no capabilities at all", bool(_devices(["/dev/kvm"])))
check("kvm message names the capability",
      "capabilities" in " ".join(_devices(["/dev/kvm"], ["compose"])))
check("other devices rejected even with kvm capability",
      bool(_devices(["/dev/sda"], ["compose", "kvm"])))
check("mixed devices rejected", bool(_devices(["/dev/kvm", "/dev/sda"], ["kvm"])))
check("devices must be a list", bool(_devices("/dev/kvm", ["kvm"])))

print("Uninstall cleanup")
from backend.services.package_cleanup import split_images  # noqa: E402

USAGE = {
    "sha256:aa": [{"container": "pkg-a-1", "project": "pkg-a"}],
    "sha256:bb": [{"container": "pkg-b-1", "project": "pkg-b"}],
    "sha256:cc": [{"container": "pkg-a-1", "project": "pkg-a"},
                  {"container": "pkg-b-1", "project": "pkg-b"}],
}
removable, shared = split_images(
    {"img-a:1": "sha256:aa", "img-b:1": "sha256:bb", "img-c:1": "sha256:cc", "img-d:1": "sha256:dd"},
    USAGE,
    own_containers=["/pkg-a-1"],
)
check("own-only image is removable", [i["ref"] for i in removable] == ["img-a:1", "img-d:1"], str(removable))
check("image used by another package is kept",
      [i["ref"] for i in shared] == ["img-b:1", "img-c:1"], str(shared))
check("shared entry names its owner",
      shared[0]["in_use_by"][0]["project"] == "pkg-b", str(shared[0]))
check("no images means nothing to remove", split_images({}, USAGE) == ([], []))

print("Installer stale directory")
import tempfile  # noqa: E402
from backend.services import package_installer, packages_store  # noqa: E402

with tempfile.TemporaryDirectory() as _tmp:
    _root = Path(_tmp)
    _apps = _root / "apps"
    _backup = _root / ".backup"
    _apps.mkdir()
    _target = _apps / "demo-pkg"
    (_target / "server").mkdir(parents=True)
    (_target / "server" / "bootstrap.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _old_backup = packages_store.BACKUP_DIR
    packages_store.BACKUP_DIR = _backup
    try:
        check("stale package dir deleted when possible",
              package_installer._discard_dir(_target, "stale") and not _target.exists())
        _target.mkdir()
        (_target / "server").mkdir()
        # Simulate root-owned leftovers: rmtree cannot delete, the rename can.
        _real_rmtree = package_installer.shutil.rmtree
        package_installer.shutil.rmtree = lambda *a, **k: None
        try:
            moved = package_installer._discard_dir(_target, "stale")
        finally:
            package_installer.shutil.rmtree = _real_rmtree
        _backed_up = list(_backup.glob("demo-pkg-stale-*"))
        check("undeletable dir moved into .backup", moved and not _target.exists())
        check("moved dir keeps its files",
              bool(_backed_up) and (_backed_up[0] / "server").is_dir(), str(_backed_up))
        check("missing dir is a no-op", package_installer._discard_dir(_root / "nope", "stale"))
    finally:
        packages_store.BACKUP_DIR = _old_backup

print("\nPort preflight")
import socket  # noqa: E402
from backend.services import package_installer  # noqa: E402

_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_sock.bind(("0.0.0.0", 0))
_sock.listen(1)
_busy = _sock.getsockname()[1]
try:
    def _conflict():
        package_installer._preflight_ports(
            {"id": "demo-pkg", "name": "Demo", "ui": {"port": _busy}}, "demo-pkg")

    try:
        _conflict()
        _raised = None
    except PackageError as exc:
        _raised = exc
    check("a taken port fails the install before anything is deployed",
          _raised is not None and _raised.status == 409, str(_raised))
    check("the message names the port and the service",
          _raised is not None and str(_busy) in _raised.detail and "Demo" in _raised.detail,
          str(_raised.detail if _raised else ""))
    check("a free port passes", package_installer._preflight_ports(
        {"id": "demo-pkg", "name": "Demo", "ui": {"port": 0}, "healthcheck": {}}, "demo-pkg") is None)
finally:
    _sock.close()

print("\nPort remap")
from backend.services import package_ports  # noqa: E402

_demo = {"services": {"app": {"ports": ["3000:3000", "2222:22"]}}}
_text = package_ports.build_override(_demo, {3000: 3007})
check("override publishes the new host port", '"3007:3000"' in _text, _text)
check("override drops the busy one", '"3000:3000"' not in _text, _text)
check("override keeps the other mappings", '"2222:22"' in _text, _text)
check("override uses !override so compose replaces the base list",
      "ports: !override" in _text, _text)
check("no override when no service publishes that port",
      package_ports.build_override({"services": {"app": {"ports": ["8080:80"]}}}, {3000: 3007}) == "")
check("host-ip form is remapped",
      package_ports.remap_spec("127.0.0.1:3000:3000", 3000, 3007) == "127.0.0.1:3007:3000")
check("long syntax is remapped",
      package_ports.remap_spec({"published": "3000", "target": 3000}, 3000, 3007)
      == {"published": "3007", "target": 3000})
check("unrelated mapping is untouched",
      package_ports.remap_spec("2222:22", 3000, 3007) == "2222:22")

_sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
_sock2.bind(("0.0.0.0", 0))
_sock2.listen(1)
_busy2 = _sock2.getsockname()[1]
try:
    check("allocate skips a bound port", package_ports.allocate_port(_busy2) != _busy2)
    check("allocate respects ports reserved by other packages",
          package_ports.allocate_port(_busy2 + 1, reserved=[_busy2 + 1, _busy2 + 2]) >= _busy2 + 3)
finally:
    _sock2.close()

_manifest_ui = {"ui": {"mode": "window", "port": 3000}, "healthcheck": {"port": 3000}}
_plan = {"ui": 3007, "healthcheck": 3007, "remap": {3000: 3007}, "reserved": [3007]}
_record = package_installer._apply_port_plan(dict(_manifest_ui), _plan)
check("record carries the effective port",
      _record["ui"]["port"] == 3007 and _record["healthcheck"]["port"] == 3007, str(_record)[:120])
check("record keeps the remap for the next update",
      _record["port_remap"]["from"] == 3000 and _record["port_remap"]["to"] == 3007)
check("the manifest itself is never mutated", _manifest_ui["ui"]["port"] == 3000)

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
