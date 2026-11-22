#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/SyncHot/ethos-nas.git"
INSTALL_DIR="/opt/ethos-nas"
FROM_CHECKOUT=0

usage() {
    cat <<'USAGE'
Usage: install.sh [--install-dir ABSOLUTE_PATH] [--from-checkout]

Installs EthOS NAS on Ubuntu Server 64-bit. Must run as root through sudo,
so the invoking administrator is available as SUDO_USER. Use --from-checkout
when the repository is already present (offline, air-gapped or a pinned tag).

Docker Engine + Compose v2 are installed from Docker's official apt repository
(Packages apps and the Docker Manager need them). Set ETHOS_SKIP_DOCKER=1
to skip that step and install Docker later from the Docker Manager app.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --install-dir needs a path" >&2; exit 2; }
            INSTALL_DIR="$2"
            shift 2
            ;;
        --from-checkout)
            FROM_CHECKOUT=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ] || [ -z "${SUDO_USER:-}" ] || [ "${SUDO_USER}" = root ]; then
    echo "ERROR: run this installer with sudo from an administrator account." >&2
    echo "       Example: curl -fsSL <installer-url> | sudo bash" >&2
    exit 1
fi

if [[ ! "${INSTALL_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "ERROR: install path must be absolute and use only letters, numbers, '.', '_', '-', and '/': ${INSTALL_DIR}" >&2
    exit 1
fi
INSTALL_DIR="$(realpath -m "${INSTALL_DIR}")"

if [ ! -r /etc/os-release ]; then
    echo "ERROR: cannot identify the operating system (/etc/os-release missing)." >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != ubuntu ]; then
    echo "ERROR: this installer currently supports Ubuntu Server only (found ${PRETTY_NAME:-unknown})." >&2
    exit 1
fi
case "$(uname -m)" in
    aarch64|x86_64) ;;
    *)
        echo "ERROR: unsupported architecture: $(uname -m); use a 64-bit ARM or x86_64 system." >&2
        exit 1
        ;;
esac

if [ "${FROM_CHECKOUT}" = "1" ]; then
    if [ ! -d "${INSTALL_DIR}/.git" ] || [ ! -f "${INSTALL_DIR}/requirements.txt" ] \
            || [ ! -f "${INSTALL_DIR}/setup_scripts/setup_service.sh" ]; then
        echo "ERROR: --from-checkout requires a cloned EthOS NAS repository at ${INSTALL_DIR}." >&2
        exit 1
    fi
    REPO_ROOT="$(git -C "${INSTALL_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ "${REPO_ROOT}" != "${INSTALL_DIR}" ]; then
        echo "ERROR: install path must be the repository root: ${INSTALL_DIR}" >&2
        exit 1
    fi
    OWNER_UID="$(stat -c '%u' "${INSTALL_DIR}")"
    if [ "${OWNER_UID}" != "$(id -u "${SUDO_USER}")" ]; then
        echo "ERROR: the checkout must be owned by ${SUDO_USER}; refusing to change its ownership." >&2
        exit 1
    fi
    if [ -n "$(find "${INSTALL_DIR}/backend/data" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        echo "ERROR: backend/data is not empty; back it up and use the existing-install upgrade process." >&2
        exit 1
    fi
elif [ -e "${INSTALL_DIR}" ]; then
    echo "ERROR: install path already exists; refusing to overwrite: ${INSTALL_DIR}" >&2
    exit 1
fi
if systemctl list-unit-files ethos-nas.service --no-legend 2>/dev/null | grep -q '^ethos-nas\.service'; then
    echo "ERROR: an ethos-nas systemd unit already exists; refusing to overwrite it." >&2
    exit 1
fi
# Image builds run in a chroot that shares the *host's* network namespace, so a
# NAS already listening on :9000 (or the build host itself) makes this a false
# positive. ethos-os-release sets ETHOS_SKIP_PORT_CHECK=1 for its build chroot.
if [ "${ETHOS_SKIP_PORT_CHECK:-}" != "1" ] \
        && command -v fuser >/dev/null 2>&1 && fuser 9000/tcp >/dev/null 2>&1; then
    echo "ERROR: port 9000 is already in use; stop or reconfigure that service, then retry." >&2
    exit 1
fi
if [ -e /etc/ethos-nas/ethos.env ]; then
    echo "ERROR: /etc/ethos-nas/ethos.env already exists; refusing to overwrite secrets." >&2
    exit 1
fi
if ! getent passwd "${SUDO_USER}" >/dev/null; then
    echo "ERROR: invoking user '${SUDO_USER}' does not exist." >&2
    exit 1
fi

if [ ! -r /dev/tty ]; then
    echo "ERROR: a terminal is required to confirm installation." >&2
    exit 1
fi
printf 'Install EthOS NAS under %s for user %s? This installs OS packages and configures a system service. [y/N] ' \
    "${INSTALL_DIR}" "${SUDO_USER}" >/dev/tty
IFS= read -r answer </dev/tty
case "${answer}" in
    y|Y|yes|YES) ;;
    *) echo "Installation cancelled."; exit 1 ;;
esac

echo "→ installing Ubuntu dependencies"
apt-get update
if ! DEBIAN_FRONTEND=noninteractive apt-get install -y \
    7zip acl btrfs-progs build-essential ca-certificates cron curl dosfstools \
    exfatprogs ffmpeg git iptables libarchive-tools libffi-dev libpam0g-dev \
    libssl-dev nftables ntfs-3g openssl parted psmisc python3-dev python3-venv \
    rclone rsync samba smartmontools software-properties-common sudo ufw xfsprogs zip; then
    echo "ERROR: could not install the required Ubuntu packages." >&2
    echo "       If apt reports unmet dependencies (for example acl/libacl1), the" >&2
    echo "       '<codename>-updates' suite is missing from" >&2
    echo "       /etc/apt/sources.list.d/ubuntu.sources. Add it to the 'Suites:' line" >&2
    echo "       (e.g. 'Suites: noble noble-updates'), run apt-get update, and retry." >&2
    exit 1
fi

python_meets_minimum() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' >/dev/null 2>&1
}

find_python() {
    local candidate path
    for candidate in python3.14 python3; do
        path="$(command -v "${candidate}" 2>/dev/null || true)"
        if [ -n "${path}" ] && python_meets_minimum "${path}"; then
            printf '%s\n' "${path}"
            return 0
        fi
    done
    return 1
}

echo "→ resolving Python 3.14 or newer"
if ! PYTHON_BIN="$(find_python)"; then
    # Ubuntu 24.04 LTS ships Python 3.12, so pull 3.14 from the deadsnakes PPA
    # instead of failing the install.
    echo "  system Python is older than 3.14; enabling the deadsnakes PPA"
    if ! command -v add-apt-repository >/dev/null 2>&1; then
        echo "ERROR: add-apt-repository is missing (install software-properties-common)." >&2
        exit 1
    fi
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3.14 python3.14-dev python3.14-venv
    if ! PYTHON_BIN="$(find_python)"; then
        echo "ERROR: Python 3.14 or newer is required, but deadsnakes did not provide it." >&2
        exit 1
    fi
fi
echo "  using ${PYTHON_BIN} ($("${PYTHON_BIN}" --version 2>&1))"

echo "→ cloning EthOS NAS to ${INSTALL_DIR}"
if [ "${FROM_CHECKOUT}" = "0" ]; then
    install -d -o "${SUDO_USER}" -g "$(id -gn "${SUDO_USER}")" "${INSTALL_DIR}"
    runuser -u "${SUDO_USER}" -- git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
else
    echo "  using existing checkout at ${INSTALL_DIR}"
fi

echo "→ installing Python packages"
runuser -u "${SUDO_USER}" -- "${PYTHON_BIN}" -m venv "${INSTALL_DIR}/.venv"
runuser -u "${SUDO_USER}" -- "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
runuser -u "${SUDO_USER}" -- "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "→ creating a private JWT secret for the system service"
JWT_SECRET="$(openssl rand -hex 32)"
install -d -o root -g root -m 0755 /etc/ethos-nas
umask 077
printf 'ETHOS_JWT_SECRET=%s\n' "${JWT_SECRET}" >/etc/ethos-nas/ethos.env
chown root:root /etc/ethos-nas/ethos.env
chmod 0600 /etc/ethos-nas/ethos.env
unset JWT_SECRET

echo "→ installing and starting the ethos-nas service"
if [ "${ETHOS_SKIP_DOCKER:-0}" = "1" ]; then
    echo "  Docker Engine installation skipped (ETHOS_SKIP_DOCKER=1)"
    INSTALL_DOCKER=0
else
    echo "  Docker Engine + Compose v2 will be installed from Docker's official repository"
    INSTALL_DOCKER=1
fi
# File Station, the code editor and the terminal all run as the service user,
# so a fresh install must grant it ACL access to the admin home (non-dot
# entries only). Without this the UI cannot open /home/<admin> at all. Shares
# are still untouched: nothing is mounted and no share ACLs are applied.
ETHOS_SHARES= ETHOS_GRANT_HOME_ACL=1 ETHOS_INSTALL_DOCKER="${INSTALL_DOCKER}" \
    bash "${INSTALL_DIR}/setup_scripts/setup_service.sh"

echo ""
echo "EthOS NAS installation finished."
echo "  URL:    http://$(hostname -I | awk '{print $1}'):9000"
echo "  Status: sudo systemctl status ethos-nas"
echo "  Logs:   sudo journalctl -u ethos-nas -f"
echo "  Data:   ${INSTALL_DIR}/backend/data"
echo "No disks were formatted, partitioned, or mounted. Configure storage only after confirming the intended mount."
