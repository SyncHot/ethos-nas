#!/usr/bin/env python3
"""Install-time package settings (no server, no Docker).

Covers the manifest schema, value validation (defaults, required, length limits),
compose interpolation environment and the API redaction of stored passwords.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-pkg-settings-"))
# Never touch the live data directory: importing package_installer reads config.
os.environ["ETHOS_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))

from backend.routers import packages as packages_router  # noqa: E402
from backend.services import package_installer, packages_store  # noqa: E402
from backend.services.package_schema import PackageError, validate_manifest  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def rejects(fn):
    try:
        fn()
        return False
    except PackageError:
        return True


HEX = "a" * 64
BASE = {
    "id": "demo-pkg", "name": "Demo", "version": "1.0.0",
    "description": "A demo", "icon": "📦", "category": "tools",
    "compose": {"url": "https://example.test/docker-compose.yml", "sha256": HEX},
}
SETTINGS = [
    {"key": "admin_user", "env": "GF_SECURITY_ADMIN_USER", "label": "Admin login",
     "default": "admin", "required": True, "max_length": 40},
    {"key": "admin_password", "env": "GF_SECURITY_ADMIN_PASSWORD", "label": "Admin password",
     "type": "password", "default": "admin", "required": True,
     "min_length": 5, "max_length": 128},
]

print("Manifest schema")
normalized = validate_manifest({**BASE, "settings": SETTINGS})
check("settings are normalized", [d["key"] for d in normalized["settings"]] == ["admin_user", "admin_password"])
check("type defaults to text", normalized["settings"][0]["type"] == "text")
check("length limits survive", normalized["settings"][1]["min_length"] == 5
      and normalized["settings"][1]["max_length"] == 128)
check("no settings is an empty list", validate_manifest(dict(BASE))["settings"] == [])
check("unknown setting field rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "pattern": "x"}]})))
check("invalid key rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "key": "Admin"}]})))
check("invalid env rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "env": "gf_admin"}]})))
check("reserved env rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "env": "DOCKER_HOST"}]})))
check("ETHOS_ env rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "env": "ETHOS_DATA_DIR"}]})))
check("duplicate keys rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [SETTINGS[0], SETTINGS[0]]})))
check("bad type rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "type": "number"}]})))
check("min > max rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[0], "min_length": 60, "max_length": 10}]})))
check("default outside the length limits rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [{**SETTINGS[1], "default": "abc"}]})))
check("settings on a files-only package rejected",
      rejects(lambda: validate_manifest({
          **BASE, "compose": None, "settings": SETTINGS,
          "files": [{"url": "https://example.test/a.txt", "sha256": HEX, "path": "a.txt"}],
      })))
check("more than 16 settings rejected",
      rejects(lambda: validate_manifest({**BASE, "settings": [
          {**SETTINGS[0], "key": f"k{i}", "env": f"VAR_{i}"} for i in range(17)
      ]})))

print("Setting values")
defs = normalized["settings"]
values = package_installer.validate_settings_values(defs, {})
check("defaults apply", values == {"admin_user": "admin", "admin_password": "admin"})
values = package_installer.validate_settings_values(defs, {"admin_user": "boss"})
check("provided values win, the rest keep defaults",
      values == {"admin_user": "boss", "admin_password": "admin"})
values = package_installer.validate_settings_values(
    defs, {"admin_password": "hunter22"}, base={"admin_user": "keepme", "admin_password": "old"})
check("omitted keys keep the stored value",
      values == {"admin_user": "keepme", "admin_password": "hunter22"})
check("unknown key rejected",
      rejects(lambda: package_installer.validate_settings_values(defs, {"nope": "x"})))
check("too short rejected",
      rejects(lambda: package_installer.validate_settings_values(defs, {"admin_password": "abcd"})))
check("too long rejected",
      rejects(lambda: package_installer.validate_settings_values(
          defs, {"admin_password": "x" * 129})))
check("scalar values are coerced to strings",
      package_installer.validate_settings_values(defs, {"admin_user": 5})["admin_user"] == "5")
required_only = [{"key": "token", "env": "APP_TOKEN", "type": "password", "required": True}]
check("required without a default is enforced",
      rejects(lambda: package_installer.validate_settings_values(required_only, {})))
check("required can be relaxed for updates",
      package_installer.validate_settings_values(required_only, {}, enforce_required=False) == {"token": ""})

print("Compose environment")
defs_only = [{"key": "admin_user", "env": "GF_SECURITY_ADMIN_USER", "default": "admin"},
             {"key": "admin_password", "env": "GF_SECURITY_ADMIN_PASSWORD",
              "type": "password", "default": "secret"}]
env = package_installer.settings_env({"settings": defs_only,
                                      "settings_values": {"admin_user": "boss"}})
check("settings map to env names", env == {"GF_SECURITY_ADMIN_USER": "boss",
                                           "GF_SECURITY_ADMIN_PASSWORD": "secret"})
original_get = packages_store.get_package
packages_store.get_package = lambda pkg_id: {
    "id": "grafana", "settings": defs_only,
    "settings_values": {"admin_user": "boss", "admin_password": "hunter22"},
}
try:
    compose_env = package_installer._compose_env("grafana")
    check("_compose_env injects stored values",
          compose_env.get("GF_SECURITY_ADMIN_PASSWORD") == "hunter22"
          and compose_env.get("GF_SECURITY_ADMIN_USER") == "boss")
    check("_compose_env keeps the EthOS identity",
          "ETHOS_SERVICE_UID" in compose_env and "ETHOS_DATA_DIR" in compose_env)
    check("no package means no injected settings",
          "GF_SECURITY_ADMIN_PASSWORD" not in package_installer._compose_env())
finally:
    packages_store.get_package = original_get

print("API response redaction")
record = {
    "id": "grafana", "name": "Grafana", "settings": defs,
    "settings_values": {"admin_user": "boss", "admin_password": "hunter22"},
}
view = packages_router._record_view(record)
check("password never leaves the NAS", view["settings_values"]["admin_password"] == "")
check("text value is available to the form", view["settings_values"]["admin_user"] == "boss")
check("the stored record is not mutated",
      record["settings_values"]["admin_password"] == "hunter22")

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
