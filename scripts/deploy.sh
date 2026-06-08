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

# Searcharr connects to Sonarr/Radarr only ONCE at startup and never reconnects.
# depends_on:service_healthy is unreliable when several images are recreated at
# once (Searcharr can start before *arr are ready -> broken integrations), so
# explicitly wait for *arr health and restart Searcharr last.
echo "==> waiting for sonarr/radarr healthy, then restarting searcharr"
for _ in $(seq 1 40); do
  s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' sonarr 2>/dev/null || true)
  r=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' radarr 2>/dev/null || true)
  [[ "$s" == healthy && "$r" == healthy ]] && break
  sleep 3
done
docker compose restart searcharr

echo "==> docker compose ps"
docker compose ps
