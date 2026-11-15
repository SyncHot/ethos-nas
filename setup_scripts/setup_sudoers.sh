#!/usr/bin/env bash
# DEPRECATED and intentionally inert.
#
# Superseded by setup_scripts/setup_service.sh, which creates the dedicated
# `ethos-nas` service user and installs ONE consolidated, visudo-validated
# /etc/sudoers.d/ethos-nas file with narrow, argument-exact rules.
#
# The old version of this script granted privileges to "$CURRENT_USER" (often a
# developer account) and included bare `ufw`, `iptables` and `tee /etc/fstab`
# rules — effectively root for anyone who could log in. Running it by mistake
# must never be able to widen access, so this script refuses to do anything.
#
# The previous implementation is preserved in git history (removed for
# TODO.md P0-08): `git log --diff-filter=D -- setup_scripts/setup_sudoers.sh`.

echo "setup_sudoers.sh is deprecated — run setup_scripts/setup_service.sh instead." >&2
exit 1
