#!/usr/bin/env bash
# EthOS NAS — generate and install a new JWT secret.
#
# Rotating the secret invalidates every issued token (web sessions end; the
# Android apps must log in again). Per-user API keys in users.json survive, but
# tokens minted from them do not. See docs/ops.md §9.
#
# Usage:
#   bash setup_scripts/rotate_jwt_secret.sh            # print the command to run
#   sudo bash setup_scripts/rotate_jwt_secret.sh --apply   # install + restart
set -euo pipefail

SECRET="$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 64)"

cat <<EOF
New secret:
  ${SECRET}

Install it with either:

  1) systemd override (recommended):
     sudo systemctl edit ethos-nas
       [Service]
       Environment=ETHOS_JWT_SECRET=${SECRET}
     sudo systemctl restart ethos-nas

  2) .env next to the repo (0600, gitignored):
     printf 'ETHOS_JWT_SECRET=%s\n' '${SECRET}' > $(cd "$(dirname "$0")/.." && pwd)/.env
     chmod 600 $(cd "$(dirname "$0")/.." && pwd)/.env
     sudo systemctl restart ethos-nas
EOF

if [[ "${1:-}" == "--apply" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: --apply needs root (use sudo)" >&2
        exit 1
    fi
    mkdir -p /etc/systemd/system/ethos-nas.service.d
    printf '[Service]\nEnvironment=ETHOS_JWT_SECRET=%s\n' "${SECRET}" \
        > /etc/systemd/system/ethos-nas.service.d/10-jwt-secret.conf
    chmod 600 /etc/systemd/system/ethos-nas.service.d/10-jwt-secret.conf
    systemctl daemon-reload
    systemctl restart ethos-nas
    echo "✓ secret installed and service restarted"
fi
