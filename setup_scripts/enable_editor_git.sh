#!/bin/bash
# enable_editor_git.sh — let the ethos-nas service user run git in user-owned repos
#
# Background: git >= 2.35.3 refuses to operate on a repository owned by a
# different user ("detected dubious ownership"). The EthOS NAS editor's git
# integration runs as the dedicated `ethos-nas` service user, so it cannot
# operate on repos owned by the login user (e.g. ~/apps/compose/...) — even
# though the service user already has full read/write access to them via
# group membership (the repo dirs/files are group-writable). Full analysis:
# docs/editor-git-cross-user.md.
#
# This script adds `safe.directory` entries to the service user's GLOBAL git
# config (/home/ethos-nas/.gitconfig). Git reads that file on every
# invocation, so the fix takes effect IMMEDIATELY — no service restart.
#
# Usage (requires root):
#   sudo bash setup_scripts/enable_editor_git.sh --all
#       Trust every repo the service user can access. Fine on a single-user
#       NAS; the service account already runs user commands (Terminal app),
#       so this adds no meaningful attack surface (see docs for rationale).
#   sudo bash setup_scripts/enable_editor_git.sh /path/to/repo [/more/repo ...]
#       Trust only the listed repos. Safer default; re-run to add more.
#
# Idempotent — safe to re-run (existing entries are not duplicated).
#
# Testable without root (tests do this):
#   ETHOS_GITCONFIG=/tmp/somewhere/.gitconfig \
#       bash setup_scripts/enable_editor_git.sh --all
#   (non-root mode requires the target directory to exist and be writable)

set -euo pipefail

SERVICE_USER="${ETHOS_GIT_USER:-ethos-nas}"
SERVICE_HOME="${ETHOS_GIT_HOME:-/home/$SERVICE_USER}"
GITCONFIG="${ETHOS_GITCONFIG:-$SERVICE_HOME/.gitconfig}"

die() { echo "error: $*" >&2; exit 1; }

[ $# -ge 1 ] || { echo "usage: $0 --all | <repo path>..." >&2; exit 2; }

# ── Collect safe.directory entries ──────────────────────────────────────────
entries=()
if [ "$1" = "--all" ]; then
    entries=("*")
else
    for p in "$@"; do
        case "$p" in
            "~"|"~"/*) p="$HOME/${p#\~/}" ;;
        esac
        [ "${p#/}" != "$p" ] || die "repo path must be absolute: $p"
        [ -d "$p" ] || die "not a directory: $p"
        p="$(cd "$p" && pwd -P)"   # canonical (resolves symlinks, strips /.)
        entries+=("$p")
    done
fi

# ── Permissions for non-root (test) mode ────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    dir="$(dirname -- "$GITCONFIG")"
    [ -d "$dir" ] || die "non-root: $dir does not exist (run with sudo, or create it for tests)"
    [ -w "$dir" ] || die "non-root: $dir is not writable by $(id -un) — run with sudo"
fi

# ── Ensure the service user's home exists (root mode) ───────────────────────
if [ "$(id -u)" -eq 0 ]; then
    if [ ! -d "$SERVICE_HOME" ]; then
        echo "creating missing service home: $SERVICE_HOME"
        mkdir -p "$SERVICE_HOME"
        chown "$SERVICE_USER":"$SERVICE_USER" "$SERVICE_HOME"
        chmod 700 "$SERVICE_HOME"
    fi
fi

# ── Merge entries into the [safe] section ───────────────────────────────────
if ! grep -qE '^[[:space:]]*\[safe\]' "$GITCONFIG" 2>/dev/null; then
    printf '\n[safe]\n' >> "$GITCONFIG"
fi
for e in "${entries[@]}"; do
    if grep -qF "directory = $e" "$GITCONFIG"; then
        echo "  (already trusted) $e"
    else
        printf '\tdirectory = %s\n' "$e" >> "$GITCONFIG"
        echo "  added: $e"
    fi
done

if [ "$(id -u)" -eq 0 ]; then
    chown "$SERVICE_USER":"$SERVICE_USER" "$GITCONFIG"
fi
chmod 600 "$GITCONFIG"

echo "done. $GITCONFIG now trusts: ${entries[*]}"
echo "No ethos-nas restart needed — git reads this file on every invocation."
echo "Verify: open the repo in the Code Editor (git panel shows branch/status),"
echo "or GET /api/editor/git/status?repo=<path> with a JWT."
echo "Note: the permanent app-level fix (editor git helpers pass safe.directory"
echo "themselves) is tracked in docs/editor-git-cross-user.md; once it ships,"
echo "this script is no longer needed."
