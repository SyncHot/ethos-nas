#!/bin/bash
# setup_service.sh — EthOS NAS dedicated service user + consolidated sudoers installer
#
# Installs everything needed to run EthOS NAS as a dedicated unprivileged
# system account with narrow NOPASSWD privileges (no broad sudo, no root):
#
#   1. System user `ethos-nas` (nologin shell) — or pass [user] for dev use
#   2. Ownership of runtime state ONLY: backend/data/ + users.json stores
#      (code and .venv stay owned by the developer so git keeps working)
#   2b. NAS share write access — ACL entries for the service user on all dirs
#       under ETHOS_SHARES (default /mnt/SharedDrive; empty disables), same pattern as other
#       services' named-user ACLs; without this, apps like Download Station
#       cannot save into shares owned by the developer account
#   3. ONE sudoers file /etc/sudoers.d/ethos-nas with the full narrow rule
#      inventory (shares + firewall + USB + power), visudo-validated before install
#   4. Privileged helpers /usr/local/bin/ethos-usb-partition (USB only),
#      /usr/local/bin/ethos-docker-install (Docker Engine provisioning), and
#      /usr/local/bin/ethos-samba-accounts (fixed Samba passdb operations), plus
#      shadow-group membership (pam_unix needs /etc/shadow to verify PAM logins)
#   5. Docker group membership when /var/run/docker.sock exists (no sudo for docker).
#      ETHOS_INSTALL_DOCKER=1 installs Docker Engine first, so one restart covers it.
#   6. Removes the old per-feature files ethos-nas-{shares,firewall,usb}
#   7. Installs deploy/ethos-nas.service as a systemd unit and starts it
#      with paths adapted to this checkout
#
# Supersedes setup_sudoers.sh / setup_sudoers_firewall.sh / setup_sudoers_usb.sh
# (kept in the repo for reference; marked deprecated).
#
# Sudoers syntax notes (must work under GNU sudo AND sudo-rs):
#   - No wildcards in command ARGUMENTS (rejected by sudo-rs and strict visudo);
#     only exact paths/arguments, or argument-free rules where the security
#     boundary lives inside a validated helper binary.
#   - No `Defaults:<user>` — ':' is not a valid Defaults scope character.
#
# Requires root. Idempotent — safe to re-run.
#
#   sudo bash setup_scripts/setup_service.sh              # install as ethos-nas
#   sudo bash setup_scripts/setup_service.sh devuser      # target a different user (dev)
#   sudo bash setup_scripts/setup_service.sh --uninstall  # remove unit+sudoers+helper (keeps user)

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: sudo bash setup_scripts/setup_service.sh [user] [--uninstall]

  user         Target service account (default: ethos-nas). Created as a system
               user with a nologin shell if it does not exist.
  --uninstall  Remove the systemd unit, /etc/sudoers.d/ethos-nas and the
               installed helpers. The user itself is kept.
USAGE
}

UNINSTALL=0
TARGET_USER="ethos-nas"
POSITIONAL_SET=0

for arg in "$@"; do
    case "${arg}" in
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "ERROR: unknown option '${arg}'" >&2; usage >&2; exit 1 ;;
        *) if [ "${POSITIONAL_SET}" = 1 ]; then
               echo "ERROR: unexpected argument '${arg}' (only one [user] allowed)" >&2
               exit 1
           fi
           TARGET_USER="${arg}"; POSITIONAL_SET=1 ;;
    esac
done

if [ "$(id -u)" != "0" ]; then
    echo "ERROR: must run as root — sudo bash setup_scripts/setup_service.sh $*" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUDOERS_DIR="/etc/sudoers.d"
SUDOERS_FILE="${SUDOERS_DIR}/ethos-nas"
WRAPPER_BIN="/usr/local/bin/ethos-usb-partition"
DOCKER_HELPER_BIN="/usr/local/bin/ethos-docker-install"
SYSTEM_DEPS_HELPER_BIN="/usr/local/bin/ethos-system-deps"
SAMBA_ACCOUNTS_HELPER_BIN="/usr/local/bin/ethos-samba-accounts"
# Core update helper (deploy/ethos-update). The sudoers rule is argument-free;
# the helper validates the signed release index and its own conf file.
UPDATE_HELPER_BIN="/usr/local/bin/ethos-update"
UPDATE_CONF="/etc/ethos-nas/update.conf"
UPDATE_PUBKEY_PEM="/etc/ethos-nas/update-pubkey.pem"
# Ed25519 release public key (DER/SPKI, base64) — same key as backend/config.py
# RELEASE_PUBKEY_B64; the private half never leaves the maintainer's machine.
UPDATE_PUBKEY_DER_B64="MCowBQYDK2VwAyEAsXiL7v0droY2BVyttuDt0Bqq6jB7MpknylJo+/iavqE="
UPDATE_INDEX_URL="${ETHOS_UPDATE_INDEX_URL:-https://raw.githubusercontent.com/SyncHot/ethos-nas/master/releases/index.json}"
# Host requirements EthOS may install/remove on a package's behalf. Must match
# SYSTEM_REQUIREMENTS in backend/services/system_deps.py (guarded by
# tests/test_system_deps_helper.py); `docker` is absent — it has its own helper.
SYSTEM_DEPS_IDS="samba"
UNIT_SRC="${REPO_DIR}/deploy/ethos-nas.service"
UNIT_DST="/etc/systemd/system/ethos-nas.service"

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "${UNINSTALL}" = 1 ]; then
    if systemctl list-unit-files 2>/dev/null | grep -q '^ethos-nas\.service'; then
        echo "→ stopping and disabling ethos-nas.service"
        systemctl disable --now ethos-nas || true
    fi
    rm -f "${UNIT_DST}"
    systemctl daemon-reload
    rm -f "${SUDOERS_FILE}"
    rm -f "${WRAPPER_BIN}"
    rm -f "${DOCKER_HELPER_BIN}"
    rm -f "${SYSTEM_DEPS_HELPER_BIN}"
    rm -f "${SAMBA_ACCOUNTS_HELPER_BIN}"
    rm -f "${UPDATE_HELPER_BIN}" "${UPDATE_CONF}" "${UPDATE_PUBKEY_PEM}"
    echo "✓ removed: ${UNIT_DST}, ${SUDOERS_FILE}, ${WRAPPER_BIN}, ${DOCKER_HELPER_BIN}, ${SYSTEM_DEPS_HELPER_BIN}, ${SAMBA_ACCOUNTS_HELPER_BIN}, ${UPDATE_HELPER_BIN}"
    echo "  user '${TARGET_USER}' kept (remove manually with: userdel ${TARGET_USER})"
    exit 0
fi

# Never terminate an unknown service or partially install over its port. A
# running EthOS service is safe to replace; setup will restart it below.
if ! command -v fuser >/dev/null 2>&1; then
    echo "ERROR: fuser is required to check port 9000; install psmisc and retry." >&2
    exit 1
fi
if fuser 9000/tcp >/dev/null 2>&1 && ! systemctl is-active --quiet ethos-nas; then
    echo "ERROR: port 9000 is already in use; stop or reconfigure that service, then retry." >&2
    exit 1
fi

# ── 1. System user ───────────────────────────────────────────────────────────
if getent passwd "${TARGET_USER}" >/dev/null; then
    echo "✓ system user '${TARGET_USER}' already exists"
else
    NOLOGIN_BIN="$(command -v nologin || true)"
    [ -n "${NOLOGIN_BIN}" ] || NOLOGIN_BIN="/usr/sbin/nologin"
    useradd --system --shell "${NOLOGIN_BIN}" "${TARGET_USER}"
    echo "✓ created system user '${TARGET_USER}' (nologin shell)"
fi

# ── 2. Runtime state ownership (NOT the whole repo — git must keep working) ──
mkdir -p "${REPO_DIR}/backend/data"
chown -R "${TARGET_USER}:${TARGET_USER}" "${REPO_DIR}/backend/data"
echo "✓ chowned backend/data/ → ${TARGET_USER}"

# PAM-seeded account stores live outside DATA_DIR and are written in place.
for f in "${REPO_DIR}/backend/users.json" "${REPO_DIR}/backend/routers/users.json"; do
    if [ ! -f "${f}" ]; then
        echo '[]' > "${f}"   # pre-create so the service user can write on first login
    fi
    chown "${TARGET_USER}:${TARGET_USER}" "${f}"
done
echo "✓ users.json stores owned by ${TARGET_USER}"

# ── 2b. NAS share write access (ACLs for the service user) ───────────────────
# The server runs unprivileged, so shares owned by the developer account are
# not writable out of the box. Grant named-user ACL entries on directories
# only (files created by the service itself need no entry; default entries
# propagate to new subdirs). Non-fatal: some filesystems don't support ACLs.
SHARES="${ETHOS_SHARES-/mnt/SharedDrive}"
for share in ${SHARES}; do
    if [ ! -d "${share}" ]; then
        echo "· skipping ${share} (not a directory)"
        continue
    fi
    if find "${share}" -type d -exec setfacl -m "u:${TARGET_USER}:rwx" -dm "u:${TARGET_USER}:rwx" {} + 2>/dev/null; then
        echo "✓ ACLs: ${TARGET_USER} rwx on all dirs under ${share}"
    else
        echo "! ACLs: failed on some dirs under ${share} (non-fatal)"
    fi
done

# ── 2c. Home ACLs (File Station / code editor access) ───────────────────────
# All file-manager operations run as ${TARGET_USER} (the service user), NOT as
# the logged-in PAM user. Grant it rwx on the admin user's home so the File
# Station and code editor can create/edit files there. Dot-dirs (.ssh, .gnupg,
# .config, .local, …) are excluded on purpose — they stay private. Default
# ACLs make newly created files/dirs inherit the grant (the owner's umask
# must be ≤ 0002, which is the default for interactive shells).
if [ "${ETHOS_GRANT_HOME_ACL:-1}" = "1" ]; then
    ADMIN_USER="${ETHOS_ADMIN_USER:-${SUDO_USER:-}}"
    if [ -z "${ADMIN_USER}" ]; then
        ADMIN_USER="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65000 {print $1; exit}')"
    fi
    ADMIN_HOME="$(getent passwd "${ADMIN_USER:-}" | cut -d: -f6)"
    if [ -n "${ADMIN_HOME}" ] && [ -d "${ADMIN_HOME}" ] && command -v setfacl >/dev/null 2>&1; then
        echo "→ home ACLs: granting ${TARGET_USER} rwx on ${ADMIN_HOME} (non-dot entries) — may take a while"
        setfacl -m "u:${TARGET_USER}:rwx" -dm "u:${TARGET_USER}:rwx" "${ADMIN_HOME}" 2>/dev/null || true
        if find "${ADMIN_HOME}" -mindepth 1 -maxdepth 1 ! -name '.*' \
                -exec setfacl -R -m "u:${TARGET_USER}:rwx" -dm "u:${TARGET_USER}:rwx" {} + 2>/dev/null; then
            echo "✓ home ACLs: ${TARGET_USER} rwx on ${ADMIN_HOME} (non-dot entries; dot-dirs excluded)"
        else
            echo "! home ACLs: some entries failed (root-owned or ACL-unsupported) — non-fatal"
        fi
    else
        echo "· no admin home to grant ACLs on (set ETHOS_ADMIN_USER) — skipping home ACLs"
    fi
else
    echo "· home ACL changes disabled (set ETHOS_GRANT_HOME_ACL=1 to enable)"
fi

# ── 3. Consolidated sudoers file (visudo-validated before install) ───────────
if [ ! -d "${SUDOERS_DIR}" ]; then
    echo "ERROR: ${SUDOERS_DIR} not found on this system" >&2
    exit 1
fi

TEE_BIN="$(command -v tee || true)"
SYSTEMCTL_BIN="$(command -v systemctl || true)"
UFW_BIN="$(command -v ufw || true)"
JOURNALCTL_BIN="$(command -v journalctl || true)"
IPTABLES_BIN="$(command -v iptables || true)"
IPTABLES_SAVE_BIN="$(command -v iptables-save || true)"
CAT_BIN="$(command -v cat || true)"

if [ -z "${TEE_BIN}" ] || [ -z "${SYSTEMCTL_BIN}" ]; then
    echo "ERROR: could not locate tee or systemctl" >&2
    exit 1
fi

RULES=(
    # Shares — smb.conf writes + graceful reload (see services/shares_service.py)
    "${TARGET_USER} ALL=(root) NOPASSWD: ${TEE_BIN} /etc/samba/smb.conf"
    "${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} reload samba"
    "${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} reload smbd"
    # Samba passdb operations accept a strict JSON request on stdin; the root
    # helper validates the action and existing Unix account before invoking tools.
    "${TARGET_USER} ALL=(root) NOPASSWD: ${SAMBA_ACCOUNTS_HELPER_BIN}"
    # Power — system buttons (see routers/system.py; no wildcards in args)
    "${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} reboot"
    "${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} shutdown"
    # USB — managed fstab block via tee + validated helper (see services/usb_*_service.py).
    # The helper rule is argument-free on purpose: a sudoers entry with no arguments
    # allows any arguments, and that form is accepted by both classic sudo and
    # sudo-rs. The security boundary stays inside the helper (USB transport check).
    "${TARGET_USER} ALL=(root) NOPASSWD: ${TEE_BIN} /etc/fstab"
    "${TARGET_USER} ALL=(root) NOPASSWD: ${WRAPPER_BIN}"
    # Docker provisioning — same argument-free form: the helper takes no
    # parameters and only installs Docker Engine/Compose from Docker's repo
    # (see services/docker_host.py). Container access still uses the group.
    "${TARGET_USER} ALL=(root) NOPASSWD: ${DOCKER_HELPER_BIN}"
    # Host dependencies (services/system_deps.py). sudoers forbids wildcards in
    # arguments, so every allowed (action, id) pair gets its own exact rule.
    # The helper re-validates the id and never takes an arbitrary package name.
)
for _req in ${SYSTEM_DEPS_IDS}; do
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEM_DEPS_HELPER_BIN} install ${_req}")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${SYSTEM_DEPS_HELPER_BIN} remove ${_req}")
done
# Core self-update (deploy/ethos-update): argument-free by design — the helper
# verifies the signed release index and only then touches git/pip/systemd.
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${UPDATE_HELPER_BIN}")
if [ -n "${UFW_BIN}" ]; then
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${UFW_BIN}")
fi
if [ -n "${IPTABLES_BIN}" ]; then
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${IPTABLES_BIN}")
fi
if [ -n "${IPTABLES_SAVE_BIN}" ]; then
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${IPTABLES_SAVE_BIN}")
fi
if [ -n "${JOURNALCTL_BIN}" ]; then
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${JOURNALCTL_BIN}")
fi
# auth.log + kernel log read fallback (when the service user is not in the adm
# group): sudoers forbids wildcards in command arguments, so rotated files are
# enumerated. kern.log/syslog carry the `UFW BLOCK` events shown in the
# Firewall app's Intrusion Attempts tab — without them (and without adm group
# membership) that tab silently shows "no blocked packets".
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/auth.log")
for n in $(seq 1 10); do
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/auth.log.${n}")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/auth.log.${n}.gz")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/kern.log.${n}")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/kern.log.${n}.gz")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/syslog.${n}")
    RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/syslog.${n}.gz")
done
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/kern.log")
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /var/log/syslog")
# UFW rule files — lists baseline rules while the firewall is inactive (the
# ban list and "do not duplicate these rules" checks read them).
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /etc/ufw/user.rules")
RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${CAT_BIN} /etc/ufw/user6.rules")

TMP_FILE="$(mktemp "${SUDOERS_DIR}/.ethos-nas.XXXXXX")"
trap 'rm -f "${TMP_FILE}"' EXIT

{
    echo "# EthOS NAS — consolidated restricted NOPASSWD rules (service user: ${TARGET_USER})"
    echo "# Installed by setup_scripts/setup_service.sh — do not edit by hand."
    echo "# ONLY the commands listed below are allowed — no shell access, no"
    echo "# arbitrary root, no arbitrary files. Docker uses group membership,"
    echo "# not sudo. Re-run the installer to regenerate this file."
    for line in "${RULES[@]}"; do
        echo "${line}"
    done
} > "${TMP_FILE}"

# Validate syntax before installing (critical — bad sudoers = locked out!)
if ! visudo -cf "${TMP_FILE}" >/dev/null 2>&1; then
    echo "ERROR: generated sudoers failed visudo validation — NOT installed." >&2
    echo "       Temporary file kept for inspection: ${TMP_FILE}" >&2
    exit 1
fi

install -m 0440 -o root -g root "${TMP_FILE}" "${SUDOERS_FILE}"
rm -f "${TMP_FILE}"
trap - EXIT
echo "✓ ${SUDOERS_FILE} installed (visudo-validated, ${#RULES[@]} rules)"

# ── 4. Privileged USB helper (argument-free sudoers rule; validation inside) ─
WRAPPER_TMP="$(mktemp)"
cat > "${WRAPPER_TMP}" <<'EOF'
#!/bin/bash
# ethos-usb-partition — privileged helper for EthOS NAS USB partition ops.
# Installed by setup_scripts/setup_service.sh. Invoked via sudo:
#
#   sudo /usr/local/bin/ethos-usb-partition <device> -- <command...>
#
# Validates that <device> is a real USB-transport storage unit, then execs
# the command as root. Nothing else is reachable through this helper.
set -euo pipefail

if [ "$(id -u)" != "0" ]; then
    echo "ethos-usb-partition: must run as root (via sudo)" >&2
    exit 1
fi

dev="${1:-}"
shift || true
if [ "${1:-}" != "--" ] || [ $# -lt 2 ]; then
    echo "ethos-usb-partition: usage: <device> -- <command...>" >&2
    exit 1
fi
shift

case "$dev" in
    /dev/sd[a-z]*|/dev/vd[a-z]*|/dev/xvd[a-z]*|/dev/nvme[0-9]n[0-9]*|/dev/mmcblk[0-9]*) ;;
    *) echo "ethos-usb-partition: invalid device name: $dev" >&2; exit 1 ;;
esac

name="${dev#/dev/}"
# TRAN is reported on the whole disk, not on partitions — resolve parent
# (PKNAME has no /dev/ prefix; lsblk needs the full path).
parent="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -n1 || true)"
case "${parent}" in
    /*) check_dev="${parent}" ;;
    "") check_dev="${dev}" ;;
    *) check_dev="/dev/${parent}" ;;
esac
tran="$(lsblk -no TRAN "$check_dev" 2>/dev/null | head -n1 || true)"
if [ "$tran" != "usb" ]; then
    echo "ethos-usb-partition: $dev is not a USB device (transport: ${tran:-unknown})" >&2
    exit 1
fi

exec "$@"
EOF
install -m 0755 -o root -g root "${WRAPPER_TMP}" "${WRAPPER_BIN}"
rm -f "${WRAPPER_TMP}"
echo "✓ ${WRAPPER_BIN} installed (USB-validated partition helper)"

# ── 4b. Shadow group (PAM password verification) ────────────────────────────
# pam_unix reads /etc/shadow (root:shadow 640). Without shadow membership the
# service user cannot verify ANY PAM login — the web UI would be unusable.
if getent group shadow >/dev/null; then
    if id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -qx "shadow"; then
        echo "✓ ${TARGET_USER} already in shadow group"
    else
        usermod -aG shadow "${TARGET_USER}"
        echo "✓ added ${TARGET_USER} to shadow group (effective after service restart)"
    fi
else
    echo "· no shadow group on this system — skipping"
fi

# ── 4c. adm group (read /var/log/*) ────────────────────────────────────────
# kern.log (UFW BLOCK events) and auth.log are root:adm 0640. Membership lets
# the Firewall app read them directly instead of relying on journald.
if getent group adm >/dev/null; then
    if id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -qx "adm"; then
        echo "✓ ${TARGET_USER} already in adm group"
    else
        usermod -aG adm "${TARGET_USER}"
        echo "✓ added ${TARGET_USER} to adm group (effective after service restart)"
    fi
else
    echo "· no adm group on this system — skipping"
fi

# ── 4d. Docker provisioning helper (argument-free sudoers rule) ─────────────
# Docker Engine needs root to install, but the service user must not get apt.
# This helper is the only privileged path: no arguments, fixed package list,
# fixed repository, and it refuses to run on unsupported systems.
case "${TARGET_USER}" in
    ""|*[!A-Za-z0-9_-]*) echo "ERROR: unsupported user name: ${TARGET_USER}" >&2; exit 1 ;;
esac

DOCKER_TMP="$(mktemp)"
cat > "${DOCKER_TMP}" <<'EOF'
#!/bin/bash
# ethos-docker-install — privileged helper for EthOS NAS Docker provisioning.
# Installed by setup_scripts/setup_service.sh. Invoked via sudo with NO arguments:
#
#   sudo /usr/local/bin/ethos-docker-install
#
# Installs Docker Engine + Compose v2 from Docker's official apt repository and
# adds the EthOS service account to the docker group. Idempotent: a second run
# is a no-op when Docker already answers. Nothing else is reachable here.
set -euo pipefail

SERVICE_USER="__ETHOS_SERVICE_USER__"
KEYRING="/etc/apt/keyrings/docker.asc"
SOURCES="/etc/apt/sources.list.d/docker.list"
# Docker's release signing key, pinned by fingerprint (verified after download).
KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"

if [ "$(id -u)" -ne 0 ]; then
    echo "ethos-docker-install: must run as root (via sudo)" >&2
    exit 1
fi
if [ "$#" -ne 0 ]; then
    echo "ethos-docker-install: this helper takes no arguments" >&2
    exit 2
fi

# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
case "${ID:-}" in
    ubuntu|debian) ;;
    *) echo "ethos-docker-install: unsupported distribution: ${ID:-unknown}" >&2; exit 3 ;;
esac

# Already working? Make sure group membership exists, then stop.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    echo "OK docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown) compose=$(docker compose version --short 2>/dev/null || echo unknown)"
    if getent group docker >/dev/null 2>&1; then
        usermod -aG docker "${SERVICE_USER}"
    fi
    echo "RESTART_REQUIRED=1"
    exit 0
fi

# Another engine owns the command or its state — never remove it silently.
if command -v podman >/dev/null 2>&1; then
    echo "ethos-docker-install: podman provides a conflicting docker CLI shim; remove it first" >&2
    exit 4
fi
if dpkg -s docker.io >/dev/null 2>&1; then
    echo "ethos-docker-install: the distribution package docker.io is installed; remove it (sudo apt-get remove docker.io) and retry" >&2
    exit 4
fi
if [ -e /var/lib/docker ] && ! command -v dockerd >/dev/null 2>&1; then
    echo "ethos-docker-install: /var/lib/docker exists but no Docker Engine is installed; inspect it manually" >&2
    exit 4
fi

case "$(uname -m)" in
    x86_64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "ethos-docker-install: unsupported architecture: $(uname -m)" >&2; exit 5 ;;
esac

CODENAME="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
if [ -z "${CODENAME}" ]; then
    echo "ethos-docker-install: cannot determine the release codename from /etc/os-release" >&2
    exit 5
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
KEY_TMP="$(mktemp)"
trap 'rm -f "${KEY_TMP}"' EXIT
curl -fsSL --proto '=https' --tlsv1.2 "https://download.docker.com/linux/${ID}/gpg" -o "${KEY_TMP}"
GOT_FPR="$(gpg --batch --with-colons --show-keys "${KEY_TMP}" | awk -F: '/^fpr:/{print $10; exit}')"
if [ "${GOT_FPR}" != "${KEY_FPR}" ]; then
    echo "ethos-docker-install: Docker repository key fingerprint mismatch (got ${GOT_FPR:-none})" >&2
    exit 6
fi
gpg --batch --yes --dearmor -o "${KEYRING}" "${KEY_TMP}"
chmod 0644 "${KEYRING}"
rm -f "${KEY_TMP}"
trap - EXIT

echo "deb [arch=${ARCH} signed-by=${KEYRING}] https://download.docker.com/linux/${ID} ${CODENAME} stable" > "${SOURCES}"
apt-get update
# shellcheck disable=SC2086
apt-get install -y ${PACKAGES}
systemctl enable --now docker

if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "${SERVICE_USER}"
fi

echo "OK docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown) compose=$(docker compose version --short 2>/dev/null || echo unknown)"
echo "RESTART_REQUIRED=1"
EOF
sed -i "s|__ETHOS_SERVICE_USER__|${TARGET_USER}|g" "${DOCKER_TMP}"
install -m 0755 -o root -g root "${DOCKER_TMP}" "${DOCKER_HELPER_BIN}"
rm -f "${DOCKER_TMP}"
echo "✓ ${DOCKER_HELPER_BIN} installed (Docker Engine provisioning helper)"

# ── 4d. Host dependency helper (install/remove per system_requires) ─────────
SYSTEM_DEPS_SRC="${REPO_DIR}/deploy/ethos-system-deps"
if [ -f "${SYSTEM_DEPS_SRC}" ]; then
    install -m 0755 -o root -g root "${SYSTEM_DEPS_SRC}" "${SYSTEM_DEPS_HELPER_BIN}"
    echo "✓ ${SYSTEM_DEPS_HELPER_BIN} installed (host dependency helper: ${SYSTEM_DEPS_IDS})"
fi

# ── 4d-ter. Samba passdb helper (fixed operations; JSON request via stdin) ──
SAMBA_ACCOUNTS_SRC="${REPO_DIR}/deploy/ethos-samba-accounts"
if [ -f "${SAMBA_ACCOUNTS_SRC}" ]; then
    install -m 0755 -o root -g root "${SAMBA_ACCOUNTS_SRC}" "${SAMBA_ACCOUNTS_HELPER_BIN}"
    echo "✓ ${SAMBA_ACCOUNTS_HELPER_BIN} installed (Samba passdb helper)"
fi

# ── 4d-bis. Core update helper + its config and public key ──────────────────
# The helper (deploy/ethos-update) may only fetch and apply the *signed* release
# index. Its conf file lists the checkout, the repo owner and the log/data dir,
# so the web UI cannot redirect an update anywhere else.
UPDATE_SRC="${REPO_DIR}/deploy/ethos-update"
if [ -f "${UPDATE_SRC}" ]; then
    install -m 0755 -o root -g root "${UPDATE_SRC}" "${UPDATE_HELPER_BIN}"

    install -d -m 0755 /etc/ethos-nas
    PUBKEY_TMP="$(mktemp)"
    printf '%s\n' "${UPDATE_PUBKEY_DER_B64}" | base64 -d > "${PUBKEY_TMP}"
    openssl pkey -pubin -inform DER -in "${PUBKEY_TMP}" -out "${UPDATE_PUBKEY_PEM}" >/dev/null 2>&1 \
        || { echo "ERROR: could not write ${UPDATE_PUBKEY_PEM}" >&2; rm -f "${PUBKEY_TMP}"; exit 1; }
    rm -f "${PUBKEY_TMP}"
    chmod 0644 "${UPDATE_PUBKEY_PEM}"

    UPDATE_REPO_OWNER="$(stat -c %U "${REPO_DIR}")"
    UPDATE_DATA_DIR="${ETHOS_DATA_DIR:-${REPO_DIR}/backend/data}"
    CONF_TMP="$(mktemp)"
    {
        echo "# Generated by setup_scripts/setup_service.sh — do not edit by hand."
        echo "INSTALL_DIR='${REPO_DIR}'"
        echo "REPO_OWNER='${UPDATE_REPO_OWNER}'"
        echo "SERVICE_USER='${TARGET_USER}'"
        echo "SERVICE_UNIT='ethos-nas.service'"
        echo "DATA_DIR='${UPDATE_DATA_DIR}'"
        echo "INDEX_URL='${UPDATE_INDEX_URL}'"
        echo "PUBKEY_PEM='${UPDATE_PUBKEY_PEM}'"
    } > "${CONF_TMP}"
    install -m 0644 -o root -g root "${CONF_TMP}" "${UPDATE_CONF}"
    rm -f "${CONF_TMP}"
    echo "✓ ${UPDATE_HELPER_BIN} installed (core update helper; repo owner: ${UPDATE_REPO_OWNER})"
fi

# ── 4e. Optionally install Docker Engine now (fresh-server installs) ────────
if [ "${ETHOS_INSTALL_DOCKER:-0}" = "1" ]; then
    if [ -S /var/run/docker.sock ]; then
        echo "✓ Docker Engine already present — skipping installation"
    else
        echo "→ installing Docker Engine + Compose v2 (official Docker repository)"
        if "${DOCKER_HELPER_BIN}"; then
            echo "✓ Docker Engine installed"
        else
            echo "! Docker installation failed — install it later from the Docker Manager app" >&2
        fi
    fi
fi

# ── 5. Docker group membership (standard model — no sudo for docker) ─────────
if [ -S /var/run/docker.sock ] && getent group docker >/dev/null; then
    if id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -qx "docker"; then
        echo "✓ ${TARGET_USER} already in docker group"
    else
        usermod -aG docker "${TARGET_USER}"
        echo "✓ added ${TARGET_USER} to docker group (effective after service restart)"
    fi
else
    echo "· /var/run/docker.sock not present — skipping docker group membership"
fi

# ── 6. Remove old per-feature sudoers files (superseded by the consolidated one)
for old in ethos-nas-shares ethos-nas-firewall ethos-nas-usb; do
    if [ -f "${SUDOERS_DIR}/${old}" ]; then
        rm -f "${SUDOERS_DIR}/${old}"
        echo "✓ removed legacy ${SUDOERS_DIR}/${old}"
    fi
done

# ── 7. systemd unit + cutover ───────────────────────────────────────────────
if [ ! -f "${UNIT_SRC}" ]; then
    echo "ERROR: ${UNIT_SRC} not found" >&2
    exit 1
fi
install -m 0644 -o root -g root "${UNIT_SRC}" "${UNIT_DST}"
# Adapt the account and checkout paths (the repo file is canonical for ethos-nas).
UNIT_REPO_DIR="${REPO_DIR//\\/\\\\}"
UNIT_REPO_DIR="${UNIT_REPO_DIR//&/\\&}"
UNIT_REPO_DIR="${UNIT_REPO_DIR//|/\\|}"
sed -i \
    -e "s|^User=.*|User=${TARGET_USER}|" \
    -e "s|^Group=.*|Group=${TARGET_USER}|" \
    -e "s|__ETHOS_INSTALL_DIR__|${UNIT_REPO_DIR}|g" \
    "${UNIT_DST}"
systemctl daemon-reload

systemctl enable ethos-nas
# Restart (not just start) so group changes (docker) are picked up on re-runs.
systemctl restart ethos-nas
echo "✓ systemd unit installed and service started as ${TARGET_USER}"

sleep 3
code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9000/api/system/status || true)"
if [ "${code}" = "200" ] || [ "${code}" = "401" ]; then
    echo "✓ service is up (HTTP ${code} on /api/system/status — 401 unauthenticated = healthy)"
else
    echo "! service may not be up yet (HTTP ${code:-none}) — check: journalctl -u ethos-nas" >&2
fi

echo ""
echo "Done. EthOS NAS now runs as '${TARGET_USER}' with narrow NOPASSWD privileges."
echo "  sudoers : ${SUDOERS_FILE}"
echo "  unit    : systemctl status ethos-nas"
echo "  logs    : journalctl -u ethos-nas -f"
