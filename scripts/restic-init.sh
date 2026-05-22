#!/usr/bin/env bash
# One-off: initialize the restic repository on /backup.
# Run this once, then save the password to a password manager.
set -euo pipefail

REPO="${RESTIC_REPOSITORY:-/backup/restic-media-srv}"

if [[ ! -d /backup ]]; then
  echo "ERROR: /backup is not mounted. Check the CIFS mount to flint2." >&2
  exit 1
fi

if [[ -f /backup/restic-media-srv/config ]]; then
  echo "Repo already exists at $REPO — nothing to do."
  exit 0
fi

if [[ -z "${RESTIC_PASSWORD:-}" ]] && [[ ! -f /root/.restic-media-srv.pass ]]; then
  echo "Set RESTIC_PASSWORD env var, or put it in /root/.restic-media-srv.pass (chmod 600)." >&2
  echo "Then re-run this script." >&2
  exit 1
fi

export RESTIC_REPOSITORY="$REPO"
[[ -f /root/.restic-media-srv.pass ]] && export RESTIC_PASSWORD_FILE=/root/.restic-media-srv.pass

sudo -E restic init
echo "Done. Repo: $REPO"
