#!/usr/bin/env bash
# Daily restic backup of /opt/appdata to /backup (CIFS to flint2).
# Stops containers around the snapshot so SQLite DBs stay consistent.
set -euo pipefail

REPO="${RESTIC_REPOSITORY:-/backup/restic-media-srv}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/media-srv}"
APPDATA="${APPDATA:-/opt/appdata}"
PASSFILE="${RESTIC_PASSWORD_FILE:-/root/.restic-media-srv.pass}"

if [[ ! -d /backup ]] || ! mountpoint -q /backup; then
  echo "ERROR: /backup is not mounted." >&2
  exit 1
fi
if [[ ! -f "$PASSFILE" ]]; then
  echo "ERROR: $PASSFILE not found. Run restic-init.sh first." >&2
  exit 1
fi

export RESTIC_REPOSITORY="$REPO"
export RESTIC_PASSWORD_FILE="$PASSFILE"

cd "$COMPOSE_DIR"

echo "==> stopping stack for consistent snapshot"
docker compose stop

trap 'echo "==> bringing stack back up"; docker compose start' EXIT

echo "==> restic backup $APPDATA"
restic backup "$APPDATA" \
  --tag media-srv \
  --tag appdata \
  --exclude "*/log/*" \
  --exclude "*/logs/*" \
  --exclude "*/Logs/*" \
  --exclude "*/Cache/*" \
  --exclude "*/cache/*"

echo "==> retention: keep 7 daily, 4 weekly, 6 monthly"
restic forget --prune \
  --keep-daily 7 \
  --keep-weekly 4 \
  --keep-monthly 6

echo "==> snapshots:"
restic snapshots --compact
