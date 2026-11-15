#!/usr/bin/env bash
# DEPRECATED and intentionally inert.
#
# Superseded by setup_scripts/setup_service.sh (dedicated `ethos-nas` service
# user + one consolidated, visudo-validated sudoers file). The firewall rules
# it used to install were plain `ufw` / `iptables` / `iptables-save` with no
# argument restrictions and were granted to "$CURRENT_USER" — i.e. root for any
# local login. This script now refuses to run.
#
# Previous implementation: `git log --diff-filter=D -- setup_scripts/setup_sudoers_firewall.sh`.

echo "setup_sudoers_firewall.sh is deprecated — run setup_scripts/setup_service.sh instead." >&2
exit 1
