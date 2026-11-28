"""Package format v2: schema validation, hashing and version rules.

A package is a manifest plus a Compose service and optional declarative files.
New packages may not run lifecycle scripts; legacy installed packages keep
working, but a manifest with non-null ``scripts`` is rejected for install.
"""

import hashlib
import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Optional

from backend.services import system_deps

logger = logging.getLogger("ethos-nas.services.package_schema")

# A single safe path component, used for package ids.
_PKG_ID_CORE = r"[a-z0-9][a-z0-9-]{1,48}[a-z0-9]"
PKG_ID_RE = re.compile(rf"^{_PKG_ID_CORE}$")

# Ids that would collide with other subsystems' directories under DATA_DIR.
RESERVED_IDS = frozenset({
    "packages", "trash", "snapshots", "thumbnails", "temp_chunks", "wallpapers",
    "git", "models", "temp", "users", "desktop_layouts", "app_events",
    "agent-cache", "backup", "shares", "share_links",
})

REQUIRED_MANIFEST_FIELDS = ("id", "name", "version", "description", "icon")
REQUIRED_INDEX_FIELDS = ("id", "name", "version", "description", "icon", "manifest_url")
CHANNELS = ("stable", "beta")
# Manifest capabilities a package may declare. `kvm` unlocks the /dev/kvm
# device exception in services/compose_policy.py; `dri` unlocks the /dev/dri
# render nodes (Intel/AMD VAAPI/QuickSync) and `nvidia` the /dev/nvidia* nodes
# plus `runtime: nvidia` / `deploy…devices[driver: nvidia]`. Hardware is still
# materialised by EthOS from the host probe (services/package_gpu.py).
PACKAGE_CAPABILITIES = frozenset({"compose", "kvm", "dri", "nvidia"})
# Host dependencies a package may declare in `system_requires`. The allowlist is
# the registry in services/system_deps.py: each id maps to the binaries EthOS
# probes and the apt package the operator is told to install. EthOS never runs
# apt itself (only Docker is provisionable, through the privileged helper).
PACKAGE_SYSTEM_REQUIRES = frozenset(system_deps.SYSTEM_REQUIREMENTS)
# Package runtimes. `compose` deploys a Docker Compose project (needs the engine);
# `venv` creates a private virtualenv inside the package directory, installs
# wheels from the package's offline wheelhouse and runs one loopback process as
# the service user; `files` is a declarative bundle with no service at all (a
# native UI or assets). See docs/app-store.md.
PACKAGE_RUNTIMES = frozenset({"compose", "venv", "files"})
PACKAGE_WHEEL_ARCHES = frozenset({"x86_64", "aarch64", "any"})
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
# `name[extras]` plus up to four comma-separated version clauses. Deliberately
# narrow: no URLs, no `@`, no whitespace, no pip options — the offline
# wheelhouse only ever resolves plain requirements.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}"
    r"(\[[A-Za-z0-9._,-]{1,80}\])?"
    r"((===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]{1,40})?"
    r"(,((===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]{1,40})?){0,3}$"
)
_WHEEL_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.whl$")
_REQUIRES_PYTHON_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")


class PackageError(Exception):
    """Service-level error carrying an HTTP status for the router layer."""

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def canonical_json_bytes(value: Any) -> bytes:
    """Stable JSON encoding used for hashes (sorted keys, compact separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_pkg_id(pkg_id: str) -> None:
    if not PKG_ID_RE.fullmatch(pkg_id or ""):
        raise PackageError(f"Invalid package id: {pkg_id!r}", 400)
    if pkg_id in RESERVED_IDS:
        raise PackageError(f"Package id {pkg_id!r} is reserved", 400)


def version_key(version: str) -> tuple:
    parts = []
    for segment in re.split(r"[.\-_+]", str(version).strip()):
        match = re.match(r"(\d+)", segment)
        parts.append(int(match.group(1)) if match else 0)
    return tuple(parts)


def version_gt(a: str, b: str) -> bool:
    """True when version a is strictly newer than b (numeric-aware)."""
    ka, kb = version_key(a), version_key(b)
    n = max(len(ka), len(kb))
    ka += (0,) * (n - len(ka))
    kb += (0,) * (n - len(kb))
    if ka != kb:
        return ka > kb
    return str(a).strip() > str(b).strip()


def check_min_ethos_version(manifest: dict, current: str) -> None:
    """Refuse a package that needs a newer core than the one running."""
    minimum = str(manifest.get("min_ethos_version") or "").strip()
    if minimum and version_gt(minimum, current):
        raise PackageError(
            f"Requires EthOS {minimum} or newer (running {current})", 409,
        )


def is_https(url: str) -> bool:
    if not isinstance(url, str):
        return False
    if url.startswith("https://"):
        return True
    # Test-only loopback repos; package_repo.validate_url still enforces the
    # ETHOS_PACKAGES_ALLOW_INSECURE / ALLOW_PRIVATE flags at fetch time.
    return url.startswith("http://127.0.0.1") or url.startswith("http://localhost")


def require_https(url: Optional[str], field: str) -> None:
    if url and not is_https(url):
        raise PackageError(f"{field} must be an HTTPS URL", 400)


def require_sha256(value: Optional[str], field: str) -> str:
    value = str(value or "").strip().lower()
    if not HEX64_RE.fullmatch(value):
        raise PackageError(f"{field} must be a sha256 hex digest", 400)
    return value


def safe_relative_path(path: Any, field: str = "path") -> str:
    """Reject absolute paths and traversal; return a POSIX-style relative path."""
    raw = str(path or "").strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
        raise PackageError(f"{field} must stay inside the package directory", 400)
    return pure.as_posix()


def _validate_files(files: Any) -> list[dict]:
    if files is None:
        return []
    if not isinstance(files, list):
        raise PackageError("files must be a list", 400)
    out = []
    for item in files:
        if not isinstance(item, dict):
            raise PackageError("each files entry must be an object", 400)
        url = item.get("url")
        require_https(url, "files.url")
        out.append({
            "url": url,
            "sha256": require_sha256(item.get("sha256"), "files.sha256"),
            "path": safe_relative_path(item.get("path"), "files.path"),
            "mode": int(item.get("mode") or 0o644) & 0o777,
        })
    return out


def _validate_compose(manifest: dict) -> Optional[dict]:
    compose = manifest.get("compose")
    if compose is None:
        return None
    if not isinstance(compose, dict):
        raise PackageError("compose must be an object", 400)
    require_https(compose.get("url"), "compose.url")
    return {
        "url": compose.get("url"),
        "sha256": require_sha256(compose.get("sha256"), "compose.sha256"),
    }


def _validate_ui(value: Any) -> Optional[dict]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PackageError("ui must be an object", 400)
    mode = str(value.get("mode") or "window").lower()
    if mode not in ("window", "native"):
        raise PackageError("ui.mode must be 'window' or 'native'", 400)
    port = int(value.get("port") or 0)
    if mode == "window" and not (1 <= port <= 65535):
        raise PackageError("ui.port must be 1-65535", 400)
    if port and not (1 <= port <= 65535):
        raise PackageError("ui.port must be 1-65535", 400)
    path = str(value.get("path") or "/")
    if not path.startswith("/"):
        raise PackageError("ui.path must start with /", 400)
    scheme = str(value.get("scheme") or "http")
    if scheme not in ("http", "https"):
        raise PackageError("ui.scheme must be http or https", 400)
    # Where the port can be reached from. `loopback` (default) is only reachable
    # on the NAS itself; `lan` means the browser connects straight to the port,
    # so EthOS cannot authenticate it — the window is only trusted when the app
    # brings its own login (`auth: "package"`).
    expose = str(value.get("expose") or "loopback").lower()
    if expose not in ("loopback", "lan"):
        raise PackageError("ui.expose must be 'loopback' or 'lan'", 400)
    auth = str(value.get("auth") or "").strip().lower()
    if auth and auth not in ("package", "none"):
        raise PackageError("ui.auth must be 'package' (own login) or 'none'", 400)
    if expose == "lan" and mode == "native":
        raise PackageError("ui.expose 'lan' requires ui.mode 'window'", 400)
    if expose == "lan" and not auth:
        raise PackageError(
            "ui.expose 'lan' requires ui.auth: 'package' (own login) or 'none' — "
            "EthOS cannot authenticate a page served from the package port",
            400,
        )
    if expose == "loopback" and auth:
        raise PackageError("ui.auth is only valid with ui.expose: 'lan'", 400)
    # Some apps refuse to be framed (X-Frame-Options/CSP). The browser gives the
    # window no event to detect that, so the package declares it and EthOS shows
    # the "Open in a browser tab" card instead of a permanently blank pane.
    embed = value.get("embed")
    if embed is None:
        embed = True
    if not isinstance(embed, bool):
        raise PackageError("ui.embed must be true or false", 400)
    if not embed and mode != "window":
        raise PackageError("ui.embed is only meaningful for ui.mode 'window'", 400)
    entry = None
    ribbon = None
    if mode == "native":
        entry = safe_relative_path(value.get("entry"), "ui.entry")
        if not entry.endswith((".js", ".mjs")):
            raise PackageError("ui.entry must be a .js or .mjs file", 400)
        if value.get("ribbon") is not None:
            if not isinstance(value.get("ribbon"), dict):
                raise PackageError("ui.ribbon must be an object", 400)
            ribbon_entry = safe_relative_path(value["ribbon"].get("entry"), "ui.ribbon.entry")
            if not ribbon_entry.endswith((".js", ".mjs")):
                raise PackageError("ui.ribbon.entry must be a .js or .mjs file", 400)
            title = str(value["ribbon"].get("title") or "").strip()
            if len(title) > 80:
                raise PackageError("ui.ribbon.title is limited to 80 characters", 400)
            ribbon = {"entry": ribbon_entry, "title": title or None}
    elif value.get("ribbon") is not None:
        raise PackageError("ui.ribbon requires ui.mode 'native'", 400)
    return {"mode": mode, "entry": entry, "port": port or None, "path": path, "scheme": scheme,
            "expose": expose, "auth": auth or None, "embed": embed, "ribbon": ribbon}


# ── User-supplied settings (optional, compose packages) ────────────────────
# A manifest may declare values the user fills in at install time (an admin
# login, a password, a hostname). EthOS keeps the values with the package record
# and hands them to Compose for `${VAR}` interpolation; the signed compose file
# stays fixed. Environment names are restricted so a package can never redirect
# the Docker client (DOCKER_HOST, COMPOSE_*) or EthOS itself (ETHOS_*).
SETTING_TYPES = ("text", "password")
_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SETTING_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RESERVED_SETTING_ENV = frozenset({
    "PATH", "HOME", "SHELL", "IFS", "TMPDIR", "USER", "LOGNAME",
})
_RESERVED_SETTING_ENV_PREFIXES = (
    "ETHOS_", "DOCKER_", "COMPOSE_", "LD_", "PYTHON", "BASH", "PERL", "RUBY", "GIT_",
)


def _validate_settings(value: Any) -> list[dict]:
    """Validate install-time settings; at most 16 string fields per package."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise PackageError("settings must be a list", 400)
    if len(value) > 16:
        raise PackageError("settings may declare at most 16 entries", 400)
    allowed = {"key", "env", "label", "default", "type", "required", "min_length", "max_length"}
    out: list[dict] = []
    keys: set[str] = set()
    envs: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise PackageError("each settings entry must be an object", 400)
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise PackageError(f"unknown settings field: {unknown[0]}", 400)
        key = str(item.get("key") or "").strip()
        if not _SETTING_KEY_RE.fullmatch(key):
            raise PackageError(f"invalid settings key: {key!r} (lowercase a-z, 0-9, _)", 400)
        if key in keys:
            raise PackageError(f"duplicate settings key: {key}", 400)
        env = str(item.get("env") or "").strip()
        if not _SETTING_ENV_RE.fullmatch(env):
            raise PackageError(f"invalid settings env for {key!r}: {env!r}", 400)
        if env in _RESERVED_SETTING_ENV or env.startswith(_RESERVED_SETTING_ENV_PREFIXES):
            raise PackageError(f"settings env {env!r} is reserved by EthOS or Docker", 400)
        if env in envs:
            raise PackageError(f"duplicate settings env: {env}", 400)
        setting_type = str(item.get("type") or "text").lower()
        if setting_type not in SETTING_TYPES:
            raise PackageError(f"settings type must be text or password: {setting_type!r}", 400)
        default = item.get("default")
        default = "" if default is None else str(default)
        if len(default) > 2048:
            raise PackageError(f"settings default for {key!r} is too long", 400)
        try:
            min_length = int(item.get("min_length") or 0)
            max_length = int(item.get("max_length") or 256)
        except (TypeError, ValueError):
            raise PackageError(f"settings lengths for {key!r} must be integers", 400) from None
        if not 0 <= min_length <= 1024 or not 1 <= max_length <= 4096 or min_length > max_length:
            raise PackageError(f"settings lengths for {key!r} are out of range", 400)
        if default and not min_length <= len(default) <= max_length:
            # A default that the validator would reject makes every install fail.
            raise PackageError(f"settings default for {key!r} violates its length limits", 400)
        out.append({
            "key": key,
            "env": env,
            "label": str(item.get("label") or key).strip()[:80],
            "type": setting_type,
            "default": default,
            "required": bool(item.get("required", False)),
            "min_length": min_length,
            "max_length": max_length,
        })
        keys.add(key)
        envs.add(env)
    return out


# ── Data folder (optional, compose packages) ───────────────────────────────
# A package may declare the container path its data lives in. EthOS mounts a
# host folder there (`${ETHOS_PACKAGE_DATA_DIR}`): by default
# `<DATA_DIR>/packages/data/<id>`, but the admin can pick any folder under
# /home, /mnt, /media or the EthOS data root at install time and later.
def _validate_data(value: Any) -> Optional[dict]:
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise PackageError("data must be an object with a mount path", 400)
    unknown = sorted(set(value) - {"mount", "label", "uid", "gid"})
    if unknown:
        raise PackageError(f"unknown data field: {unknown[0]}", 400)
    mount = str(value.get("mount") or "").strip()
    if not mount.startswith("/") or ".." in PurePosixPath(mount).parts:
        raise PackageError("data.mount must be an absolute path inside the container", 400)

    def _id(name: str) -> Optional[int]:
        raw = value.get(name)
        if raw is None:
            return None
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise PackageError(f"data.{name} must be an integer", 400) from None
        if not 0 <= number <= 2**31 - 1:
            raise PackageError(f"data.{name} is out of range", 400)
        return number

    return {
        "mount": mount[:200],
        "label": str(value.get("label") or "Data folder").strip()[:80] or "Data folder",
        # Images that run as a fixed non-root uid (Grafana 13 is 472) cannot
        # write to a folder owned by the EthOS service user; the installer
        # grants this uid/gid access with a POSIX ACL.
        "uid": _id("uid"),
        "gid": _id("gid"),
    }


def _validate_requirement(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _REQUIREMENT_RE.fullmatch(text):
        raise PackageError(
            f"{field} must be 'name' or 'name<op>version' (no URLs, extras spaces or pip options)",
            400,
        )
    return text


def _validate_wheels(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise PackageError("python.wheels must be a non-empty list (offline install)", 400)
    if len(value) > 200:
        raise PackageError("python.wheels is limited to 200 entries", 400)
    out = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise PackageError("each python.wheels entry must be an object", 400)
        filename = str(item.get("filename") or "").strip()
        if not _WHEEL_FILE_RE.fullmatch(filename):
            raise PackageError(
                f"python.wheels filename must be a bare .whl file name: {filename!r}", 400,
            )
        if filename in seen:
            raise PackageError(f"Duplicate wheel filename: {filename}", 400)
        seen.add(filename)
        arch = str(item.get("arch") or "").strip().lower()
        if arch not in PACKAGE_WHEEL_ARCHES:
            raise PackageError(
                f"python.wheels arch must be one of {', '.join(sorted(PACKAGE_WHEEL_ARCHES))}", 400,
            )
        require_https(item.get("url"), "python.wheels.url")
        out.append({
            "url": item.get("url"),
            "sha256": require_sha256(item.get("sha256"), "python.wheels.sha256"),
            "arch": arch,
            "filename": filename,
        })
    return out


def _validate_python(manifest: dict, runtime: str, files: list[dict]) -> Optional[dict]:
    """Normalize the `python` block; only `runtime: venv` packages may have one."""
    value = manifest.get("python")
    if runtime != "venv":
        if value:
            raise PackageError("python is only valid with runtime: 'venv'", 400)
        return None
    if not isinstance(value, dict):
        raise PackageError("runtime 'venv' requires a python object", 400)
    entry = safe_relative_path(value.get("entry"), "python.entry")
    if not entry.endswith(".py"):
        raise PackageError("python.entry must be a .py file", 400)
    if entry not in {item["path"] for item in files}:
        raise PackageError("python.entry must be listed in files[]", 400)

    port = int(value.get("port") or 0)
    if not 1 <= port <= 65535:
        raise PackageError("python.port must be 1-65535 (the loopback service port)", 400)

    requirements = value.get("requirements")
    if not isinstance(requirements, list):
        raise PackageError("python.requirements must be a list", 400)
    if len(requirements) > 100:
        raise PackageError("python.requirements is limited to 100 entries", 400)
    requirements = [_validate_requirement(item, "python.requirements") for item in requirements]
    wheels_value = value.get("wheels") or []
    # A stdlib-only service has no dependencies at all; anything with
    # requirements must ship the wheels that satisfy them offline.
    if requirements and not wheels_value:
        raise PackageError("python.wheels must list the wheels for python.requirements", 400)

    requires_python = str(value.get("requires_python") or "").strip()
    if requires_python and not _REQUIRES_PYTHON_RE.fullmatch(requires_python):
        raise PackageError("python.requires_python must look like '3.14'", 400)

    return {
        "entry": entry,
        "port": port,
        "requirements": requirements,
        "wheels": _validate_wheels(wheels_value) if wheels_value else [],
        "requires_python": requires_python or None,
    }


def _validate_runtime(manifest: dict) -> str:
    declared = str(manifest.get("runtime") or "").strip().lower()
    if declared:
        if declared not in PACKAGE_RUNTIMES:
            raise PackageError(
                f"Unknown runtime: {declared} (known: {', '.join(sorted(PACKAGE_RUNTIMES))})", 400,
            )
        return declared
    # Infer: a compose file means the compose runtime, everything else is a
    # declarative bundle (UI or assets) with no service.
    if manifest.get("compose") or manifest.get("compose_url"):
        return "compose"
    return "files"


def _reject_scripts(manifest: dict) -> None:
    scripts = manifest.get("scripts")
    if not scripts:
        return
    if isinstance(scripts, dict) and any(v for v in scripts.values() if v):
        raise PackageError(
            "Lifecycle scripts are not supported for new installs; ship compose + files instead",
            400,
        )


def validate_manifest(manifest: Any, *, allow_legacy: bool = False) -> dict:
    """Validate and normalize a manifest (v2, or legacy v1 without scripts)."""
    if not isinstance(manifest, dict):
        raise PackageError("Manifest must be a JSON object", 400)
    for field in REQUIRED_MANIFEST_FIELDS:
        if not str(manifest.get(field, "")).strip():
            raise PackageError(f"Manifest missing required field: {field}", 400)
    check_pkg_id(str(manifest["id"]))
    _reject_scripts(manifest)
    runtime = _validate_runtime(manifest)

    compose = _validate_compose(manifest)
    legacy_compose = manifest.get("compose_url")
    require_https(legacy_compose, "compose_url")
    if compose is None and legacy_compose and not allow_legacy:
        raise PackageError("compose_url is legacy; use compose {url, sha256}", 400)
    if compose is None and not legacy_compose and not manifest.get("files"):
        raise PackageError("Package must declare compose or files", 400)

    capabilities = manifest.get("capabilities") or (
        ["compose"] if (compose or manifest.get("compose_url")) else []
    )
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise PackageError("capabilities must be a list of strings", 400)
    unknown = sorted({item for item in capabilities if item not in PACKAGE_CAPABILITIES})
    if unknown:
        raise PackageError(
            f"Unknown package capability: {unknown[0]} "
            f"(known: {', '.join(sorted(PACKAGE_CAPABILITIES))})", 400,
        )
    capabilities = list(dict.fromkeys(capabilities))

    system_requires = manifest.get("system_requires") or []
    if not isinstance(system_requires, list) or not all(isinstance(item, str) for item in system_requires):
        raise PackageError("system_requires must be a list of strings", 400)
    unknown = sorted({item for item in system_requires if item not in PACKAGE_SYSTEM_REQUIRES})
    if unknown:
        raise PackageError(
            f"Unknown system requirement: {unknown[0]} "
            f"(known: {', '.join(sorted(PACKAGE_SYSTEM_REQUIRES))})", 400,
        )
    system_requires = list(dict.fromkeys(system_requires))
    if (compose or legacy_compose) and "docker" not in system_requires:
        # A compose package cannot deploy without the engine. Declaring it here
        # makes the catalog preflight (`system_status`) and the provisioning path
        # universal instead of failing late in the `start` step.
        system_requires.insert(0, "docker")

    files = _validate_files(manifest.get("files"))
    python = _validate_python(manifest, runtime, files)
    settings = _validate_settings(manifest.get("settings"))
    if settings and not (compose or legacy_compose):
        # Settings exist to feed compose interpolation; a files-only package has
        # nowhere to receive them.
        raise PackageError("settings require a compose service", 400)
    data = _validate_data(manifest.get("data"))
    if data and not (compose or legacy_compose):
        raise PackageError("data requires a compose service", 400)

    normalized = {
        "schema": 2,
        "runtime": runtime,
        "id": str(manifest["id"]),
        "name": str(manifest["name"]).strip()[:120],
        "version": str(manifest["version"]).strip()[:40],
        "description": str(manifest["description"]).strip()[:2000],
        "icon": str(manifest.get("icon") or "📦")[:8],
        "icon_url": manifest.get("icon_url") or None,
        "category": str(manifest.get("category") or "other").strip()[:40],
        "author": str(manifest.get("author") or "").strip()[:120],
        "homepage": manifest.get("homepage") or None,
        "license": str(manifest.get("license") or "").strip()[:80],
        "channel": str(manifest.get("channel") or "stable").lower(),
        "min_ethos_version": manifest.get("min_ethos_version") or None,
        "changelog": str(manifest.get("changelog") or "").strip()[:4000],
        "screenshots": [s for s in (manifest.get("screenshots") or []) if is_https(s)][:8],
        "requires": manifest.get("requires") or [],
        "conflicts": manifest.get("conflicts") or [],
        "user_access": bool(manifest.get("user_access", False)),
        "capabilities": capabilities,
        "system_requires": system_requires,
        "compose": compose,
        "files": files,
        "python": python,
        "settings": settings,
        "data": data,
        "ui": _validate_ui(manifest.get("ui")),
        "healthcheck": _validate_ui(manifest.get("healthcheck")),
    }
    if normalized["channel"] not in CHANNELS:
        raise PackageError("channel must be stable or beta", 400)
    if not isinstance(normalized["requires"], list) or not isinstance(normalized["conflicts"], list):
        raise PackageError("requires/conflicts must be lists", 400)
    for field in ("requires", "conflicts"):
        for dep_id in normalized[field]:
            if not isinstance(dep_id, str) or not PKG_ID_RE.fullmatch(dep_id):
                raise PackageError(f"{field} entries must be package ids: {dep_id!r}", 400)
        normalized[field] = list(dict.fromkeys(normalized[field]))
        if normalized["id"] in normalized[field]:
            raise PackageError(f"{field} cannot reference the package itself", 400)
    ui = normalized["ui"]
    if ui and ui.get("mode") == "native":
        shipped = {item["path"] for item in normalized["files"]}
        if ui.get("entry") not in shipped:
            raise PackageError("ui.entry must be listed in files[]", 400)
        if ui.get("ribbon") and ui["ribbon"].get("entry") not in shipped:
            raise PackageError("ui.ribbon.entry must be listed in files[]", 400)
    if runtime == "venv":
        # The core process reaches a venv package over loopback only. The port is
        # declared under `python` (an engine package has no UI at all); `ui` is
        # optional and only adds a window/desktop entry.
        if compose:
            raise PackageError("runtime 'venv' cannot also declare compose", 400)
    if runtime == "files" and compose:
        raise PackageError("runtime 'files' cannot declare compose; use runtime 'compose'", 400)
    if normalized["user_access"]:
        # `user_access` lets every signed-in account (not only admins) call the
        # package's native UI proxy, so it is limited to native UIs. The source
        # must also be pinned — enforced again in services/package_repo.py.
        if not (normalized["ui"] and normalized["ui"].get("mode") == "native"):
            raise PackageError("user_access requires ui.mode 'native'", 400)
        if not (normalized["ui"].get("port") or python and python.get("port")):
            raise PackageError("user_access requires a loopback port for the proxy", 400)
    if legacy_compose:
        normalized["compose_url"] = legacy_compose
    return normalized


def validate_index(index: Any) -> dict:
    """Validate and normalize a repository index (schema 1 or 2)."""
    if not isinstance(index, dict):
        raise PackageError("Repository index must be a JSON object", 400)
    packages = index.get("packages")
    if not isinstance(packages, list):
        raise PackageError("Repository index must contain a packages list", 400)
    out = []
    seen = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise PackageError("each package entry must be an object", 400)
        for field in REQUIRED_INDEX_FIELDS:
            if not str(entry.get(field, "")).strip():
                raise PackageError(f"Index package missing {field}", 400)
        check_pkg_id(str(entry["id"]))
        if entry["id"] in seen:
            raise PackageError(f"Duplicate package id in index: {entry['id']}", 400)
        seen.add(entry["id"])
        require_https(entry.get("manifest_url"), "manifest_url")
        manifest_sha = entry.get("manifest_sha256")
        if manifest_sha:
            require_sha256(manifest_sha, "manifest_sha256")
        out.append({
            **entry,
            "schema": int(entry.get("schema") or index.get("schema") or 1),
            "manifest_sha256": str(manifest_sha).lower() if manifest_sha else None,
            "channel": str(entry.get("channel") or "stable").lower(),
            "requires": entry.get("requires") or [],
            "screenshots": [s for s in (entry.get("screenshots") or []) if is_https(s)][:8],
        })
    return {
        "schema": int(index.get("schema") or 1),
        "name": str(index.get("name") or "Package repository")[:160],
        "publisher": index.get("publisher") or {},
        "generated_at": index.get("generated_at"),
        "packages": out,
    }
