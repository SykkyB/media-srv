#!/usr/bin/env bash
# Interactive restore helper. Lists snapshots, restores a chosen one to /tmp.
set -euo pipefail

REPO="${RESTIC_REPOSITORY:-/backup/restic-media-srv}"
PASSFILE="${RESTIC_PASSWORD_FILE:-/root/.restic-media-srv.pass}"

export RESTIC_REPOSITORY="$REPO"
export RESTIC_PASSWORD_FILE="$PASSFILE"

echo "Snapshots in $REPO:"
restic snapshots --compact

read -rp "Snapshot ID to restore (or 'latest'): " SNAP
read -rp "Target directory [/tmp/restore]: " TARGET
TARGET="${TARGET:-/tmp/restore}"

mkdir -p "$TARGET"
restic restore "$SNAP" --target "$TARGET"

echo "Done. Restored to $TARGET"
echo "If you want to put it back: stop the stack, rsync from $TARGET/opt/appdata/ to /opt/appdata/, start the stack."
