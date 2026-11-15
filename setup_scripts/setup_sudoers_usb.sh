#!/usr/bin/env bash
# DEPRECATED and intentionally inert.
#
# Superseded by setup_scripts/setup_service.sh, which installs the argument-free
# `/usr/local/bin/ethos-usb-partition` helper with its own internal validation
# and grants it to the dedicated `ethos-nas` service user only. The old version
# of this script granted broad device manipulation to "$CURRENT_USER".
#
# Running this by mistake must never widen access, so it refuses to do anything.
# Previous implementation: `git log --diff-filter=D -- setup_scripts/setup_sudoers_usb.sh`.

echo "setup_sudoers_usb.sh is deprecated — run setup_scripts/setup_service.sh instead." >&2
exit 1
