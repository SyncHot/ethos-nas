"""Host (OS level) requirements a package declares through ``system_requires``.

A package never runs ``apt`` itself. Two narrow privileged helpers exist, both
installed by ``setup_scripts/setup_service.sh`` with argument-exact sudoers rules:

  * ``/usr/local/bin/ethos-docker-install`` (argument-free, Docker's own repo) —
    see ``services/docker_host.py``;
  * ``/usr/local/bin/ethos-system-deps <install|remove> <id>`` — one id per call,
    validated against the allowlist compiled into that root-owned script.

Registry ids are the only accepted values in a manifest
(``services/package_schema.PACKAGE_SYSTEM_REQUIRES`` is derived from this map),
so a manifest can never smuggle in an arbitrary command or package name.

Lifecycle (this module):

  * ``ensure()`` at install/update — probe the host, install what is missing
    through the helper, and record that *EthOS* installed it.
  * ``release()`` at uninstall — remove a requirement only when no other
    installed package still declares it **and** EthOS is the one that installed
    it. Software the operator installed by hand is never touched.

``docker`` is deliberately excluded from ``release()``: the engine is shared
infrastructure for every compose package, so it is never removed automatically.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Iterable, Optional

from backend.config import settings
from backend.services import json_store

logger = logging.getLogger("ethos-nas.services.system_deps")

# Directories probed for a required binary. `shutil.which` alone is not enough:
# the service account's PATH usually lacks /usr/sbin, where smbd lives.
_SEARCH_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/local/bin", "/usr/local/sbin")

# Privileged helper for OS packages (argument-exact sudoers rule per id).
HELPER_PATH = "/usr/local/bin/ethos-system-deps"
_HELPER_TIMEOUT = 600

# id -> metadata.
#   bins          — every binary that must exist for the requirement to be met
#   apt           — Debian/Ubuntu package installed through the helper
#   serviceable   — EthOS may install AND remove it through HELPER_PATH
#   autoremove    — may be removed on uninstall when nobody needs it any more
#   provisionable — handled by a dedicated helper (Docker Engine)
#   optional      — a recommended extra: a missing tool never fails an install
SYSTEM_REQUIREMENTS: dict[str, dict] = {
    "docker": {
        "label": "Docker Engine",
        "bins": ("docker",),
        "apt": "docker.io",
        "serviceable": False,
        "autoremove": False,
        "provisionable": True,
    },
    "samba": {
        "label": "Samba",
        "bins": ("smbd", "testparm", "smbpasswd"),
        "apt": "samba",
        "serviceable": True,
        "autoremove": True,
        "provisionable": False,
    },
    "archive-tools": {
        # bsdtar opens .7z/.rar/.zst/.lz4; 7-Zip adds ZIPX, split volumes
        # (.001/.partN.rar) and a stdin-only password prompt. Declared by
        # download-manager; File Manager extraction uses the same binaries.
        "label": "Archive tools (bsdtar, 7-Zip)",
        "bins": ("bsdtar", "7z"),
        "apt": "libarchive-tools 7zip",
        "serviceable": True,
        "autoremove": True,
        "provisionable": False,
        "optional": True,
    },
}

# Requirements EthOS installed itself. Only these may ever be removed again —
# see release(). Versioned store so the shape can grow without data loss.
OWNERS_STORE = json_store.get(
    settings.DATA_DIR / "packages" / "system_deps.json",
    version=1,
    mode=0o660,
)


def _load_owned() -> dict:
    """Requirements EthOS installed. An unreadable store must not break uninstall."""
    try:
        data = OWNERS_STORE.load()
    except Exception:
        logger.error("Cannot read the system dependency ownership store", exc_info=True)
        return {}
    owned = data.get("owned") if isinstance(data, dict) else None
    return owned if isinstance(owned, dict) else {}


def _save_owned(owned: dict) -> None:
    """Persist the ownership map (json_store.update mutates the loaded value)."""
    def _apply(data):
        if not isinstance(data, dict):
            raise TypeError("system dependency store must be a JSON object")
        data["owned"] = owned

    OWNERS_STORE.update(_apply)


def owned_requirements() -> dict:
    """Requirements EthOS installed (and may therefore remove again)."""
    return dict(_load_owned())


def has_binary(name: str) -> bool:
    """True when `name` exists as an executable on the host.

    Absolute candidates are checked first with ``os.access`` so a service
    account with a minimal PATH still finds /usr/sbin/smbd; ``shutil.which`` is
    the fallback for unusual install locations.
    """
    for directory in _SEARCH_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return shutil.which(name) is not None


def helper_available() -> bool:
    """True when the privileged OS-package helper is installed on this host."""
    return os.path.isfile(HELPER_PATH) and os.access(HELPER_PATH, os.X_OK)


def install_hint(requirement: str) -> str:
    """Operator-facing command that satisfies `requirement`, or '' if unknown."""
    meta = SYSTEM_REQUIREMENTS.get(requirement)
    if not meta:
        return ""
    return f"sudo apt install {meta['apt']}"


def probe_one(requirement: str) -> dict:
    """Probe a single requirement id. Unknown ids report ``ok: false``."""
    meta = SYSTEM_REQUIREMENTS.get(requirement)
    if not meta:
        return {
            "id": requirement,
            "label": requirement,
            "ok": False,
            "missing_tools": [],
            "hint": "",
            "error": "unknown system requirement",
        }
    missing = [name for name in meta["bins"] if not has_binary(name)]
    return {
        "id": requirement,
        "label": meta["label"],
        "ok": not missing,
        "missing_tools": missing,
        "hint": install_hint(requirement),
        "provisionable": bool(meta.get("provisionable")),
        "serviceable": bool(meta.get("serviceable")),
        "autoremove": bool(meta.get("autoremove")),
        "optional": bool(meta.get("optional")),
        "owned": requirement in _load_owned(),
    }


def probe(requires: Optional[Iterable[str]]) -> list[dict]:
    """Probe every requirement in a manifest's ``system_requires`` list."""
    seen: list[str] = []
    for item in requires or []:
        if isinstance(item, str) and item not in seen:
            seen.append(item)
    return [probe_one(item) for item in seen]


def missing(requires: Optional[Iterable[str]]) -> list[dict]:
    """Only the unsatisfied requirements (empty list means all good)."""
    return [item for item in probe(requires) if not item["ok"]]


def is_satisfied(requirement: str) -> bool:
    """Shortcut used by core features that need one host tool (e.g. Samba)."""
    return probe_one(requirement)["ok"]


def describe_missing(unsatisfied: list[dict], *, can_install: bool = True) -> str:
    """One-sentence explanation, safe for an API detail."""
    parts = []
    for item in unsatisfied:
        tools = ", ".join(item["missing_tools"]) or "required binaries"
        parts.append(f"{item['label']} (missing: {tools})")
    text = "This package requires host software that is not installed: " + ", ".join(parts) + "."
    if can_install:
        text += (
            " EthOS tried to install it automatically but could not — retry after running "
            "`sudo bash setup_scripts/setup_service.sh` on the NAS."
        )
        return text
    commands = "; ".join(item["hint"] for item in unsatisfied if item.get("hint"))
    if commands:
        text += (
            f" Install it on the NAS with: {commands} — or re-run "
            "`sudo bash setup_scripts/setup_service.sh` to enable automatic provisioning."
        )
    return text


# ── Privileged helper ───────────────────────────────────────────────────────

def _run_helper(action: str, requirement: str, job_id: Optional[str]) -> None:
    """Run ``ethos-system-deps <action> <id>`` through sudo (no password)."""
    from backend.services import package_jobs

    meta = SYSTEM_REQUIREMENTS[requirement]
    cmd = ["sudo", "-n", HELPER_PATH, action, requirement]
    package_jobs.log(job_id, f"{action} host requirement: {meta['label']} ({meta['apt']})")
    logger.info("Running privileged helper: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_HELPER_TIMEOUT)
    except FileNotFoundError:
        logger.error("sudo not found — cannot run %s", HELPER_PATH)
        raise RuntimeError("sudo is not available on this host") from None
    except subprocess.TimeoutExpired:
        logger.error("System dependency helper timed out: %s %s", action, requirement)
        raise RuntimeError(f"Timed out while running {action} {requirement}") from None

    for line in (proc.stdout or "").splitlines()[-30:]:
        package_jobs.log(job_id, line)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:] or f"exit {proc.returncode}"
        logger.error("System dependency helper failed (%s %s): %s", action, requirement, detail)
        raise RuntimeError(f"{action} {requirement} failed: {detail}")


def install(requirement: str, job_id: Optional[str] = None,
            package_id: Optional[str] = None) -> None:
    """Install one requirement through the helper and record the ownership."""
    from backend.services.package_schema import PackageError

    meta = SYSTEM_REQUIREMENTS.get(requirement)
    if not meta or not meta.get("serviceable"):
        raise PackageError(f"{requirement!r} cannot be installed by EthOS", 400)
    if not helper_available():
        raise PackageError(describe_missing([probe_one(requirement)], can_install=False), 503)
    _run_helper("install", requirement, job_id)
    if not probe_one(requirement)["ok"]:
        raise PackageError(
            f"{meta['label']} was installed but its tools are still missing — "
            "check `journalctl -u ethos-nas` and `apt-get -f install`.", 500,
        )
    owned = _load_owned()
    owned[requirement] = {
        "apt": meta["apt"],
        "package_id": package_id or "",
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_owned(owned)
    logger.info("EthOS installed host requirement %s for %s", requirement, package_id or "?")


def remove(requirement: str, job_id: Optional[str] = None) -> bool:
    """Remove one requirement through the helper. True when it was removed."""
    from backend.services import package_jobs

    meta = SYSTEM_REQUIREMENTS.get(requirement)
    if not meta or not meta.get("serviceable") or not meta.get("autoremove"):
        return False
    if not helper_available():
        logger.warning("Helper missing — leaving %s installed", requirement)
        return False
    _run_helper("remove", requirement, job_id)
    owned = _load_owned()
    owned.pop(requirement, None)
    _save_owned(owned)
    package_jobs.log(job_id, f"removed host requirement: {meta['label']}")
    logger.info("EthOS removed host requirement %s (no package needs it)", requirement)
    return True


# ── Package lifecycle hooks ─────────────────────────────────────────────────

def ensure(requires: Optional[Iterable[str]], job_id: Optional[str] = None,
           package_id: Optional[str] = None) -> None:
    """Satisfy a manifest's host requirements before anything is deployed.

    ``docker`` is delegated to ``services/docker_host.py`` (privileged engine
    helper). Every other serviceable requirement is installed through
    ``ethos-system-deps`` and recorded as EthOS-owned, so ``release()`` can undo
    it when the last package that needs it goes away.

    Synchronous — call through ``asyncio.to_thread`` from async code.
    """
    from backend.services.package_schema import PackageError

    names = [item for item in (requires or []) if isinstance(item, str)]
    if not names:
        return

    for item in probe(names):
        if item["ok"] or item.get("provisionable"):
            continue
        if item.get("optional"):
            # Recommended extra (e.g. archive tools): try the helper, but never
            # turn a missing extractor into a failed package install/update.
            if item.get("serviceable") and helper_available():
                try:
                    install(item["id"], job_id, package_id)
                except Exception as exc:  # noqa: BLE001 — optional; keep installing
                    logger.warning("Optional host requirement %s not installed: %s",
                                   item["id"], exc)
            else:
                logger.info("Optional host requirement %s is missing (helper unavailable)",
                            item["id"])
            continue
        if item.get("serviceable") and helper_available():
            install(item["id"], job_id, package_id)
            continue
        raise PackageError(describe_missing([item], can_install=helper_available()), 503)

    if "docker" in names:
        # Docker also probes the daemon (not just the CLI), so always let it decide.
        from backend.services import docker_host  # local: docker_host is heavy

        docker_host.ensure_system_deps(["docker"], job_id)


def required_by_others(requirement: str, exclude_package: str) -> list[str]:
    """Installed packages (other than `exclude_package`) that declare `requirement`."""
    from backend.services import packages_store

    owners = []
    for record in packages_store.load_installed():
        pkg_id = str(record.get("id") or "")
        if not pkg_id or pkg_id == exclude_package:
            continue
        declared = record.get("system_requires")
        if declared is None:
            declared = (record.get("manifest") or {}).get("system_requires") or []
        if requirement in (declared or []):
            owners.append(pkg_id)
    return owners


def release(package_id: str, job_id: Optional[str] = None) -> list[str]:
    """Drop host requirements the uninstalled package no longer needs.

    Only requirements EthOS installed itself (``owned_requirements``) and that
    no remaining installed package declares are removed. Returns the removed ids.

    Synchronous — call through ``asyncio.to_thread`` from async code.
    """
    from backend.services import package_jobs, packages_store
    from backend.services.notifications_service import emit_event

    record = packages_store.get_package(package_id) or {}
    declared = record.get("system_requires")
    if declared is None:
        declared = (record.get("manifest") or {}).get("system_requires") or []
    candidates = [item for item in (declared or []) if isinstance(item, str)]
    if not candidates:
        return []

    owned = _load_owned()
    removed: list[str] = []
    for requirement in candidates:
        meta = SYSTEM_REQUIREMENTS.get(requirement) or {}
        label = meta.get("label", requirement)
        if requirement not in owned:
            package_jobs.log(job_id, f"keeping {label} — not installed by EthOS")
            continue
        if not meta.get("autoremove"):
            package_jobs.log(job_id, f"keeping {label} — shared infrastructure")
            continue
        owners = required_by_others(requirement, exclude_package=package_id)
        if owners:
            package_jobs.log(job_id, f"keeping {label} — still required by {', '.join(owners)}")
            continue
        try:
            if remove(requirement, job_id):
                removed.append(requirement)
                emit_event("info", f"Removed {label}",
                           "No installed package needs it any more", source="packages")
        except RuntimeError as exc:
            # Never fail an uninstall because host cleanup failed.
            logger.error("Could not remove host requirement %s: %s", requirement, exc)
            package_jobs.log(job_id, f"could not remove {requirement}: {exc}")
    return removed
