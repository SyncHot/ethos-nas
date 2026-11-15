"""EthOS NAS — USB Partition Service

Management of USB storage from the Storage app:

- format_usb()          — reformat a partition (or whole disk) with a chosen
                          filesystem + label. Auto-unmounts first if needed.
- relabel_usb()         — change the volume label of an existing partition
                          without reformatting (no data loss).
- create_partition()    — add a new partition in the trailing free space of a
                          USB disk, format it, and optionally auto-mount it.
- delete_partition()    — unmount + remove fstab entries, then delete the
                          partition from the table.
- initialize_disk()     — wipe every partition on a USB disk (GPT re-init)
                          and recreate one full-size volume ("Initialize",
                          like a typical NAS storage manager).

Safety model:
- Every operation resolves the device through usb_mount_service._resolve_device,
  which rejects anything that is not a real USB-transport storage unit.
- Formatting/initializing always unmounts affected units first; fstab entries
  for wiped partitions are removed so stale boot-time mounts never happen.
- Privileges: direct when the server runs as root, `sudo -n` fallback otherwise
  (see setup_scripts/setup_service.sh).
"""

import logging
import math
import os
import re
import shutil
import subprocess
import time
from typing import Optional

from backend.services import usb_mount_service
from backend.services.usb_mount_service import UsbMountError

logger = logging.getLogger("ethos-nas.services.usb_partition_service")

# Single source of truth lives in usb_mount_service (shared with its sudo path).
WRAPPER = usb_mount_service.WRAPPER

def _run_dev_cmd(device: str, cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a privileged command that operates on `device`.

    As root the command runs directly. Unprivileged it goes through the
    ethos-usb-partition helper (installed by setup_scripts/setup_service.sh),
    which re-validates USB transport before exec'ing as root — one wildcard-free
    sudoers rule that works on both classic sudo and sudo-rs.
    """
    if os.geteuid() == 0:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if not os.path.exists(WRAPPER):
        raise UsbMountError(
            "Running unprivileged without the USB partition helper. Run "
            "setup_scripts/setup_service.sh as root to install it."
        )
    return subprocess.run(
        ["sudo", "-n", WRAPPER, device, "--"] + cmd,
        capture_output=True, text=True, timeout=timeout,
    )

def _run_dev_checked(device: str, cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    result = _run_dev_cmd(device, cmd, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise UsbMountError(
            f"{' '.join(cmd)} failed: {detail}" if detail else f"{' '.join(cmd)} failed"
        )
    return result

# fstype -> (mkfs argv builder, max label length)
# Builders receive (device, label) and return the full command list.
def _mkfs_ext4(dev: str, label: str) -> list[str]:
    cmd = ["mkfs.ext4", "-F"]
    if label:
        cmd += ["-L", label]
    cmd.append(dev)
    return cmd

def _mkfs_xfs(dev: str, label: str) -> list[str]:
    # xfsprogs ships the maker as mkfs.xfs (xfs_mkfs is not installed).
    cmd = ["mkfs.xfs", "-f"]
    if label:
        cmd += ["-L", label]
    cmd.append(dev)
    return cmd

def _mkfs_btrfs(dev: str, label: str) -> list[str]:
    cmd = ["btrfs", "mkfs"]
    if label:
        cmd += ["-L", label]
    cmd.append(dev)
    return cmd

def _mkfs_vfat(dev: str, label: str) -> list[str]:
    # FAT labels are 11 chars max and conventionally uppercase.
    return ["mkfs.vfat", "-n", (label or "USB").upper()[:11], dev]

def _mkfs_exfat(dev: str, label: str) -> list[str]:
    return ["mkfs.exfat", "-n", (label or "USB")[:15], dev]

def _mkfs_ntfs(dev: str, label: str) -> list[str]:
    cmd = ["mkntfs", "-f"]
    if label:
        cmd += ["-L", label]
    cmd.append(dev)
    return cmd

# Filesystem hints parted accepts in `mkpart <name> <fstype>` — anything else
# (exfat) is simply omitted from the command.
PARTED_FS_HINTS: dict[str, str] = {
    "ext4": "ext4",
    "xfs": "xfs",
    "btrfs": "btrfs",
    "vfat": "fat32",
    "ntfs": "ntfs",
}

FILESYSTEMS: dict[str, dict] = {
    "ext4":  {"builder": _mkfs_ext4,  "max_label": 16, "tool": "mkfs.ext4"},
    "xfs":   {"builder": _mkfs_xfs,   "max_label": 12, "tool": "mkfs.xfs"},
    "btrfs": {"builder": _mkfs_btrfs, "max_label": 12, "tool": "btrfs"},
    "vfat":  {"builder": _mkfs_vfat,  "max_label": 11, "tool": "mkfs.vfat", "display": "FAT32"},
    "exfat": {"builder": _mkfs_exfat, "max_label": 15, "tool": "mkfs.exfat"},
    "ntfs":  {"builder": _mkfs_ntfs,  "max_label": 12, "tool": "mkntfs"},
}

_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]*$")


def supported_filesystems() -> list[dict]:
    """Filesystems this host can actually create (mkfs tool present)."""
    out = []
    for name, spec in FILESYSTEMS.items():
        available = shutil.which(spec["tool"]) is not None
        if available:
            out.append({
                "name": spec.get("display", name),
                "value": name,
                "max_label": spec["max_label"],
            })
    return out


def _sanitize_label(label: str, max_len: int) -> str:
    label = (label or "").strip()
    if not _LABEL_RE.match(label):
        raise UsbMountError(
            f"Invalid label {label!r}: use letters, digits, dot, dash only."
        )
    return label[:max_len]


def _mkfs(device: str, fstype: str, label: str) -> None:
    """Create a filesystem on device (must be unmounted)."""
    spec = FILESYSTEMS.get(fstype)
    if spec is None:
        raise UsbMountError(
            f"Unsupported filesystem {fstype!r} "
            f"(supported: {', '.join(FILESYSTEMS)})"
        )
    tool = shutil.which(spec["tool"])
    if tool is None:
        raise UsbMountError(
            f"{spec['tool']} is not installed on this host — cannot create "
            f"a {fstype} filesystem."
        )
    label = _sanitize_label(label, spec["max_label"])

    # Clear stale signatures first (best effort).
    if shutil.which("wipefs"):
        try:
            _run_dev_cmd(device, ["wipefs", "-a", device], timeout=30)
        except Exception:  # noqa: BLE001 — mkfs below is the real gate
            pass

    cmd = spec["builder"](device, label)
    logger.info("Formatting %s as %s (label=%r)", device, fstype, label)
    # mkfs on large disks can take a while; ext4/xfs are fast with lazy init.
    _run_dev_checked(device, cmd, timeout=900)
    _wait_fstype_visible(device, fstype)


def _wait_fstype_visible(device: str, expected: str, timeout: float = 15.0) -> None:
    """Wait until lsblk reports the new filesystem type on `device`.

    Right after mkfs, libblkid/lsblk can lag (notably on loop devices), and a
    mount attempted before FSTYPE is visible fails with 'has no filesystem'.
    """
    deadline = time.time() + timeout
    name = device.rsplit("/", 1)[-1]
    while True:
        try:
            tree = usb_mount_service._lsblk_tree()
            node, _parent = usb_mount_service._find_node(tree, name)
            if node and (node.get("fstype") or "").lower() == expected.lower():
                return
        except Exception:  # noqa: BLE001 — transient lsblk failure, retry
            pass
        if time.time() >= deadline:
            raise UsbMountError(
                f"{device}: filesystem {expected} not visible to lsblk after mkfs"
            )
        time.sleep(0.25)


def _disk_children(tree: list[dict], disk_name: str) -> tuple[Optional[dict], list[dict]]:
    """Return (disk_node, partition_nodes_in_order) for a USB disk."""
    node, parent = usb_mount_service._find_node(tree, disk_name)
    if node is None or node.get("type") != "disk":
        raise UsbMountError(f"Disk not found: /dev/{disk_name}")
    parts = [c for c in (node.get("children") or []) if c.get("type") == "part"]
    return node, parts


def _unmount_node(node: dict, device: Optional[str] = None) -> bool:
    """Unmount a mounted lsblk node. Returns True if it was mounted."""
    mp = (node.get("mountpoint") or "").strip()
    if not mp:
        return False
    result = usb_mount_service._run_privileged(["umount", mp], device=device)
    if result.returncode != 0:
        usb_mount_service._run_checked(["umount", "-l", mp], device=device)
        logger.info("Lazy-unmounted %s (%s)", node.get("name"), mp)
    else:
        logger.info("Unmounted %s (%s)", node.get("name"), mp)
    # Wait for the entry to actually leave /proc/mounts (lazy unmounts linger
    # until the last file handle closes) so mkfs/parted never race a live mount.
    deadline = time.time() + 5
    while mp in usb_mount_service._current_mount_points() and time.time() < deadline:
        time.sleep(0.2)
    # Best-effort cleanup of the now-empty directory (never touches data), so
    # /mnt does not accumulate stale folders between mount/unmount cycles.
    if os.path.isdir(mp):
        try:
            if not os.listdir(mp):
                r = usb_mount_service._run_privileged(["rmdir", mp], device=device)
                if r.returncode == 0:
                    logger.info("Removed empty mount point %s", mp)
        except OSError:
            pass  # intentional: a failure here must not break the caller
    return True


def _remove_fstab_entries_for(node: dict, lines: list[str]) -> tuple[list[str], int]:
    """Remove managed + manual fstab lines referencing this lsblk node."""
    partuuid = (node.get("partuuid") or "").strip()
    uuid = (node.get("uuid") or "").strip()
    devpath = f"/dev/{node.get('name', '')}"
    mountpoint = (node.get("mountpoint") or "").strip()

    kept, entries = usb_mount_service._split_managed(lines)
    before = len(entries) + sum(1 for l in kept if l.strip() and not l.strip().startswith("#"))
    entries = [
        e for e in entries
        if not usb_mount_service._line_matches_device(e, partuuid, uuid, devpath, mountpoint)
    ]
    kept = [
        l for l in kept
        if not (l.strip() and not l.strip().startswith("#")
                and usb_mount_service._line_matches_device(l.strip(), partuuid, uuid, devpath, mountpoint))
    ]
    after = len(entries) + sum(1 for l in kept if l.strip() and not l.strip().startswith("#"))
    removed = before - after
    return usb_mount_service._render_fstab(kept, entries), removed


def _clean_fstab_for(node: dict) -> int:
    """Drop managed + manual fstab entries referencing this node. Returns count."""
    lines = usb_mount_service._read_fstab()
    rendered, removed = _remove_fstab_entries_for(node, lines)
    if removed:
        usb_mount_service._write_fstab(rendered)
    return removed


def format_usb(device: str, fstype: str, label: str = "") -> dict:
    """Format a USB partition (or whole-disk unit) with the given filesystem.

    If the volume was mounted it is unmounted, reformatted and
    remounted — its persistent fstab entry survives because PARTUUID does not
    change on reformat. Whole-disk formats destroy any stale entries instead.
    """
    info = usb_mount_service._resolve_device(device)

    tree = usb_mount_service._lsblk_tree()
    node, _parent = usb_mount_service._find_node(tree, info["name"])
    if node is None:
        raise UsbMountError(f"Device not found: {device}")

    has_parts = any(c.get("type") == "part" for c in (node.get("children") or []))
    if node.get("type") == "disk" and has_parts:
        raise UsbMountError(
            f"{device} contains partitions — format a partition individually, "
            "or use Initialize to wipe the whole disk."
        )

    was_mounted = bool(info["mountpoint"])
    _unmount_node(node, info["device"])

    _mkfs(device, fstype, label)

    # The filesystem identity changed → drop the remembered path so the next
    # mount is derived from the new label instead of reusing the old folder.
    usb_mount_service.forget_mountpoint(usb_mount_service._unit_key(node))

    mountpoint = ""
    if was_mounted:
        # The old fstab line may reference the pre-format UUID or filesystem
        # type — drop it and let mount_usb recreate a correct one.
        lines = usb_mount_service._read_fstab()
        rendered, removed = _remove_fstab_entries_for(node, lines)
        if removed:
            usb_mount_service._write_fstab(rendered)
        result = usb_mount_service.mount_usb(device)
        mountpoint = result.get("mountpoint", "")

    logger.info(
        "Formatted %s as %s (label=%r)%s",
        device, fstype, label[:16], f", remounted at {mountpoint}" if mountpoint else "",
    )
    return {
        "ok": True,
        "device": info["device"],
        "fstype": fstype,
        "label": _sanitize_label(label, FILESYSTEMS[fstype]["max_label"]),
        "mountpoint": mountpoint,
        "remounted": bool(was_mounted and mountpoint),
    }


# Tools that can change the volume label of an existing filesystem without
# reformatting. Maps fstype -> (executable, max_label_len); argument order is
# handled by _label_cmd().
_LABEL_TOOLS: dict[str, tuple[str, int]] = {
    "ext2": ("e2label", 16),
    "ext3": ("e2label", 16),
    "ext4": ("e2label", 16),
    "xfs": ("xfs_admin", 12),
    "btrfs": ("btrfs", 12),
    "vfat": ("fatlabel", 11),
    "exfat": ("exfatlabel", 15),
    "ntfs": ("ntfslabel", 12),
    "ntfs3": ("ntfslabel", 12),
}

def _label_cmd(fstype: str, device: str, label: str) -> list[str]:
    """Build the argv for changing a volume label (no reformat)."""
    tool, _max = _LABEL_TOOLS[fstype]
    if fstype == "xfs":
        return [tool, "-L", label, device]
    if fstype == "btrfs":
        return [tool, "label", "set", device, label]
    lbl = label.upper() if fstype == "vfat" else label  # FAT labels are stored uppercase
    return [tool, device, lbl]

def relabel_usb(device: str, label: str) -> dict:
    """Change the volume label of a USB partition without reformatting.

    No data is touched. If the volume is mounted it is briefly unmounted and
    remounted at the SAME mount point so the new label takes effect everywhere;
    fstab entries are untouched (they key on PARTUUID/UUID, not the label).
    """
    info = usb_mount_service._resolve_device(device)
    fstype = (info["fstype"] or "").lower()
    spec = _LABEL_TOOLS.get(fstype)
    if spec is None:
        raise UsbMountError(
            f"Cannot relabel {device}: no label tool for filesystem "
            f"'{fstype or 'unknown'}' (supported: {', '.join(sorted(_LABEL_TOOLS))})"
        )
    tool, max_len = spec
    if shutil.which(tool) is None:
        raise UsbMountError(
            f"{tool} is not installed on this host — cannot change the label "
            f"of a {fstype} volume."
        )

    new_label = (label or "").strip()
    if not new_label:
        raise UsbMountError("Label must not be empty")
    new_label = _sanitize_label(new_label, max_len)
    old_label = info["label"]
    if new_label == old_label:
        return {
            "ok": True, "device": info["device"], "fstype": fstype,
            "old_label": old_label, "new_label": new_label, "changed": False,
            "mountpoint": info["mountpoint"], "remounted": False,
        }

    tree = usb_mount_service._lsblk_tree()
    node, _parent = usb_mount_service._find_node(tree, info["name"])
    if node is None:
        raise UsbMountError(f"Device not found: {device}")

    was_mounted = bool(info["mountpoint"])
    old_mp = info["mountpoint"]
    if was_mounted:
        _unmount_node(node, info["device"])

    cmd = _label_cmd(fstype, device, new_label)
    logger.info("Relabeling %s (%s): %r → %r", device, fstype, old_label, new_label)
    _run_dev_checked(device, cmd, timeout=60)

    mountpoint = ""
    if was_mounted:
        # _unmount_node may have removed the now-empty directory — recreate it.
        usb_mount_service._ensure_mount_point(old_mp)
        # Non-root this goes through the USB helper (see usb_mount_service).
        usb_mount_service._run_checked(["mount", info["device"], old_mp], device=info["device"])
        mountpoint = old_mp

    return {
        "ok": True,
        "device": info["device"],
        "fstype": fstype,
        "old_label": old_label,
        "new_label": new_label,
        "changed": True,
        "mountpoint": mountpoint,
        "remounted": bool(was_mounted and mountpoint),
    }

def create_partition(
    disk_device: str,
    size_gb: Optional[float] = None,
    fstype: str = "ext4",
    label: str = "",
    auto_mount: bool = True,
) -> dict:
    """Create a new partition in the trailing free space of a USB disk.

    size_gb=None uses all remaining free space. The new partition is formatted
    with `fstype`/`label`; when auto_mount is set it gets a persistent mount
    (fstab, nofail) like every other managed volume.
    """
    info = usb_mount_service._resolve_device(disk_device)
    tree = usb_mount_service._lsblk_tree()
    _disk_node, parts = _disk_children(tree, info["name"])  # validates it is a real USB disk

    return _create_partition_parted(
        info["device"], int(info["size_bytes"] or 0), size_gb, fstype, label,
        auto_mount, {p.get("name") for p in parts},
    )


def _parted_layout(dev: str) -> tuple[int, int, bool]:
    """Return (last_partition_end_bytes, disk_size_bytes, has_label) via parted."""
    out = _run_dev_checked(
        dev, ["parted", "-s", dev, "unit", "B", "print"], timeout=30,
    ).stdout
    size_m = re.search(r"Disk /dev/\S+:\s*(\d+)\s*B", out)
    if not size_m:
        raise UsbMountError(f"Could not parse parted output for {dev}")
    disk_size = int(size_m.group(1))

    last_end = 0
    # Rows like: " 1      1048576B   32212253695B       32211205120B   partition ext4"
    for m in re.finditer(r"^\s*\d+\s+(\d+)B\s+(\d+)B", out, re.MULTILINE):
        last_end = max(last_end, int(m.group(2)))
    has_label = bool(re.search(r"Partition Table:\s*\S+", out))
    return last_end, disk_size, has_label


def _create_partition_parted(
    dev: str, disk_size: int, size_gb: Optional[float], fstype: str, label: str,
    auto_mount: bool, existing_names: set[str],
) -> dict:
    last_end, parted_size, has_label = _parted_layout(dev)

    # Raw stick with a whole-disk filesystem and no partition table:
    # create a GPT first (this discards the old whole-disk layout).
    if not has_label:
        logger.info("No partition table on %s — creating GPT", dev)
        _run_dev_checked(dev, ["parted", "-s", dev, "mklabel", "gpt"], timeout=60)

    total = max(disk_size, parted_size)

    ALIGN = 1024 * 1024
    start = max(ALIGN, math.ceil((last_end + 1) / ALIGN) * ALIGN)
    free = total - start
    if free < ALIGN:
        raise UsbMountError(f"No usable free space left on {dev}")

    if size_gb is None:
        # Fill the rest of the disk, leaving a small tail for the GPT backup header.
        end = total - ALIGN
        new_size = free
    else:
        want = int(size_gb * 1024 ** 3)
        if want < ALIGN:
            raise UsbMountError("Partition must be at least ~1 MiB")
        if want > free:
            raise UsbMountError(
                f"Not enough free space on {dev}: requested {size_gb:g} GB, "
                f"available {free / 1024 ** 3:.1f} GB"
            )
        end = start + want
        new_size = want

    name = _sanitize_label(label, 20) or "ethos"
    logger.info("Creating partition on %s: start=%d end=%d", dev, start, end)
    # parted only knows a subset of filesystem names for the mkpart hint
    # (exfat/vfat are rejected); omit the hint when it isn't one.
    mkpart_args = ["parted", "-s", dev, "unit", "B", "mkpart", name]
    if fstype in PARTED_FS_HINTS:
        mkpart_args.append(PARTED_FS_HINTS[fstype])
    mkpart_args += [str(start), str(end)]
    _run_dev_checked(dev, mkpart_args, timeout=120)

    # Find the new partition (child not present before mkpart).
    tree = usb_mount_service._lsblk_tree()
    _, parts = _disk_children(tree, dev.rsplit("/", 1)[-1])
    fresh = [p for p in parts if p.get("name") not in existing_names]
    if not fresh:
        raise UsbMountError("Partition was created but is not visible yet")
    new_part = max(fresh, key=lambda p: int(p.get("size", 0) or 0))
    new_dev = f"/dev/{new_part['name']}"

    # Guard against stale partition nodes (e.g. /dev/loopNpM left over from a
    # previous run): the size lsblk reports must match what parted was asked
    # to create, otherwise we would format the wrong device.
    part_size = int(new_part.get("size", 0) or 0)
    tolerance = max(8 * 1024 ** 2, new_size // 50)
    if part_size and abs(part_size - new_size) > tolerance:
        raise UsbMountError(
            f"New partition {new_dev} reports {part_size / 1024**3:.2f} GB but "
            f"{new_size / 1024**3:.2f} GB was requested — refusing to format a "
            f"stale or mismatched device node"
        )

    _mkfs(new_dev, fstype, label)

    mountpoint = ""
    if auto_mount:
        result = usb_mount_service.mount_usb(new_dev)
        mountpoint = result.get("mountpoint", "")

    logger.info(
        "Created %s partition on %s (%.1f GB)%s",
        fstype, dev, new_size / 1024 ** 3, f", mounted at {mountpoint}" if mountpoint else "",
    )
    return {
        "ok": True,
        "disk": dev,
        "partition": new_dev,
        "size_bytes": int(new_part.get("size", 0) or 0),
        "fstype": fstype,
        "label": _sanitize_label(label, FILESYSTEMS[fstype]["max_label"]),
        "mountpoint": mountpoint,
    }


def delete_partition(device: str) -> dict:
    """Delete a USB partition (unmount + remove fstab entries first)."""
    info = usb_mount_service._resolve_device(device)
    tree = usb_mount_service._lsblk_tree()
    node, parent = usb_mount_service._find_node(tree, info["name"])
    if node is None:
        raise UsbMountError(f"Device not found: {device}")
    if node.get("type") != "part":
        raise UsbMountError(f"{device} is not a partition")

    disk_dev = f"/dev/{parent['name']}" if parent else ""
    if not disk_dev:
        raise UsbMountError("Could not determine the parent disk of this partition")

    # Unmount (if needed) and drop any fstab entries — even when currently
    # unmounted, a persistent entry would otherwise go stale.
    _unmount_node(node, info["device"])
    fstab_removed = _clean_fstab_for(node)

    # Partition index = position among siblings, 1-based.
    parts = [c for c in (parent.get("children") or []) if c.get("type") == "part"]
    idx = next((i + 1 for i, p in enumerate(parts) if p.get("name") == node.get("name")), None)
    if idx is None:
        raise UsbMountError(f"Could not locate {info['name']} on {disk_dev}")

    logger.info("Deleting partition %d (%s) from %s", idx, info["device"], disk_dev)
    _run_dev_checked(disk_dev, ["parted", "-s", disk_dev, "rm", str(idx)], timeout=60)
    usb_mount_service.forget_mountpoint(usb_mount_service._unit_key(node))

    return {
        "ok": True,
        "device": info["device"],
        "disk": disk_dev,
        "index": idx,
        "fstab_entries_removed": fstab_removed,
    }


def initialize_disk(disk_device: str, fstype: str = "ext4", label: str = "", auto_mount: bool = True) -> dict:
    """Wipe all partitions on a USB disk and recreate one full-size volume.

    Equivalent to "Initialize": every existing partition is unmounted,
    its fstab entries removed, the GPT table is recreated, and a single
    partition spanning the whole disk is formatted (and optionally mounted).
    """
    info = usb_mount_service._resolve_device(disk_device)
    tree = usb_mount_service._lsblk_tree()
    disk_node, old_parts = _disk_children(tree, info["name"])

    for part in old_parts:
        _unmount_node(part, info["device"])

    # Clear stale signatures (whole disk — every partition is being wiped).
    if shutil.which("wipefs"):
        try:
            _run_dev_cmd(info["device"], ["wipefs", "-a", info["device"]], timeout=30)
        except Exception:  # noqa: BLE001 — mklabel below is the real gate
            pass

    logger.info("Re-initializing GPT on %s", info["device"])
    _run_dev_checked(
        info["device"], ["parted", "-s", info["device"], "mklabel", "gpt"], timeout=60,
    )

    # Wait for the kernel to drop the old partition nodes (usually instant).
    for _ in range(10):
        tree = usb_mount_service._lsblk_tree()
        _, remaining = _disk_children(tree, info["name"])
        if not remaining:
            break
        time.sleep(0.3)
    else:
        raise UsbMountError(
            f"Old partitions on {info['device']} are still visible after re-init; "
            "aborted to avoid overlapping them."
        )

    # Every old partition is gone — drop whatever fstab entries referenced it.
    # (Uses the pre-wipe node list: after mklabel the tree no longer has them.)
    cleaned = 0
    for part in old_parts:
        cleaned += _clean_fstab_for(part)
        usb_mount_service.forget_mountpoint(usb_mount_service._unit_key(part))

    result = _create_partition_parted(
        info["device"], int(info["size_bytes"] or 0), None, fstype, label,
        auto_mount, set(),
    )
    result["fstab_entries_removed"] = cleaned
    return result
