#!/bin/bash
# EthOS NAS — development server (auto-reload).
#
# Production runs as the `ethos-nas` systemd unit on :9000 (see docs/ops.md).
# This script is ONLY for editing code: it binds :9001 and points ETHOS_DATA_DIR
# at a temporary directory, so a reload can never race the service or corrupt
# the production JSON stores (TODO.md P5-02).
#
# Override with ETHOS_PORT / ETHOS_DATA_DIR / ETHOS_HOST if needed.
set -euo pipefail
cd "$(dirname "$0")"

source .venv/bin/activate

export ETHOS_PORT="${ETHOS_PORT:-9001}"
export ETHOS_DATA_DIR="${ETHOS_DATA_DIR:-/tmp/ethos-dev-data}"
export ETHOS_DEBUG=true
mkdir -p "$ETHOS_DATA_DIR"

echo "Dev server → http://${ETHOS_HOST:-127.0.0.1}:${ETHOS_PORT}  (data: ${ETHOS_DATA_DIR})"
echo "Production is untouched on :9000 (systemd unit ethos-nas)."

exec python3 -m uvicorn backend.main:app \
    --host "${ETHOS_HOST:-127.0.0.1}" \
    --port "$ETHOS_PORT" \
    --reload