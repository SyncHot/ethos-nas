"""EthOS NAS — USB Mount Service

Persistent mount/unmount of USB storage partitions from the Storage app.

Design:
- "Mount"  = create a stable mount point under /mnt, add an fstab entry with
  `defaults,nofail`, and mount it now via `mount <mp>` (resolved through
  fstab, so runtime behaviour is identical to boot-time behaviour).
- "Unmount" = unmount the partition and remove every fstab line that
  references it, so it will not come back on reboot.

Mount-point stability (no folder churn under /mnt):
- The last mount point used for each partition is remembered in a JSON file
  (<DATA_DIR>/usb_mounts.json) keyed by PARTUUID (or fs UUID), so re-mounting
  — even after the kernel renames the device sdb→sdc on re-plug — reuses the
  same path instead of creating /mnt/xxx_2, _3, …
- The Mount action accepts an explicit folder name (e.g. "Backups") which
  becomes the stable path from then on.
- Unmount waits for lazy-unmounted entries to drain and removes the now-empty
  directory; dying `shutdown` mounts in /proc/mounts never block reuse.
- Boot safety: entries always carry `nofail`, so systemd only *wants* (never
  *requires*) the mount unit — if the drive is disconnected at boot time the
  system still boots normally.

fstab layout: our entries live in a clearly marked block appended to
/etc/fstab:

    # >>> EthOS NAS USB mounts (managed by the Storage app)
    PARTUUID=... /mnt/Label ext4 defaults,nofail 0 2
    # <<< EthOS NAS USB mounts

The block is rewritten atomically on every change. Unmount additionally
removes pre-existing (manually added) lines that reference the same
partition, so "unmount" is always permanent.

Privileges: works when the server runs as root; for unprivileged dev mode it
falls back to `sudo -n` — see setup_scripts/setup_service.sh.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger("ethos-nas.services.usb_mount_service")

FSTAB_PATH = "/etc/fstab"
MOUNT_ROOT = "/mnt"
# Privileged helper installed by setup_scripts/setup_service.sh — re-validates
# USB transport before exec'ing the requested command as root. One wildcard-free
# sudoers rule covers every device-scoped operation (classic sudo and sudo-rs).
WRAPPER = "/usr/local/bin/ethos-usb-partition"
STATE_PATH: Optional[str] = None  # override for tests (default <DATA_DIR>/usb_mounts.json)
MANAGED_BEGIN = "# >>> EthOS NAS USB mounts (managed by the Storage app)"
MANAGED_END = "# <<< EthOS NAS USB mounts"

# Filesystems the UI offers to mount. Everything else (LVM2_member, swap,
# crypto_LUKS, ...) is shown but not actionable.
MOUNTABLE_FSTYPES = {
    "ext2", "ext3", "ext4", "xfs", "btrfs",
    "vfat", "exfat", "ntfs", "ntfs3",
}

# fsck pass number in fstab (field 6)
_FSCK_PASS = {"ext2": 2, "ext3": 2, "ext4": 2}

_DEV_RE = re.compile(
    r"^/dev/(sd[a-z]+\d*|vd[a-z]+\d*|xvd[a-z]+\d*"
    r"|nvme\d+n\d+(p\d+)?|mmcblk\d+(p\d+)?)$"
)


class UsbMountError(Exception):
    """User-facing error (message is safe to show in the UI)."""


# ── Privileged execution ────────────────────────────────────────────────────

def _run_privileged(cmd: list[str], timeout: int = 30, input_text: Optional[str] = None,
                    device: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command that needs root; as non-root go straight to `sudo -n`.

    A direct attempt is only made when already running as root — otherwise the
    binary would run and exit with EPERM ("must be superuser") without ever
    reaching the sudo fallback. Unprivileged:
      - with `device`: via the ethos-usb-partition helper, which re-validates
        USB transport before exec'ing as root (one wildcard-free rule);
      - otherwise: directly (`sudo -n <cmd>`), matching the specific rules in
        setup_scripts/setup_service.sh.

    Raises UsbMountError with a clear hint when sudo is missing or not allowed.
    """
    if os.geteuid() == 0:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)

    if device is not None and not os.path.exists(WRAPPER):
        raise UsbMountError(
            f"`{' '.join(cmd)}` needs root. Run setup_scripts/setup_service.sh "
            "as root to install the USB helper and NOPASSWD rules."
        )
    full_cmd = (["sudo", "-n", WRAPPER, device, "--"] + cmd) if device is not None else ["sudo", "-n"] + cmd

    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
        )
    except FileNotFoundError as exc:
        raise UsbMountError(
            "Permission denied and `sudo` is not available. Run "
            "setup_scripts/setup_service.sh as root."
        ) from exc
    if result.returncode != 0:
        err = (result.stderr or "").lower()
        if "password" in err or "not allowed" in err or "may not run sudo" in err:
            raise UsbMountError(
                f"`{cmd[0]}` requires passwordless sudo. Run setup_scripts/setup_service.sh "
                "as root for NOPASSWD access."
            )
    return result


def _run_checked(cmd: list[str], timeout: int = 30, input_text: Optional[str] = None,
                 device: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a privileged command and raise UsbMountError on non-zero exit."""
    result = _run_privileged(cmd, timeout=timeout, input_text=input_text, device=device)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise UsbMountError(f"{' '.join(cmd)} failed: {detail}" if detail else f"{' '.join(cmd)} failed")
    return result


# ── Device discovery ────────────────────────────────────────────────────────

def _lsblk_tree() -> list[dict]:
    """Full lsblk JSON tree (disks + partitions) with transport info."""
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,SIZE,TYPE,TRAN,MODEL,FSTYPE,PARTUUID,UUID,LABEL,MOUNTPOINT"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise UsbMountError(f"lsblk failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise UsbMountError(f"Could not read block devices: {exc}") from exc
    return data.get("blockdevices", [])


def _find_node(nodes: list[dict], name: str) -> Optional[tuple[dict, Optional[dict]]]:
    """Find a node by kernel name; returns (node, parent_disk_or_None)."""
    for node in nodes:
        if node.get("name") == name:
            return node, None
        found = _find_node(node.get("children", []) or [], name)
        if found and found[0] is not None:
            child, _ = found
            # parent disk is the top-level ancestor with type "disk"
            return child, node if node.get("type") == "disk" else None
    return None, None


def _resolve_device(device: str) -> dict:
    """Validate a device path and resolve it to a USB partition descriptor.

    Returns a dict with: device, name, label, size_bytes, fstype, partuuid,
    uuid, mountpoint (current or ""), is_usb.
    Raises UsbMountError for anything that is not a real USB storage unit.
    """
    dev = os.path.normpath(device) if device else ""
    if not _DEV_RE.match(dev):
        raise UsbMountError(f"Invalid device name: {device!r}")

    short = dev.rsplit("/", 1)[-1]
    tree = _lsblk_tree()
    node, parent = _find_node(tree, short)
    if node is None:
        raise UsbMountError(f"Device not found: {dev}")

    # The transport of the whole disk decides whether this is USB storage.
    tran = (parent or node).get("tran") or ""
    if tran != "usb":
        raise UsbMountError(f"{dev} is not a USB device (transport: {tran or 'unknown'})")

    fstype = (node.get("fstype") or "").strip()
    mountpoint = (node.get("mountpoint") or "").strip()
    return {
        "device": dev,
        "name": short,
        "label": (node.get("label") or "").strip(),
        "size_bytes": int(node.get("size", 0) or 0),
        "fstype": fstype,
        "partuuid": (node.get("partuuid") or "").strip(),
        "uuid": (node.get("uuid") or "").strip(),
        "mountpoint": mountpoint,
    }


def list_usb_devices() -> list[dict]:
    """USB disks with their mountable units (partitions, or the whole disk).

    Each unit carries everything the UI needs: device path, label, size,
    fstype, stable identifiers, current mount point and whether a managed
    fstab entry exists for it.
    """
    tree = _lsblk_tree()
    managed_lines = _managed_entries()
    memory = _load_memory()  # remembered mount points (stable paths)
    devices = []
    for disk in tree:
        if (disk.get("tran") or "") != "usb" or disk.get("type") != "disk":
            continue

        children = [c for c in (disk.get("children") or []) if c.get("type") == "part"]
        # Whole-disk filesystems (no partition table) are mountable too.
        units = children if children else ([disk] if disk.get("fstype") else [])

        partitions = []
        for unit in units:
            name = unit.get("name", "")
            fstype = (unit.get("fstype") or "").strip()
            partuuid = (unit.get("partuuid") or "").strip()
            uuid = (unit.get("uuid") or "").strip()
            mountpoint = (unit.get("mountpoint") or "").strip()
            partitions.append({
                "device": f"/dev/{name}",
                "name": name,
                "label": (unit.get("label") or "").strip(),
                "size_bytes": int(unit.get("size", 0) or 0),
                "fstype": fstype,
                "mountable": fstype in MOUNTABLE_FSTYPES,
                "partuuid": partuuid,
                "uuid": uuid,
                "mounted": bool(mountpoint),
                "mountpoint": mountpoint,
                "last_mountpoint": memory.get(_unit_key(unit), ""),
                "managed": any(
                    _line_matches_device(line, partuuid, uuid, f"/dev/{name}", mountpoint)
                    for line in managed_lines
                ),
            })

        devices.append({
            "device": f"/dev/{disk.get('name', '')}",
            "name": disk.get("name", ""),
            "model": (disk.get("model") or "").strip(),
            "label": (disk.get("label") or "").strip(),
            "size_bytes": int(disk.get("size", 0) or 0),
            "partitions": partitions,
        })
    return devices


# ── fstab management ────────────────────────────────────────────────────────

def _read_fstab() -> list[str]:
    try:
        with open(FSTAB_PATH) as f:
            return f.read().splitlines()
    except PermissionError:
        result = subprocess.run(
            ["cat", FSTAB_PATH], capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise UsbMountError(f"Cannot read {FSTAB_PATH}: {result.stderr.strip()}")
        return result.stdout.splitlines()


def _write_fstab(lines: list[str]) -> None:
    """Atomically replace /etc/fstab (direct write, or `sudo tee` fallback)."""
    content = "\n".join(lines) + "\n"
    try:
        tmp = FSTAB_PATH + ".ethos-tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, FSTAB_PATH)
        logger.info("fstab updated (direct write)")
    except PermissionError:
        result = _run_privileged(["tee", FSTAB_PATH], input_text=content)
        if result.returncode != 0:
            raise UsbMountError(
                f"Cannot write {FSTAB_PATH}: {(result.stderr or '').strip()}. "
                "Run setup_scripts/setup_service.sh as root to install the USB helper and NOPASSWD rules."
            )
        logger.info("fstab updated (via sudo tee)")


def _managed_entries(lines: Optional[list[str]] = None) -> list[str]:
    """Return the fstab entry lines inside our managed block."""
    if lines is None:
        lines = _read_fstab()
    entries: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            in_block = True
            continue
        if stripped == MANAGED_END:
            in_block = False
            continue
        if in_block and stripped and not stripped.startswith("#"):
            entries.append(stripped)
    return entries


def _split_managed(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split fstab lines into (lines_without_block, managed_entries)."""
    kept: list[str] = []
    entries: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == MANAGED_BEGIN:
            in_block = True
            continue
        if stripped == MANAGED_END:
            in_block = False
            continue
        if in_block:
            if stripped and not stripped.startswith("#"):
                entries.append(stripped)
            continue
        kept.append(line)
    return kept, entries


def _render_fstab(kept: list[str], entries: list[str]) -> list[str]:
    """Rebuild fstab lines with the managed block appended at the end."""
    out = [line for line in kept]
    # Drop trailing blank lines so the block sits cleanly at EOF.
    while out and not out[-1].strip():
        out.pop()
    if entries:
        out.append(MANAGED_BEGIN)
        out.extend(entries)
        out.append(MANAGED_END)
    return out


def _line_matches_device(
    line: str, partuuid: str, uuid: str, devpath: str, mountpoint: str,
    unscoped_uuid: bool = False,
) -> bool:
    """True if an fstab line references this partition.

    PARTUUID matches are unique per partition and safe on their own. UUID/
    device-path matches are normally scoped to the known mount point so we
    never touch entries for other partitions that happen to share a
    filesystem UUID; with ``unscoped_uuid=True`` (mount flow, only used when
    the partition has a PARTUUID) they match at any mount point — this is
    what prevents duplicate boot-time mounts of the same partition.
    """
    fields = line.split()
    if len(fields) < 2:
        return False
    what, where = fields[0], fields[1]
    # UUIDs are case-insensitive; fstab lines may use any casing.
    what_l = what.lower()
    if partuuid and what_l == f"partuuid={partuuid.lower()}":
        return True
    if uuid and what_l == f"uuid={uuid.lower()}":
        if unscoped_uuid:
            return True
        if mountpoint and where == mountpoint:
            return True
    if mountpoint and where == mountpoint and what == devpath:
        return True
    return False


def _fstab_what(info: dict) -> str:
    """Stable identifier for the fstab `what` field."""
    if info["partuuid"]:
        return f"PARTUUID={info['partuuid']}"
    if info["uuid"]:
        return f"UUID={info['uuid']}"
    return info["device"]


def _current_mount_points(include_dying: bool = True) -> set[str]:
    """Mount points currently in /proc/mounts.

    Lazy-unmounted filesystems linger with the `shutdown` option until their
    last file handle closes; with include_dying=False those dying entries are
    excluded so they never block reusing a path.
    """
    out: set[str] = set()
    try:
        with open("/proc/mounts") as f:
            for row in f.read().splitlines():
                parts = row.split()
                if len(parts) < 2:
                    continue
                opts = parts[2].split(",") if len(parts) > 2 else []
                if not include_dying and "shutdown" in opts:
                    continue
                out.add(parts[1])
    except OSError:
        pass  # intentional: a failure here must not break the caller
    return out

# ── Mount-point memory (stable paths across mount/unmount cycles) ───────────

_MEMORY_LOCK = threading.Lock()

def _state_file() -> str:
    if STATE_PATH:
        return STATE_PATH
    from backend.config import settings
    return os.path.join(str(settings.DATA_DIR), "usb_mounts.json")

def _load_memory() -> dict[str, str]:
    """{PARTUUID=…|UUID=…: mountpoint} — missing/corrupt file → empty."""
    try:
        with open(_state_file()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except FileNotFoundError:
        pass  # unreadable/missing file is treated as absent
    except Exception as exc:  # noqa: BLE001 — corrupt memory must not break mounts
        logger.warning("usb mount memory unreadable (%s) — starting fresh", exc)
    return {}

def _save_memory(mem: dict[str, str]) -> None:
    try:
        path = _state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(mem, f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 — memory is best-effort
        logger.warning("could not persist usb mount memory: %s", exc)

def _unit_key(unit: dict) -> str:
    """Stable identity for mount-point memory: PARTUUID, else fs UUID."""
    partuuid = (unit.get("partuuid") or "").strip()
    if partuuid:
        return f"PARTUUID={partuuid}"
    uuid_ = (unit.get("uuid") or "").strip()
    return f"UUID={uuid_}" if uuid_ else ""

def _remember(key: str, mountpoint: str) -> None:
    if not key or not mountpoint:
        return
    with _MEMORY_LOCK:
        mem = _load_memory()
        if len(mem) > 1000:  # pathological growth guard
            mem.clear()
        mem[key] = mountpoint
        _save_memory(mem)

def forget_mountpoint(key: str) -> None:
    """Drop the remembered path for a partition (after format/delete/init)."""
    if not key:
        return
    with _MEMORY_LOCK:
        mem = _load_memory()
        if key in mem:
            del mem[key]
            _save_memory(mem)


def _sanitize_folder(folder: str) -> str:
    """Turn user input into a safe single folder name (no paths, no junk)."""
    name = os.path.basename((folder or "").strip().strip("/"))
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    if not name:
        raise UsbMountError(f"Invalid mount folder name: {folder!r}")
    return name

def _mount_point_for(info: dict, preferred: Optional[str] = None,
                     folder: Optional[str] = None) -> str:
    """Derive a stable, unique mount point under /mnt for this partition.

    Preference order: an explicit `preferred` full path (the remembered one),
    then a user-chosen `folder` name, then the volume label or device name.
    A candidate is usable when it is not currently mounted and either does
    not exist yet or exists as an empty directory (we never hide files by
    mounting over a non-empty directory).
    """
    taken = _current_mount_points(include_dying=False)

    def usable(mp: str) -> bool:
        if mp in taken:
            return False
        if os.path.exists(mp):
            try:
                return len(os.listdir(mp)) == 0
            except OSError:
                return False
        return True

    if preferred and usable(preferred):
        return preferred

    base = _sanitize_folder(folder) if folder else \
        re.sub(r"[^A-Za-z0-9._-]", "_", info["label"]).strip("._") or f"usb_{info['name']}"

    candidate = os.path.join(MOUNT_ROOT, base)
    if usable(candidate):
        return candidate
    for n in range(2, 100):
        alt = f"{candidate}_{n}"
        if usable(alt):
            return alt
    raise UsbMountError(f"Could not find a free mount point under {MOUNT_ROOT}")


def _ensure_mount_point(mp: str, device: Optional[str] = None) -> None:
    """Create the mount point directory (idempotent)."""
    if os.path.isdir(mp):
        return
    result = _run_privileged(["mkdir", "-p", mp], device=device)
    if result.returncode != 0:
        raise UsbMountError(f"Cannot create {mp}: {(result.stderr or '').strip()}")
    try:
        os.chmod(mp, 0o755)
    except OSError:
        pass  # best-effort cleanup — nothing to report


def _fstab_entry(info: dict, mountpoint: str) -> str:
    fstype = info["fstype"]
    passno = _FSCK_PASS.get(fstype, 0)
    return f"{_fstab_what(info)} {mountpoint} {fstype} defaults,nofail 0 {passno}"


# ── Public operations ───────────────────────────────────────────────────────

def mount_usb(device: str, folder: Optional[str] = None) -> dict:
    """Mount a USB partition persistently (fstab entry with nofail).

    The path is stable across cycles: the last mount point used for this
    partition (keyed by PARTUUID/UUID) is reused when free; an explicit
    `folder` name overrides it and becomes the remembered path.
    """
    info = _resolve_device(device)
    if not info["fstype"]:
        raise UsbMountError(f"{info['device']} has no filesystem")
    if info["fstype"] not in MOUNTABLE_FSTYPES:
        raise UsbMountError(
            f"Filesystem '{info['fstype']}' on {info['device']} is not supported "
            f"(supported: {', '.join(sorted(MOUNTABLE_FSTYPES))})"
        )

    key = _unit_key(info)
    remembered = _load_memory().get(key, "") if key else ""

    mountpoint = info["mountpoint"] or _mount_point_for(
        info, preferred=remembered or None, folder=folder,
    )

    # Upsert the managed fstab entry (dedupe any line referencing this unit).
    # When the partition has a PARTUUID we also drop pre-existing lines that
    # reference its filesystem UUID at *any* mount point — leaving them would
    # create duplicate boot-time mounts of the same partition, and a manual
    # line without nofail could block boot.
    unscoped = bool(info["partuuid"])
    lines = _read_fstab()
    kept, entries = _split_managed(lines)
    entries = [
        e for e in entries
        if not _line_matches_device(e, info["partuuid"], info["uuid"], info["device"], mountpoint, unscoped_uuid=unscoped)
    ]
    kept = [
        l for l in kept
        if not (l.strip() and not l.strip().startswith("#")
                and _line_matches_device(l.strip(), info["partuuid"], info["uuid"], info["device"], mountpoint, unscoped_uuid=unscoped))
    ]
    entries.append(_fstab_entry(info, mountpoint))
    _write_fstab(_render_fstab(kept, entries))

    if not info["mountpoint"]:
        _ensure_mount_point(mountpoint, info["device"])
        # Mount explicitly (device + options) rather than via fstab name lookup.
        # Same runtime behaviour as the fstab line (`defaults`; nofail only
        # matters at boot), but it doesn't depend on mount(8) resolving the
        # entry we just wrote — and keeps working when FSTAB_PATH is redirected
        # (tests).
        # Non-root this goes through the USB helper (sudoers rules cannot use
        # argument wildcards under sudo-rs, so the two-arg form is not directly
        # grantable); as root it runs exactly as written.
        result = _run_checked(["mount", "-o", "defaults", info["device"], mountpoint], device=info["device"])
        logger.info("Mounted %s at %s (persistent)", info["device"], mountpoint)

    # Remember the path (also for already-mounted/boot-time mounts) so future
    # cycles reuse it instead of creating new numbered folders.
    _remember(key, mountpoint)

    return {
        "ok": True,
        "device": info["device"],
        "mountpoint": mountpoint,
        "fstype": info["fstype"],
        "already_mounted": bool(info["mountpoint"]),
        "persistent": True,
    }


def unmount_usb(device: str) -> dict:
    """Unmount a USB partition and remove its fstab entries (permanent)."""
    info = _resolve_device(device)
    mountpoint = info["mountpoint"]
    lazy = False

    if mountpoint:
        result = _run_privileged(["umount", mountpoint], device=info["device"])
        if result.returncode != 0:
            # Retry lazily (defers the actual unmount until no process uses it).
            result = _run_checked(["umount", "-l", mountpoint], device=info["device"])
            lazy = True
        # Wait for the entry to actually leave /proc/mounts (lazy unmounts
        # linger until the last file handle closes) so an immediate remount
        # reuses the same path instead of picking a new numbered one.
        deadline = time.time() + 5
        while mountpoint in _current_mount_points() and time.time() < deadline:
            time.sleep(0.2)
        logger.info("Unmounted %s from %s%s", info["device"], mountpoint, " (lazy)" if lazy else "")

    # Remove every fstab line referencing this partition.
    lines = _read_fstab()
    kept, entries = _split_managed(lines)
    before = len(entries) + sum(1 for l in kept if l.strip() and not l.strip().startswith("#"))
    entries = [
        e for e in entries
        if not _line_matches_device(e, info["partuuid"], info["uuid"], info["device"], mountpoint)
    ]
    kept = [
        l for l in kept
        if not (l.strip() and not l.strip().startswith("#")
                and _line_matches_device(l.strip(), info["partuuid"], info["uuid"], info["device"], mountpoint))
    ]
    after = len(entries) + sum(1 for l in kept if l.strip() and not l.strip().startswith("#"))
    removed = before - after

    if removed:
        _write_fstab(_render_fstab(kept, entries))

    # Best-effort cleanup of the now-empty directory (never touches data), so
    # /mnt does not accumulate stale folders; it is recreated on next mount.
    if mountpoint and os.path.isdir(mountpoint):
        try:
            if not os.listdir(mountpoint):
                result = _run_privileged(["rmdir", mountpoint], device=info["device"])
                if result.returncode == 0:
                    logger.info("Removed empty mount point %s", mountpoint)
        except OSError:
            pass  # intentional: a failure here must not break the caller

    return {
        "ok": True,
        "device": info["device"],
        "mountpoint": mountpoint,
        "was_mounted": bool(mountpoint),
        "lazy_unmount": lazy,
        "fstab_entries_removed": removed,
    }
