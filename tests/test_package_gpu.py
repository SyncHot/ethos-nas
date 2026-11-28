#!/usr/bin/env python3
"""Host GPU probe, GPU compose override and `ui.embed` (no server).

The probe never runs against the real host here: the DRI/PCI/docker helpers are
monkeypatched so both a GPU-less NAS and an Intel/NVIDIA box are covered.
"""
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import gpu_host, package_gpu, package_installer, package_ports  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _no_dri():
    gpu_host.dri_nodes = lambda: []
    gpu_host.dri_cards = lambda: []
    gpu_host._card_details = lambda: []
    gpu_host._nvidia_nodes = lambda: []
    gpu_host._group_gid = lambda name: None
    gpu_host._device_gid = lambda path: None
    gpu_host._docker_runtimes = lambda: ["runc"]
    gpu_host.reset_cache()


def _intel_dri(openable=None):
    nodes = ["/dev/dri/renderD128"]
    gpu_host.dri_nodes = lambda: list(nodes)
    gpu_host.dri_cards = lambda: ["/dev/dri/card0"]
    gpu_host.dri_openable = openable or (lambda node: True)
    gpu_host._card_details = lambda: [
        {"device": "card0", "vendor": "intel", "vendor_id": "8086", "driver": "i915"}]
    gpu_host._nvidia_nodes = lambda: []
    gpu_host._group_gid = lambda name: {"render": 990, "video": 44}.get(name)
    gpu_host._device_gid = lambda path: None
    gpu_host._docker_runtimes = lambda: ["runc"]
    gpu_host.reset_cache()


def _nvidia(runtime=True, driver=True):
    gpu_host.dri_nodes = lambda: []
    gpu_host.dri_cards = lambda: []
    gpu_host._card_details = lambda: []
    gpu_host.dri_openable = lambda node: False
    gpu_host._nvidia_nodes = lambda: ["/dev/nvidia0", "/dev/nvidiactl"]
    gpu_host._group_gid = lambda name: None
    gpu_host._device_gid = lambda path: None
    gpu_host._docker_runtimes = lambda: (["runc", "nvidia"] if runtime else ["runc"])
    gpu_host.system_deps.has_binary = (lambda name: driver) if driver else (lambda name: False)
    gpu_host.reset_cache()


ORIGINAL_HAS_BINARY = gpu_host.system_deps.has_binary
ORIGINAL_DRI_OPENABLE = gpu_host.dri_openable

print("Host probe — Intel iGPU")
_intel_dri()
gpu = gpu_host.probe(refresh=True)
check("openable render node reports available", gpu["dri"]["available"] is True)
check("render node listed", gpu["dri"]["nodes"] == ["/dev/dri/renderD128"])
check("vendor decoded from sysfs", gpu["dri"]["vendor"] == "intel", str(gpu["dri"]["vendor"]))
check("render/video group ids exposed",
      gpu["dri"]["render_gid"] == 990 and gpu["dri"]["video_gid"] == 44)
check("no reason when usable", gpu["dri"]["reason"] == "")
_env = gpu_host.compose_env()
check("compose env exports the render gid", _env.get("ETHOS_RENDER_GID") == "990", str(_env))
check("compose env exports VAAPI flag", _env.get("ETHOS_GPU_VAAPI") == "1")
check("compose env reports no nvidia", _env.get("ETHOS_GPU_NVIDIA") == "0")

print("Host probe — node present but unreadable")
_intel_dri(openable=lambda node: False)
gpu = gpu_host.probe(refresh=True)
check("unreadable render node is not available", gpu["dri"]["available"] is False)
check("reason names the render group fix", "usermod -aG render,video" in gpu["dri"]["reason"],
      gpu["dri"]["reason"])

print("Host probe — no GPU at all")
_no_dri()
gpu = gpu_host.probe(refresh=True)
check("no dri node means unavailable", gpu["dri"]["available"] is False)
check("reason explains the missing node", "no /dev/dri" in gpu["dri"]["reason"])
check("nvidia absent", gpu["nvidia"]["available"] is False)
check("nvidia hint tells the operator what to run",
      "nvidia-container-toolkit" in gpu["nvidia"]["hint"])

print("Host probe — NVIDIA")
_nvidia(runtime=True)
gpu = gpu_host.probe(refresh=True)
check("driver + runtime means available", gpu["nvidia"]["available"] is True)
check("toolkit detected", gpu["nvidia"]["toolkit"] is True)
check("hint empty when ready", gpu["nvidia"]["hint"] == "")
_nvidia(runtime=False)
gpu = gpu_host.probe(refresh=True)
check("missing nvidia runtime blocks the pass-through", gpu["nvidia"]["available"] is False)
check("reason explains the runtime", "nvidia-ctk runtime configure" in gpu["nvidia"]["reason"],
      gpu["nvidia"]["reason"])
_nvidia(runtime=True, driver=False)
gpu = gpu_host.probe(refresh=True)
check("missing driver blocks the pass-through", gpu["nvidia"]["available"] is False)
gpu_host.system_deps.has_binary = ORIGINAL_HAS_BINARY
gpu_host.dri_openable = ORIGINAL_DRI_OPENABLE

print("Override fragment")
_intel_dri()
intel_dri = gpu_host.probe(refresh=True)
INTEL_MANIFEST = {"capabilities": ["compose", "dri"]}
NVIDIA_MANIFEST = {"capabilities": ["compose", "nvidia"]}
BOTH_MANIFEST = {"capabilities": ["compose", "dri", "nvidia"]}

frag = package_gpu.fragment(INTEL_MANIFEST, intel_dri)
check("dri fragment passes the render node", frag.get("devices") == ["/dev/dri/renderD128"], str(frag))
check("dri fragment joins the render/video groups",
      frag.get("group_add") == ["990", "44"], str(frag))
check("dri fragment sets no runtime", "runtime" not in frag and "deploy" not in frag)
check("no capability, no fragment", package_gpu.fragment({"capabilities": ["compose"]}, intel_dri) == {})

_no_dri()
no_gpu = gpu_host.probe(refresh=True)
check("no hardware, no fragment", package_gpu.fragment(INTEL_MANIFEST, no_gpu) == {})
summary = package_gpu.summary(INTEL_MANIFEST, no_gpu)
check("summary records the request without applying it",
      summary["requested"] == ["dri"] and summary["applied"] is False, str(summary))
check("summary explains why", "no /dev/dri" in summary["reason"], str(summary))
check("summary is empty for packages without GPU capabilities",
      package_gpu.summary({"capabilities": ["compose"]}, no_gpu) == {})
lines = package_gpu.log_lines(INTEL_MANIFEST, summary)
check("job log says software transcoding", "software transcoding" in lines[0], str(lines))

_nvidia(runtime=True)
nvidia_facts = gpu_host.probe(refresh=True)
frag = package_gpu.fragment(NVIDIA_MANIFEST, nvidia_facts)
check("nvidia fragment sets the runtime", frag.get("runtime") == "nvidia", str(frag))
check("nvidia fragment reserves the GPU",
      frag["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia", str(frag))
check("nvidia fragment passes the device nodes",
      frag.get("devices") == ["/dev/nvidia0", "/dev/nvidiactl"], str(frag))
check("nvidia log line names the runtime",
      "runtime nvidia" in " ".join(package_gpu.log_lines(NVIDIA_MANIFEST, package_gpu.summary(
          NVIDIA_MANIFEST, nvidia_facts))))
gpu_host.system_deps.has_binary = ORIGINAL_HAS_BINARY

_intel_dri()
_intel_now = gpu_host.probe(refresh=True)
both_facts = {**_intel_now, "nvidia": nvidia_facts["nvidia"]}
frag = package_gpu.fragment(BOTH_MANIFEST, both_facts)
check("both capabilities combine", frag.get("runtime") == "nvidia" and "/dev/dri/renderD128" in frag["devices"], str(frag))

print("Compose override")
COMPOSE = {"services": {"media": {"image": "jellyfin/jellyfin:10.10.7", "ports": ["8096:8096"]}}}


def _load(text):
    """Parse the generated override; `!override` is a Compose tag, not a YAML type."""
    return yaml.safe_load(text.replace("ports: !override", "ports:"))


plain = package_ports.build_override(COMPOSE, {}, None)
check("no addition and no remap means no override file", plain == "")
with_gpu = package_ports.build_override(COMPOSE, {}, {"devices": ["/dev/dri/renderD128"],
                                                      "group_add": ["990", "44"]})
parsed = _load(with_gpu)
check("override puts the device on the service",
      parsed["services"]["media"]["devices"] == ["/dev/dri/renderD128"], with_gpu)
check("override keeps group_add numeric",
      parsed["services"]["media"]["group_add"] == ["990", "44"], with_gpu)
check("override header warns about regeneration", with_gpu.startswith("# Generated by EthOS"))
mapped = package_ports.build_override(COMPOSE, {8096: 18096},
                                      {"devices": ["/dev/dri/renderD128"]})
parsed = _load(mapped)
check("port remap and GPU section coexist",
      parsed["services"]["media"]["ports"] == ["18096:8096"]
      and parsed["services"]["media"]["devices"] == ["/dev/dri/renderD128"], mapped)
_gpu_only = package_ports.build_override(COMPOSE, {}, {"runtime": "nvidia"})
check("runtime survives the round trip",
      _load(_gpu_only)["services"]["media"]["runtime"] == "nvidia", _gpu_only)

check("recorded mapping needs a remap",
      package_installer._recorded_mapping({}) == {}
      and package_installer._recorded_mapping({"port_remap": {"from": 8096, "to": 8096}}) == {})
check("recorded mapping is rebuilt from the record",
      package_installer._recorded_mapping(
          {"port_remap": {"from": 8096, "to": 18096, "mapping": {"8096": "18096", "7359": "17359"}}})
      == {8096: 18096, 7359: 17359})

print("Installer wiring")
with tempfile.TemporaryDirectory() as tmp:
    package_dir = Path(tmp)
    (package_dir / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE), encoding="utf-8")
    manifest = {**INTEL_MANIFEST, "id": "jellyfin"}
    text, state = package_installer._write_overrides("jellyfin", manifest, package_dir, {})
    written = (package_dir / package_ports.OVERRIDE_NAME).read_text(encoding="utf-8")
    check("install writes the GPU override", written == text and "/dev/dri/renderD128" in written,
          written)
    check("install records the applied GPU state",
          state["applied"] is True and state["devices"] == ["/dev/dri/renderD128"], str(state))

    _no_dri()
    text, state = package_installer._write_overrides("jellyfin", manifest, package_dir, {})
    check("a GPU-less host removes the override",
          not (package_dir / package_ports.OVERRIDE_NAME).exists() and text == "")
    check("and says so in the record", state["applied"] is False)

    _intel_dri()
    from backend.services import packages_store
    original_package_dir = packages_store.package_dir
    packages_store.package_dir = lambda _pkg_id: package_dir  # the temp dir is the package dir
    try:
        changed, state = package_installer._refresh_gpu_override("jellyfin", manifest)
        check("start re-detects a GPU that appeared later",
              changed is True and (package_dir / package_ports.OVERRIDE_NAME).exists(), str(state))
        changed, _ = package_installer._refresh_gpu_override("jellyfin", manifest)
        check("a second refresh is a no-op", changed is False)
        check("packages without GPU capabilities are left alone",
              package_installer._refresh_gpu_override("other", {"capabilities": ["compose"]})
              == (False, {}))
    finally:
        packages_store.package_dir = original_package_dir

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
