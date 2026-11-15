# Storage App — USB Relabel & Stable Mount Points

Two improvements to the Storage app's USB tab, both aimed at making
repeated mount/unmount cycles painless so backup jobs can target a fixed `/mnt/...` path.
Everything below was verified against the code on 2026-07-14.

## Problem

1. **Folder churn under /mnt** — every Mount/Unmount cycle could create a new numbered
   folder (`usb_sdb1`, `usb_sdb2`, …, `Backup2_2`) because:
   - unmount left the mount-point directory behind;
   - lazy-unmounted entries lingered in `/proc/mounts` with the `shutdown` option and
     blocked reuse of the old path;
   - the fallback name is derived from the kernel device name (`usb_sdb1`), which changes
     when a drive gets a different letter on re-plug.
2. **No way to rename a volume** — changing a label required reformatting (data loss).

## File map

| What | Where |
|---|---|
| Mount/unmount service (+ memory) | `backend/services/usb_mount_service.py` |
| Partition ops + relabel | `backend/services/usb_partition_service.py` (`relabel_usb`, `_LABEL_TOOLS`) |
| API endpoints | `backend/routers/storage.py` — `POST /api/storage/usb/mount` (new optional `folder`), `POST /api/storage/usb/relabel` |
| Frontend UI | `frontend/static/apps/storage.js` — mount dialog with folder input, `✎ Rename` button + `openRelabelDialog()` |
| Mount-point memory file | `<DATA_DIR>/usb_mounts.json` (default `backend/data/`) — `{PARTUUID=…: "/mnt/path"}` |
| E2E test (root) | `tests/test_usb_partition.py` — sections [2], [4], [7] (relabel), [9] (stability) |

## Design

### Stable mount points (`usb_mount_service.py`)

- **Memory**: after every successful mount the chosen path is stored in
  `<DATA_DIR>/usb_mounts.json` keyed by `PARTUUID=` (or fs `UUID=` for whole-disk units).
  Thread-safe (lock + atomic replace), best-effort — a corrupt/missing file just means
  "no memory yet". Module attr `STATE_PATH` overrides the location (tests).
- **Path preference on mount**: remembered path (if free) → explicit `folder` from the
  Mount dialog → label/device-name derivation with `_2`, `_3`, … suffixes as before.
  A candidate is usable only when not currently mounted and either absent or an empty
  directory — we never hide files by mounting over a non-empty dir.
- **Unmount**: waits up to 5 s for the entry to actually leave `/proc/mounts` (lazy
  unmounts drain until the last handle closes), then best-effort `rmdir` of the now-empty
  directory. Dying entries (`shutdown` option) are excluded from "taken" via
  `_current_mount_points(include_dying=False)` so they never block reuse.
- **Forget hooks**: `forget_mountpoint(key)` is called after format (fs identity changed →
  next mount derives from the new label), partition delete, and disk re-initialize.
- fstab entries are untouched by relabel/re-mount — they key on PARTUUID/UUID, so a
  renamed volume keeps its persistent entry at the same path.

### Relabel (`usb_partition_service.relabel_usb`)

Per-filesystem label tools (no reformat, no data loss):

| Filesystem | Tool | Max len |
|---|---|---|
| ext2/3/4 | `e2label` | 16 |
| xfs | `xfs_admin -L` | 12 |
| btrfs | `btrfs label set` | 12 |
| vfat | `fatlabel` (uppercased) | 11 |
| exfat | `exfatlabel` | 15 |
| ntfs/ntfs3 | `ntfslabel` | 12 |

Flow: validate USB device → require non-empty label (`_sanitize_label`) → if mounted,
unmount (fstab untouched) → run the tool → remount at the **same** path. Missing tools
raise a clear error (e.g. `xfs_admin is not installed on this host …`).

## Status

- [x] `relabel_usb()` in partition service + per-fs label tools
- [x] `POST /api/storage/usb/relabel` endpoint (+ notification)
- [x] Storage UI: partition labels shown, `✎ Rename` button + dialog (max-len enforced)
- [x] Mount-point memory (`usb_mounts.json`) + reuse on mount
- [x] Optional `folder` param on `POST /api/storage/usb/mount`; Mount dialog input
  pre-filled with the remembered path (or label/device name)
- [x] Unmount: drain-wait for lazy unmounts + rmdir of empty dir; `_unmount_node` too
- [x] Forget hooks in format/delete/init
- [x] `last_mountpoint` exposed per partition (`GET /api/storage/usb`) — UI shows
  "Not mounted (was /mnt/X)"
- [x] Tests: relabel ext4/vfat/exfat (mounted + unmounted), stability section [9]
- [ ] Manual verification on a real USB drive (relabel + mount/unmount cycles)

## Verification log

- `sudo .venv/bin/python tests/test_usb_partition.py` — see session 2026-07-14 run.
- Host tool availability: `e2label`, `fatlabel`, `exfatlabel`, `ntfslabel` present;
  `xfs_admin`, `btrfs` absent (relabel of xfs/btrfs volumes raises a clear error until
  installed).
