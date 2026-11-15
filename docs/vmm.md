# EthOS VM Manager (`vmm`) — spec & AI handoff

A **Virtual Machine Manager** (VMM 2.8) equivalent shipped as a Packages package:
create, run, snapshot and export KVM virtual machines from a native EthOS window, with a browser
console. Planned 2026‑11‑13, **nothing implemented yet** — this document is the plan and the resume
point.

Package code lives in the **separate repo** `~/apps/compose/ethos-packages/vmm/`, never in
`backend/`. Only Phase 0 (a narrow compose-policy capability) touches this repository.

## Decisions (locked 2026‑11‑13)

| Decision | Choice | Consequence |
|---|---|---|
| Identity | id **`vmm`**, name **EthOS VM Manager** | window/taskbar title from `manifest.name`, Packages order `vmm` |
| KVM access | new `kvm` compose capability, `devices: ["/dev/kvm"]` only | approved — Phase 0 lands in this repo first |
| Engine image | **no prebuilt image**; `python:3.12-slim` + apt bootstrap, `.deb` cache in the package volume | first start 1–3 min, later starts seconds, needs egress once |
| UI & auth | `ui.mode: "native"`, API through `ctx.proxy`, console iframe on LAN port `8094` with HMAC ticket | the engine never trusts an unauthenticated LAN request |
| v1 scope | **MVP**: create → boot → console → stop (+ ISOs, autostart, persistence) | port forwards, snapshots, clone, OVA and charts ship in v1.1 |

## What VMM does (the thing being matched)

| VMM capability | Scope | Notes |
|---|---|---|
| Run Windows/Linux VMs on KVM | **MVP** | QEMU/KVM, one engine container |
| Web VNC console in the browser | **MVP** | noVNC + websockify inside the engine container |
| CPU/RAM/disk/vNIC/boot config per VM | **MVP** | virtio + q35/UEFI, IDE/SATA fallback |
| Start / shutdown / stop / reset | **MVP** | QMP + ACPI |
| Autostart on host start | **MVP** | engine replays `autostart` VMs |
| Pause / resume | v1.1 | `stop`/`cont` over QMP, same code path |
| Instant clone | v1.1 | `qemu-img` backing-file clone |
| Snapshots (+ scheduled) | v1.1 | qcow2 internal snapshots via QMP `savevm`/`loadvm` |
| Export / import (OVA, vmdk/vhdx/vdi/qcow2/img) | v1.1 | `qemu-img convert` + OVA tarball |
| Port forwarding to a VM | v1.1 | pool `8100-8199` (user-mode NAT has no inbound by default) |
| Per-VM CPU/RAM/network monitoring | v1.1 | QMP blockstats + `/proc` inside the container |
| Per-user permissions, URL sharing | **no (gap)** | packages have no EthOS user model — see *Decisions* |
| Virtual switches, bridged/LACP networking, SR‑IOV | **no (gap)** | needs `NET_ADMIN` / host network — forbidden |
| Clusters, live migration, HA, protection plans to a remote host | **no (gap)** | multi-host product, out of scope |
| USB passthrough, vendor VM images | **no (gap)** | needs host devices; vendor images are not supported |

## Blocking constraint: KVM needs `/dev/kvm`

`backend/services/compose_policy.py` rejects `devices:` outright, and `privileged` / host network /
`cap_add` are equally forbidden. Without `/dev/kvm`, QEMU falls back to TCG software emulation —
roughly 5–10× slower, unusable for Windows and for anything interactive.

The host already exposes KVM (`/dev/kvm`, group `kvm`, `vmx` present on this NAS), so the honest
path is a **deliberately narrow policy exception**, not TCG-by-default:

- Only the literal device `/dev/kvm` may be declared.
- Only when the manifest declares `"capabilities": ["kvm"]`.
- Nothing else in the `devices:` list is accepted; `privileged`, `cap_add`, host network and docker
  socket binds stay forbidden.
- No core change is needed for `group_add` (already unchecked) or for `user: "0:0"`.

The engine still degrades gracefully: if `/dev/kvm` is missing or the policy rejected it (older
EthOS), it starts the VM with `-accel tcg` and the UI labels the VM "software emulation".

## Phases

### Phase 0 — core: `kvm` compose capability (this repo, `S`)

| File | Change |
|---|---|
| `backend/services/compose_policy.py` | `lint_compose(text_or_dict, capabilities=None)`; allow `devices` entries exactly `['/dev/kvm']` when `"kvm" in capabilities`, keep rejecting everything else |
| `backend/services/package_installer.py` | pass `manifest.get("capabilities")` into `enforce_compose` |
| `backend/services/package_schema.py` | validate `capabilities` as a list of known strings (`compose`, `kvm`) |
| `frontend/static/apps/packages.js` | "/dev/kvm" chip on the catalog card + detail modal, with a note that the package falls back to software emulation when the host has no KVM |
| `tests/test_package_logic.py` | policy tests: `devices: ['/dev/kvm']` accepted with the capability, rejected without it, `devices: ['/dev/sda']` still rejected |
| `docs/packages.md` | document the capability next to the existing policy list |

Also worth exposing: whether the host has KVM (`os.path.exists("/dev/kvm")`) in `/api/system/status`
so Packages and the package BOTH agree.

**Acceptance:** a compose file that only adds `devices: ["/dev/kvm"]` + `capabilities: ["kvm"]`
installs; the same file without the capability still fails with the existing message.

### Phase 1 — engine bootstrap + console (`M`)

```text
ethos-packages/vmm/
├── manifest.json            # ui.mode native, port 8095, capabilities ["compose","kvm"]
├── docker-compose.yml
├── icon.svg
├── frontend/app.js          # native module (self-contained, light+dark)
├── server/
│   ├── bootstrap.sh         # apt provisioning of qemu + noVNC, cached in the volume
│   ├── app.py               # API (:8095) + noVNC + console page (:8094) + WS bridge
│   ├── engine.py            # registry, paths, lifecycle, autostart
│   ├── qemu.py              # argv builder, qemu-img helpers
│   ├── qmp.py               # QMP client (unix socket per VM)
│   ├── store.py             # atomic JSON stores
│   └── console.py           # WebSocket <-> VNC byte pump + RFC 6455 framing
└── tests/test_qemu.py       # argv, path scope, tickets, handshake
```

Phase 1 goal: a VM described by a hand-written `vms.json` boots and is visible in the browser
console, driven entirely by the API (no wizard yet).

Compose (one service: QEMU, the API and the console share the container so QMP sockets and VNC
ports stay on loopback):

```yaml
services:
  vmm:
    image: debian:12-slim        # bare Debian; QEMU is provisioned at first start
    container_name: ethos-vmm
    user: "0:0"                  # KVM access + 0660 NAS files
    devices: ["/dev/kvm"]        # allowed only with capabilities: ["kvm"]
    command: ["sh", "/app/bootstrap.sh"]
    ports:
      - "127.0.0.1:8095:8095"    # JSON API: NAS loopback -> EthOS proxy only
      - "8094:8094"              # console page + WS bridge (ticket-gated)
    volumes:
      - vmm-data:/data
      - ./server:/app:ro
      - /home:/vmhost/home
      - /mnt:/vmhost/mnt
      - /media:/vmhost/media
    stop_grace_period: 60s
    restart: unless-stopped
```

**Two listeners, by necessity.** EthOS's `/api/packages/{id}/proxy/` forwards GET/POST only, so a
VNC WebSocket cannot travel through it. The API therefore listens on 8095 and is published on the
NAS loopback (reachable only by EthOS and the NAS itself); the console listens on 8094 and carries
nothing but the noVNC page, its assets and the bridge — every request needs an HMAC ticket.

Browsers do not show anything useful at `http://<nas>:8094/` itself: that URL is not the app, it is
the console endpoint. It answers with a short explanatory page; the console link is minted per click
in the window.

**WebSocket subprotocol (fixed in 1.0.1).** noVNC offers `binary` in `Sec-WebSocket-Protocol`; a
browser aborts the upgrade when the server accepts but selects nothing, and the symptom is a black
console stuck on "connecting…" plus `Connection closed (code: 1006)` in the console log. The bridge
now echoes `binary` and rejects any other offered protocol with 400.

- `server/bootstrap.sh` provisions the engine. Apt **lists and archives** are kept in the volume
  (`/data/.engine/lists`, `/data/.engine/apt`), so the first start downloads `python3`,
  `qemu-system-x86`, `qemu-utils`, `novnc`, `ovmf` (≈60 s) and every later container re-install is
  fully offline (`apt-get --no-download`, ≈40 s). An incomplete cache falls back to the network.
  Two `dpkg`-only variants were tried and rejected: dpkg halts on dependency order.
- `/api/health` answers `503 {"state":"provisioning","step":"apt"}` while bootstrapping, so the
  window shows progress instead of an error.
- Service code arrives through `files[]`, so app fixes never need an image rebuild.
- `stop_grace_period: 60s` + SIGTERM handler: `system_powerdown` every running VM, wait, then `quit`.
- QEMU runs daemonized with `-pidfile`, so the QMP socket is the single control channel and a
  status query on a dead socket answers `stopped`.

### Phase 2 — MVP completion (`M`)

- Create VM wizard: name, vCPU (≤ host cores), RAM, disk size/bus, ISO, firmware (BIOS/UEFI),
  network model.
- Disk/ISO storage: user picks a root with `ctx.pickFolder({roots:['/home','/mnt','/media']})`, the
  service creates `<root>/ethos-vm/<vm-id>/`; **every** path is re-validated to start with
  `/vmhost/<root>/` before touching the filesystem — no client-supplied absolute paths outside it.
- Virtual media: attach/detach ISO on a running VM (`change` QMP command).
- Autostart + recovery: on container start, VMs with `autostart` are started (SIGKILL left them
  dirty; disks are qcow2, QEMU replays the journal).
- MVP is done when: install → bootstrap → create a VM from an uploaded ISO → boot → console → clean
  shutdown → restart EthOS → VM comes back.

### Phase 3 — v1.1: network, snapshots, clone, export (`M`)

- Port forwards: allocate from the published `8100-8199` pool, persisted, shown as
  `nas:8100 → 3389/tcp` — user-mode NAT has no inbound traffic otherwise.
- Pause/resume: `stop`/`cont` over QMP, surfaced as a status pill.
- Snapshots: `savevm <tag>` / `loadvm <tag>` / `delvm <tag>` over QMP, limit 16/VM, `snapshots.json`
  keeps name/description/date/size; restore requires a stopped VM (VMM does the same for
  non-quiesced snapshots — no guest agent here).
- Scheduled snapshots per VM (daily/weekly, keep N) executed by the engine's scheduler.
- Clone: `qemu-img create -f qcow2 -b <disk> -F qcow2` backing-file clone (instant), then detach on
  first write — plus "full clone" via `qemu-img convert`.
- Export: stop → `qemu-img convert` to `vmdk`/`vhdx`/`qcow2`, optional OVA tar (`.ovf` + disks).
  Import: `qemu-img info` → convert into the VM directory, reject formats/damaged images.
- Long operations run as **tasks** (`tasks.json`), polled by the UI — the `/proxy/` endpoint has a
  1800 s ceiling and no streaming.

### Phase 4 — polish (`S`)

- Light/dark palettes from the EthOS class, `ctx.notify` toasts on completion/failure.
- Host panel: KVM/TCG, cores, RAM, disk free on the VM root, engine version.
- Per-VM live chart (CPU %, RSS, disk IOPS, net throughput), 60 samples in memory.
- Errors surfaced verbatim from QMP (`"VM is not running"`), never a bare status code.

## Data model (package volume `/data`)

```jsonc
// vms.json (schema 1)
{"schema": 1, "vms": [{
  "id": "vm-8f2c1a", "name": "Windows 11", "status": "running",   // running|stopped|paused|error
  "config": {
    "vcpu": 2, "memory_mb": 4096, "machine": "q35", "firmware": "uefi",
    "disks":  [{"id": "d1", "path": "/vmhost/mnt/nas/ethos-vm/vm-8f2c1a/disk0.qcow2",
                "format": "qcow2", "bus": "virtio", "size_gb": 64}],
    "media":  [{"path": "/vmhost/mnt/nas/ethos-vm/iso/win11.iso", "bus": "sata"}],
    "nics":   [{"model": "virtio", "mac": "52:54:00:ab:cd:ef",
                "forwards": [{"host_port": 8100, "guest_port": 3389, "proto": "tcp"}]}],
    "display": {"type": "vnc", "port": 5901},
    "boot_order": ["disk", "cdrom"]
  },
  "autostart": true, "created_at": "…", "updated_at": "…"
}]}

// snapshots.json   {"schema":1,"by_vm":{"vm-8f2c1a":[{"tag":"before-update","name":"…","created_at":"…"}]}}
// tasks.json       {"schema":1,"tasks":[{"id":"t-…","kind":"export|clone|import","state":"running","progress":0.4}]}
// settings.json    {"schema":1,"secret":"<32B hex>","port_pool":[8100,8199],"roots":["/vmhost/home",…]}
```

All stores: write to `*.tmp` in the same directory, `os.replace()`, no `json.dump` straight onto the
live file. Lists are capped (16 snapshots/VM, 200 tasks, 500 log lines).

## Service API (package-local, reached through `ctx.proxy`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | `200` ready / `503` starting (engine still probing KVM) |
| GET | `/api/host` | KVM yes/no, cores, RAM, free space, engine/QEMU version |
| GET | `/api/vms` | list + live status |
| POST | `/api/vms` | create |
| GET/PUT | `/api/vms/{id}` | read / reconfigure (stopped only) |
| DELETE | `/api/vms/{id}?keep_disks=1` | delete |
| POST | `/api/vms/{id}/start\|shutdown\|stop` | lifecycle (MVP) |
| POST | `/api/vms/{id}/pause\|resume\|reset\|clone` | lifecycle (v1.1) |
| GET/POST/DELETE | `/api/vms/{id}/snapshots[/{tag}]` | snapshot CRUD (v1.1) |
| POST | `/api/vms/{id}/snapshots/{tag}/restore` | restore (v1.1) |
| GET | `/api/vms/{id}/stats` | CPU/RAM/IO/net series (v1.1) |
| POST | `/api/vms/{id}/export` | task: vmdk/vhdx/qcow2/OVA (v1.1) |
| GET | `/api/tasks/{id}` | task state + progress (v1.1) |
| GET/POST | `/api/storage/isos` | list / import ISO (chunked upload, MVP) |
| POST | `/api/console/ticket` | `{url, expires}` for the console iframe, HMAC over `{vm, exp}` |

`/data/settings.json:secret` is generated on first start; console URLs are
`http://<nas>:8094/console?vm=<id>&ticket=<hmac>` valid for 120 s (re-issued on each click). Every
other request is refused unless it came through the EthOS proxy — the LAN port serves the console
page only.

## Frontend wiring

- `ui: {"mode": "native", "entry": "frontend/app.js", "port": 8094}` — Packages renders the
  manifest identity; the module must not set the window title.
- Layout mirrors the AntiVirus/Domains rail: **VMs · Storage · Snapshots · Host · Settings**.
- VM card: status pill, vCPU/RAM/disk, quick actions, sparkline; detail drawer with **Console**
  (iframe to the ticketed URL), config, snapshots, ports, stats.
- `ctx.pickFolder` for the VM root and ISO selection, typed input as fallback.
- Polling every 4 s while the window is visible, paused when hidden.

## Security invariants

- `devices` limited to `/dev/kvm`; never `privileged`, `cap_add`, host network, docker socket.
- VM storage confined to `/vmhost/{home,mnt,media}/…`; paths come from the picker, are resolved, and
  re-checked against the allowlist (no `..`, no symlink escape).
- `subprocess` argument lists only, `shell=False`; QEMU argv built from a whitelist of fields.
- Port forwards only inside `8100-8199`; no arbitrary host port binding.
- Console tickets HMAC-signed, 120 s TTL, single VM; VNC itself listens on `127.0.0.1` inside the
  container only.
- No credentials in `vms.json`; no shell in the container exposed to the LAN.

## Risks

| Risk | Mitigation |
|---|---|
| No bridged networking (host network forbidden) | user-mode NAT + port-forward pool; document "no DHCP from LAN, no ICMP" |
| No per-VM memory cgroup | QEMU `-m` + host-level overcommit warning in the create form |
| Container update stops VMs (EthOS recreates containers) | SIGTERM powerdown + autostart replay; warn in the Packages update dialog |
| First start needs egress for `apt` | `.deb` cache lives in the `vmm-data` volume; a failed bootstrap keeps `/api/health` at `503` with a readable error instead of crash-looping |
| `dpkg -i` after every container recreation | `--download-only` cache + `--skip-same-version`; a few seconds, no network |
| Unclean shutdown (SIGKILL) leaves qcow2 dirty | qcow2 journal replay; optional `qemu-img check` in the VM doctor panel |
| `/dev/kvm` absent on the NAS | detect at start → TCG mode + visible warning |

## Status

Verified 2026-11-13 against `ethos-packages/vmm` @ `0d3271f` (package 1.0.0, signed index verified).

- [x] Phase 0 — `kvm` compose capability + tests + `docs/packages.md`
- [x] Phase 1 — bootstrap + engine API + console, VM boots, console reachable
- [x] Phase 2 — **MVP**: create wizard, ISO selection, start/stop/shutdown, autostart, recovery, native window
- [ ] Phase 3 — v1.1: port forwards, pause/resume, snapshots, clone, export/import, tasks
- [ ] Phase 4 — polish, stats charts, themes (partial: light/dark and host panel already ship)
- [x] Signed release in `ethos-packages` (`vmm` 1.0.0) + README catalog row
- [x] Parity check reported (Android: not applicable — no VM feature in the mobile apps)

### What was actually exercised

| Check | Result |
|---|---|
| Container on `debian:12-slim`, first start | ready in ~60 s, `accel: kvm`, QEMU 7.2.22 |
| Container recreated (package update) | ready in ~39 s, fully offline (`apt-get --no-download`) |
| Create VM | qcow2 disk created on the mounted share, MAC + VNC port allocated |
| Start VM | QMP reports `running`, QEMU daemonized with pidfile |
| Console | `/console` 200 with a ticket, 403 with a forged one, WS upgrade 101, noVNC assets 200 |
| Autostart after container recreate | VM came back `running` without intervention |
| Delete VM | directory and disk removed, runtime files cleaned |
| Package tests | `vmm/tests/test_qemu.py` 30/30 (argv, path scope, tickets, framing) |
| Compose policy | accepted with `capabilities: ["kvm"]`, rejected without it |

## Known gaps / next steps

- Per-user VM permissions and URL sharing need an EthOS user model inside packages (tracked as
  `TODO.md` F-03 / a future package v3 auth contract).
- Live migration, HA and replication to a second NAS require a multi-host design; the closest
  achievable feature is scheduled export to a remote mount.
- Virtual DSM has no open equivalent; offer "EthOS inside a VM" instead.
