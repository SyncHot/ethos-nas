# Server hardening — GMKtec N150 (Home Assistant first)

Spec of the box this EthOS NAS runs on, what was **measured** (not assumed) and which
isolation/resilience knobs are applied. Home Assistant is the highest-priority
workload: every number below is there to answer "does video work cost HA its CPU".

## Hardware and platform (measured 2026-11-27)

| Item | Value |
|---|---|
| CPU | Intel N150 · 4 cores / 4 threads (Alder Lake-N) |
| GPU | `8086:46d4` Intel Graphics · driver **`i915`** (module `xe` also loaded) |
| DRM nodes | `/dev/dri/card0`, `/dev/dri/renderD128` (group `render`) |
| VA-API | 1.23 · iHD 26.1.2 · `H264 High: VLD + EncSliceLP` (hw decode **and** encode) |
| FFmpeg | 8.0.1 · `vaapi` + `qsv`, encoders `h264_vaapi/h264_qsv/hevc_*` |
| OS / kernel | Ubuntu **26.04.1 LTS** · 7.0.0-44 (the brief said 24.04 — it is newer) |
| Temps (idle) | CPU 56–58 °C · NVMe 43 °C (sensor 3: 69 °C) |
| RAM | 14 GiB usable · swap 4 GiB file + 7.4 GiB zram |

## Transcode capability (measured, 1080p H.264 → H.264, 20 s slice)

Commands taken from `backend/services/video_engine.py` (the production path — do not
hand-roll QSV: `-hwaccel qsv` on software-decoded frames fails with
`Function not implemented`; only `-qsv_device` + `-vf …` works):

| Variant | Wall | Real-time |
|---|---|---|
| `h264_vaapi`, software decode | 4.91 s | 4.07× |
| `h264_qsv`, software decode | 4.64 s | 4.31× |
| `h264_vaapi`, **`decode_hw=True`** | 3.16 s | **6.33×** |
| `h264_qsv`, **`decode_hw=True`** | 2.44 s | **8.19×** |
| `libx264 -preset veryfast -threads 2` | 22.06 s | **0.91×** (cannot keep up) |

Hardware decode is worth +45 % (vaapi) / +114 % (qsv); production logs confirm the live
HLS path already runs `decode=hw` (`mode=transcode encoder=vaapi decode=hw`). Software
x264 is not an option on this box while HA runs.

HLS path with the real flags (`-re`, 4 s fMP4 segments, 720p `fast`): 27 % of one core
(vaapi) per viewer.

### Two simultaneous viewers, measured while HA served requests

Two real-time 720p HLS transcodes (`-re -t 40`, hw decode) + `curl` on HA every second:

| Metric | Result |
|---|---|
| HA latency (n=24) | min 1 ms · median **2 ms** · max **4 ms** |
| ffmpeg CPU (sum of both, median) | **43 %** of one core |
| Load average | 0.94 |

Conclusion: two viewers are nowhere near the CPU limit — the GPU does the work and HA
keeps its cores. The remaining ceiling is GPU encode sessions, not CPU.

## Applied isolation (2026-11-27)

| Change | Command | Effect |
|---|---|---|
| EthOS NAS capped at 2.5 cores / 2 GiB soft | `systemctl set-property ethos-nas.service CPUQuota=250% MemoryHigh=2G` | `cpu.max=250000 100000`; drop-ins in `/etc/systemd/system.control/ethos-nas.service.d/` (survives reboot); applied live, **no restart** (PID unchanged) |
| HA capped at 2 cores (was unlimited) | `docker update home-assistant --cpus=2` | `nano_cpus=2000000000`, live, container not recreated (`restarts=0`) |
| `xe` blacklisted | `/etc/modprobe.d/blacklist-xe.conf` | Alder Lake-N is i915-only; after a kernel update `xe` could claim the GPU and silently remove hardware transcode. Takes effect on next boot |

Deliberately **not** done: lowering `TranscodeLimiter` (live 2 / exports 1) — the
measurements above show 2 live sessions cost < 0.5 core, so the limiter is not the
bottleneck and tightening it would only hurt users.

## Remaining gaps (Stage 6)

1. **HA has no healthcheck** — adding one requires recreating the container (~30 s
   downtime), so it is a scheduled-window change, not a live one.
2. **No scheduled backups** — the only systemd timer is `dpkg-db-backup`. Plan: HA
   snapshot + `backend/data` to `/mnt/Backup2` on a nightly timer with retention.
3. **No UPS integration** — `nut-monitor`/`upsd` inactive, `upsc -l` empty. When the UPS
   is connected: `apt install nut`, `nut-scanner`, `ups.conf`/`upsmon.conf`, shutdown
   ordering, and `docker restart: unless-stopped` semantics on power loss.
4. **No hardware watchdog** — `RuntimeWatchdogSec` unset, no `wdat_wdt`/`softdog`.
5. **No alerting** — EthOS health/logs are local only; HA is not notified. Plan: push
   EthOS events to a HA webhook + NVMe SMART (69 °C sensor) and CPU-thermal thresholds.
6. **No ABR ladder** — one HLS session = one quality (switching restarts the session at
   the current position). A true 480/720/1080 master playlist is a planned feature
   (`docs/abr-ladder.md`), not a fix; on this GPU it means 3 encoders per viewer.

## Verification commands

```bash
systemctl show ethos-nas -p CPUQuotaPerSecUSec -p MemoryHigh   # 2.500000s / 2147483648
cat /sys/fs/cgroup/system.slice/ethos-nas.service/cpu.max      # 250000 100000
awk '/nr_throttled/{print}' /sys/fs/cgroup/system.slice/ethos-nas.service/cpu.stat
docker inspect home-assistant --format '{{.HostConfig.NanoCpus}}'   # 2000000000
lspci -k -s 00:02.0 | grep 'driver in use'                     # i915
vainfo | grep -E 'VAProfileH264High'                            # VLD + EncSliceLP
curl -s -o /dev/null -w '%{http_code}\n' localhost:9000/api/system/status  # 401 = up
```
