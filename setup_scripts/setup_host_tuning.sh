#!/bin/bash
# setup_host_tuning.sh — optional host-level memory tuning for an EthOS NAS box
#
# The application protects itself (services/pressure_guard.py, unit limits in
# deploy/ethos-nas.service), but two host decisions stay with the administrator:
#
#   1. zram swap instead of relying only on the disk swap file — a full
#      /swap.img turns every reclaim into a disk stall and the next allocation
#      spike into an OOM kill.
#   2. systemd-oomd policy — Ubuntu kills the whole user session cgroup at 50 %
#      memory pressure (user@.service). With zram there is real headroom, so the
#      limit moves to 80 % and only genuine thrashing triggers a kill.
#
# Installs (root, idempotent):
#   /etc/default/zramswap                                  ← deploy/host-tuning/zramswap
#   /etc/sysctl.d/99-ethos-memory.conf                     ← deploy/host-tuning/99-ethos-memory.conf
#   /etc/systemd/system/user@.service.d/10-ethos-oomd.conf ← deploy/host-tuning/user@.service.d/
# and enables the `zramswap` service (package: zram-tools).
#
# This is a per-host, administrator-run script like setup_service.sh — it is NOT
# wired into EthOS provisioning or into any sudoers rule.
#
#   sudo bash setup_scripts/setup_host_tuning.sh            # install / update
#   sudo bash setup_scripts/setup_host_tuning.sh --dry-run  # show what would change
#   sudo bash setup_scripts/setup_host_tuning.sh --revert   # remove everything it installed

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: sudo bash setup_scripts/setup_host_tuning.sh [--dry-run|--revert]

  --dry-run   Print the actions without changing the system.
  --revert    Remove the tuning files, disable zram swap and reload sysctl.
USAGE
}

DRY_RUN=0
REVERT=0
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=1 ;;
        --revert) REVERT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: ${arg}" >&2; usage; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "${SCRIPT_DIR}")"
SRC="${REPO_DIR}/deploy/host-tuning"

ZRAM_CONF="/etc/default/zramswap"
SYSCTL_CONF="/etc/sysctl.d/99-ethos-memory.conf"
OOMD_DROPIN_DIR="/etc/systemd/system/user@.service.d"
OOMD_DROPIN="${OOMD_DROPIN_DIR}/10-ethos-oomd.conf"

info() { printf '  %s\n' "$*"; }
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

require_root() {
    if [ "${EUID}" -ne 0 ] && [ "${DRY_RUN}" -eq 0 ]; then
        echo "error: run as root (sudo bash setup_scripts/setup_host_tuning.sh)" >&2
        exit 1
    fi
}

revert() {
    echo "→ reverting host memory tuning"
    run systemctl disable --now zramswap.service || true
    if [ -e /dev/zram0 ]; then
        run swapoff /dev/zram0 || true
        run modprobe -r zram || true
    fi
    run rm -f "${ZRAM_CONF}" "${SYSCTL_CONF}" "${OOMD_DROPIN}"
    run rmdir "${OOMD_DROPIN_DIR}" 2>/dev/null || true
    run systemctl daemon-reload || true
    run sysctl --system >/dev/null || true
    info "removed; /swap.img remains the only swap device"
}

install_zram_tools() {
    if command -v zramctl >/dev/null 2>&1 && [ -f /usr/lib/systemd/system/zramswap.service ]; then
        info "zram-tools already installed"
        return 0
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        info "[dry-run] apt-get install -y --no-install-recommends zram-tools"
        return 0
    fi
    info "installing zram-tools (small, no daemon of its own)"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends zram-tools
}

verify() {
    echo
    echo "→ verification"
    echo "-- swap devices (zram should show PRIO 100):"
    swapon --show || true
    echo "-- zram stats:"
    zramctl 2>/dev/null || echo "  (zramctl not available)"
    echo "-- sysctl:"
    sysctl vm.swappiness vm.page-cluster vm.watermark_boost_factor vm.min_free_kbytes 2>/dev/null || true
    echo "-- oomd limit installed for user sessions:"
    grep -E '^ManagedOOMMemoryPressureLimit' "${OOMD_DROPIN}" 2>/dev/null || echo "  (drop-in missing)"
    local uid
    uid="$(id -u "${SUDO_USER:-$(id -u)}" 2>/dev/null || echo 1000)"
    systemctl show "user@${uid}.service" -p ManagedOOMMemoryPressureLimit 2>/dev/null \
        || echo "  (no running user manager for uid ${uid} — the drop-in applies on next login)"
    echo
    echo "Container note: limit swap as well as RAM (memswap_limit == mem_limit),"
    echo "otherwise a container's mem_limit still lets it fill the host swap."
}

main() {
    require_root

    if [ "${REVERT}" -eq 1 ]; then
        revert
        return 0
    fi

    for f in "${SRC}/zramswap" "${SRC}/99-ethos-memory.conf" "${SRC}/user@.service.d/10-ethos-oomd.conf"; do
        [ -f "${f}" ] || { echo "error: missing ${f}" >&2; exit 1; }
    done

    echo "→ host memory tuning (zram + oomd + sysctl)"
    install_zram_tools

    info "installing ${ZRAM_CONF}"
    run install -D -m 0644 "${SRC}/zramswap" "${ZRAM_CONF}"

    info "installing ${SYSCTL_CONF}"
    run install -D -m 0644 "${SRC}/99-ethos-memory.conf" "${SYSCTL_CONF}"

    info "installing ${OOMD_DROPIN}"
    run install -D -m 0644 "${SRC}/user@.service.d/10-ethos-oomd.conf" "${OOMD_DROPIN}"

    info "applying sysctl"
    if [ "${DRY_RUN}" -eq 1 ]; then
        info "[dry-run] sysctl --system"
    else
        sysctl --system >/dev/null
    fi

    info "starting zram swap"
    run systemctl daemon-reload
    run systemctl enable zramswap.service
    run systemctl restart zramswap.service

    if [ "${DRY_RUN}" -eq 0 ] && ! systemctl list-unit-files zramswap.service >/dev/null 2>&1; then
        echo "warning: zramswap.service not found — check the zram-tools package layout" >&2
    fi

    if [ "${DRY_RUN}" -eq 0 ]; then
        verify
    fi
    echo
    echo "done — the EthOS unit itself is already protected"
    echo "(MemoryMin/MemoryHigh/ManagedOOMPreference=avoid in deploy/ethos-nas.service)."
}

main "$@"
