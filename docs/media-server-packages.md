# Media server packages — Jellyfin & Plex with hardware transcoding

Status: shipped in 2.42.0 (core support) + `ethos-packages` release.

Two long-requested add-ons, plus the core work they need: a NAS that can hand a GPU to a
container without losing the "software still works" promise.

## File map

| What | Where |
|---|---|
| Host GPU probe (DRI nodes, group ids, NVIDIA runtime) | `backend/services/gpu_host.py` |
| Capability → compose fragment | `backend/services/package_gpu.py` |
| Generated override (port remap + GPU) | `backend/services/package_ports.py` |
| Install / start / repair wiring | `backend/services/package_installer.py` |
| Manifest schema (capabilities, `ui.embed`) | `backend/services/package_schema.py` |
| Compose policy (devices, `runtime:`, `gpus:`, `deploy:`) | `backend/services/compose_policy.py` |
| `/api/system/status` → `gpu`, catalog → `gpu_status` | `backend/routers/system.py`, `backend/routers/packages.py` |
| Packages UI (badges, GPU line, embed card) | `frontend/static/apps/packages.js`, `frontend/static/app.js` |
| Packages themselves | `~/apps/compose/ethos-packages/jellyfin`, `.../plex` |
| Spec of the capability model | `docs/packages.md` → "Hardware transcoding (GPUs)" |
| Tests | `tests/test_package_gpu.py`, `tests/test_package_logic.py` |

## How a package gets a GPU

1. The manifest asks, it never points:
   ```json
   {"capabilities": ["compose", "dri", "nvidia"]}
   ```
2. `gpu_host.probe()` (cached 60 s) answers whether the host can deliver: openable
   `/dev/dri/renderD*`, `render`/`video` gids, `/dev/nvidia*`, driver binaries, and whether
   `docker info` advertises the `nvidia` runtime.
3. `package_gpu.fragment()` turns that into compose additions — or into nothing.
4. `package_ports.build_override()` writes them into the package's
   `docker-compose.override.yml` (the same file the port remap uses), so the signed compose file
   stays fixed and portable.
5. The record keeps `gpu: {requested, applied, devices, runtime, reason}`; the catalog shows
   `gpu_status` before install, Packages renders it in the detail dialog.

Anything missing (no `/dev/dri`, unreadable node, no nvidia runtime) means **software transcoding**,
never a failed install. Re-probing happens on install, update, `repair-ports` and start/restart.

## Hardware matrix (this NAS)

| Host | VAAPI/QSV (`dri`) | NVENC (`nvidia`) |
|---|---|---|
| Intel iGPU (`i915`, e.g. `8086:46D4`) | yes — `renderD128`, render gid 990 | — |
| AMD GPU (`amdgpu`) | yes — VAAPI | — |
| NVIDIA GPU | — | needs driver + `nvidia-container-toolkit` + Docker nvidia runtime (probe-only: EthOS prints the commands) |
| No GPU | software `libx264` | software |

Measured baseline for the core's own Videos engine on this host: ~1,9× realtime for 4K H.264 with
QuickSync vs ~0,8× with software (`CHANGELOG.md`, 2.41.1).

## Packages

### `jellyfin` (official `jellyfin/jellyfin`)

- `user: "${ETHOS_SERVICE_UID}:${ETHOS_SERVICE_GID}"`, config in `data` → `/config`, cache on a
  named volume, media mounted read-only from `/home`, `/mnt`, `/media`.
- `devices: [/dev/dri/renderD128]` + `group_add: [render, video]` injected when available;
  NVENC env vars ship in the signed compose file (inert without the nvidia runtime).
- `ui.expose: "lan"`, `ui.auth: "package"`, `ui.embed: false` — Jellyfin refuses framing, so the
  window shows the tab card. Enable hardware transcoding in Jellyfin → Playback.
- No `network_mode: host` (policy): client auto-discovery and DLNA are unavailable; add the server
  by address/port.

### `plex` (official `plexinc/pms-docker`)

- `PLEX_UID`/`PLEX_GID` from the EthOS service identity, `/config` on the data folder, media
  read-only, port 32400 only.
- Optional `PLEX_CLAIM` (first-run only) and `ADVERTISE_IP` in the install dialog.
- VAAPI via `/dev/dri`; NVENC is best-effort (Plex documents Intel Quick Sync only).
- Hardware transcoding requires a **Plex Pass**.

## Verification (run for each release)

```bash
.venv/bin/python tests/test_package_gpu.py          # probe, fragment, override, installer wiring
.venv/bin/python tests/test_package_logic.py        # schema + compose policy
.venv/bin/python tests/test_packages_api.py         # install/uninstall through the job API
# isolated instance on :9002 with a locally signed repo:
docker inspect ethos-jellyfin --format '{{json .HostConfig.Devices}}'
docker exec ethos-jellyfin /usr/lib/jellyfin-ffmpeg/vainfo     # iHD/i965 present
curl -s -H "Authorization: Bearer $TOKEN" localhost:9002/api/packages/jellyfin/status
```

## Status

- [x] `dri` / `nvidia` capabilities + compose policy (devices, `runtime:`, `gpus:`, `deploy:`)
- [x] Host probe, `/api/system/status` → `gpu`, `catalog/{id}` → `gpu_status`
- [x] GPU override materialisation with CPU fallback and re-probe on start/repair
- [x] `ui.embed: false` window mode
- [x] Packages UI: capability badges, GPU line, device repair label
- [x] `jellyfin` package published
- [x] `plex` package published
- [ ] NVIDIA end-to-end verification on real hardware (no NVIDIA GPU on the dev host)
- [ ] Plex NVENC official support (upstream documents Quick Sync only)

## Deferred

- Installing NVIDIA driver/toolkit through `system_requires` (probe-only today; would need new
  sudoers ids and a Docker daemon restart).
- A per-package "transcode hardware" selector — the apps own that setting.
- Letting the core Media/Videos app use the GPU of a *package* container.
