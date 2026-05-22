#!/usr/bin/env bash
# Pull latest commits + container images, recreate changed services.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

echo "==> git pull"
git pull --ff-only

echo "==> docker compose pull"
docker compose pull

echo "==> docker compose up -d"
docker compose up -d --remove-orphans

echo "==> docker compose ps"
docker compose ps
