# EthOS OS image updates (A/B) — consumer side

Status: **shipped** (2026-11-22, v2.11.0) for A/B images built by
`ethos-os-release` (`docs/ab-updates.md` there). Single-slot installations see
the card read-only with a reflash hint.

## What it does

Settings → System → **EthOS OS** checks the signed `ethos-os-release` index,
downloads the slot-agnostic rootfs artifact, verifies its SHA-256, and asks the
root helper `ethos-os-image apply` to write it into the **standby** A/B slot.
Nothing reboots on its own; the operator presses **Reboot to apply**. The image's
boot counter and health gate roll a bad slot back automatically.

Data lives on the shared `ETHOS-DATA` partition (`/var/lib/ethos-nas`) and is
never touched by an update or a rollback.

## Requirements

- an **A/B image**: `/etc/ethos-os/layout` contains `ab1`, partitions
  `ETHOS-ROOT-A`, `ETHOS-ROOT-B`, `ETHOS-DATA`, and `/usr/local/sbin/ethos-slot`
  present;
- the signed index entry has `layout: "ab1"` and a `rootfs` object
  (`image`, `url`, `sha256`, `size_bytes`);
- `ethos-os-image` installed and allowlisted by provisioning
  (`ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-image apply`).

A `single` layout (today's 8 GiB image) cannot update in place: the card says so
and points at the reflash/migration path.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/system/os-image` | any JWT | cached index, identity, slots, download/install state; always 200 |
| GET | `/api/system/os-image/status` | any JWT | download + install job detail |
| POST | `/api/system/os-image/check` | admin | fetch + verify the signed index |
| POST | `/api/system/os-image/download` | admin | stream the rootfs artifact into `DATA_DIR/os-image/` |
| POST | `/api/system/os-image/install` | admin | write `request.json` + start `ethos-os-image apply` |
| DELETE | `/api/system/os-image/download` | admin | remove the downloaded artifact |

Guards: A/B layout, helper present, non-empty signed latest, no pending
candidate slot, image downloaded, `min_ethos_version` satisfied, one download and
one install at a time. Errors carry the exact hint (for example the one-time
`setup_service.sh` run when the helper is missing).

## Root helper — `deploy/ethos-os-image`

`apply` takes no arguments and is not reachable by any other means. It:

1. fetches and verifies the signed index itself (Ed25519, pinned key),
2. requires `layout: ab1` and a `rootfs` entry,
3. verifies the requested file (path inside `DATA_DIR/os-image/`, size, SHA-256,
   `xz` integrity, uncompressed size vs slot size),
4. resolves the target by **label** (`ETHOS-ROOT-A|B`), refuses the active slot,
5. writes with `xz -dc | dd` into the standby partition, relabels it, runs
   `e2fsck`/`resize2fs`, mounts read-only and checks `build-info.json` +
   `/boot/vmlinuz`,
6. marks the slot `candidate` and activates it with two boot attempts (via
   `ethos-slot`). It never reboots and never writes the active slot.

State: `<DATA_DIR>/os-image/{request,job,result}.json` (service user owns the
directory; the helper chowns its files) and `<DATA_DIR>/system/slots.json`
(root-written mirror for the UI).

## UI

Settings → System, card **EthOS OS**:

- installed version + slot, available image (version, compressed size, notes),
- **Check** / **Download** / **Install to standby** / **Reboot to apply**
  (admin only; progress through `core/progress.js` with 1–2 s polling),
- slot status and the last rollback/install result,
- single-slot callout with the reflash hint.

Notifications: image available (`os-image:<rootfs sha>`), downloaded, staged
(reboot required), failed. Background check every 24 h, sleeps first.

## Data and rollback safety

- The data partition is not mounted by the helper and is never written.
- The active slot is never written; a failed write leaves the old slot booting.
- Slot metadata and the GRUB environment (`/boot/efi/ethos/grubenv`) are written
  only after the new slot passes `e2fsck` and the read-only verification.
- A candidate that fails to boot (tries exhausted) or fails the post-boot health
  probe switches back automatically (`ethos-slot` in the image).

## Existing single-slot installations

There is no in-place repartition. Back up `DATA_DIR`, reflash an A/B image
(installer `--layout ab` once available), restore the data into
`/var/lib/ethos-nas`, and run `setup_service.sh` once. The image card stays
read-only until then.

## Config

| Env | Default | Meaning |
|---|---|---|
| `ETHOS_OS_IMAGE_ENABLED` | `true` | kill switch |
| `ETHOS_OS_IMAGE_INDEX_URL` | `…/ethos-os-release/main/releases/index.json` | signed index |
| `ETHOS_OS_IMAGE_PUBKEY` | pinned release key | override for a fork |
| `ETHOS_OS_IMAGE_HELPER` | `/usr/local/bin/ethos-os-image` | helper path (tests bypass sudo) |
| `ETHOS_OS_IMAGE_TTL_SECONDS` | `3600` | cached check freshness |
| `ETHOS_OS_IMAGE_CHECK_HOURS` | `24` | background cadence |
| `ETHOS_OS_IMAGE_MAX_BYTES` | `4 GiB` | download cap |

## Tests

- `tests/test_os_image_helper.py` — helper contract (signed index, hash, labels,
  active-slot refusal, no reboot) and the manifest rule.
- `tests/test_os_image_service.py` — index parsing, bounds, download/install
  guards, stale jobs, download removal.
- `tests/test_os_image_api.py` — isolated instance: 401/403/200, layout/helper
  reporting, guard responses.
- `tests/test_security_invariants.py`, `test_app_ui.py`, `test_settings_ui.py`,
  `test_router_smoke.py` updated.
