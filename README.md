# media-srv

Self-hosted media stack on `home-server` (Ryzen 4700U): Jellyfin + Sonarr/Radarr/Prowlarr/Bazarr + qBittorrent + Jellyseerr + Searcharr.

Configs on NVMe (`/opt/appdata`), media on WD My Passport 2TB mounted at `/mnt/media` (ext4). Single filesystem under `/mnt/media` so Sonarr/Radarr can hardlink instead of copy.

Resource limits, healthchecks, and host-wide log rotation are wired up per [Otus best-practices](https://habr.com/ru/companies/otus/articles/1034390/) — see "Resource limits" section below.

## Layout on host

```
/opt/appdata/<service>/   configs, DBs (NVMe)
/mnt/media/downloads/     qBittorrent downloads
/mnt/media/movies/        Radarr destination
/mnt/media/tv/            Sonarr destination
```

Inside containers: `/mnt/media` is mounted as `/data` for *arr/qBit. Jellyfin sees `movies/` and `tv/` read-only.

## Ports

| Service     | Port |
|-------------|------|
| Jellyfin    | 8096 |
| qBittorrent | 8080 |
| Prowlarr    | 9696 |
| Sonarr      | 8989 |
| Radarr      | 7878 |
| Bazarr      | 6767 |
| Jellyseerr   | 5055 |
| Searcharr   | — (Telegram-only, no HTTP) |

Access only via LAN / WireGuard (vpn.sys-lab.xyz). No public exposure.

## Setup

1. Host prep (one-off, see [docs/SETUP.md](docs/SETUP.md)) — ext4 on the HDD, hd-idle, render group, VA-API libs.
2. `git clone` this repo to `/opt/media-srv` on the server.
3. `cp .env.example .env` and edit (PUID/PGID/RENDER_GID/TZ).
4. `./scripts/deploy.sh` — pulls images and starts the stack.
5. First-run wiring inside the UIs (Prowlarr → qBittorrent → Sonarr/Radarr → Jellyfin → Jellyseerr): see [docs/SETUP.md](docs/SETUP.md).

## Operations

- **Deploy / update:** `./scripts/deploy.sh`
- **Backup:** `./scripts/backup.sh` (restic → `/backup` CIFS on flint2). Initialize once with `./scripts/restic-init.sh`.
- **Watchdog:** `scripts/watchdog-check.sh` runs from cron every minute, alerts to `flint2_watchdog_bot` on Telegram if a container is down or an HTTP probe fails.

## Resource limits

Every container in `docker-compose.yml` has `deploy.resources.limits` (memory + CPU) and most have a `healthcheck`. Approximate ceiling per container:

| Container | Memory limit | CPU limit |
|-----------|-------------:|----------:|
| jellyfin | 4G | 4.0 |
| qbittorrent | 2G | 2.0 |
| sonarr | 1G | 1.0 |
| radarr | 1G | 1.0 |
| bazarr | 768M | 0.5 |
| prowlarr | 512M | 0.5 |
| jellyseerr | 512M | 0.5 |
| searcharr | 256M | 0.25 |

Total ceiling ~10 GiB out of the 32 GiB host. Healthcheck `interval/timeout/retries/start_period` are shared via a YAML anchor (`x-healthcheck-defaults`). Jellyfin keeps its image's built-in healthcheck; Searcharr has no HTTP interface so it's container-state-only.

Docker log rotation is host-wide (`/etc/docker/daemon.json` — `max-size: 10m`, `max-file: 3`), applies to this stack and the other Docker stacks on the host.

## Files

- `docker-compose.yml` — the stack.
- `.env.example` — template for `.env` (real `.env` is gitignored).
- `scripts/deploy.sh` — `git pull && docker compose pull && docker compose up -d`.
- `scripts/backup.sh` / `restic-init.sh` / `restore.sh` — restic against `/backup`.
- `scripts/watchdog-check.sh` — health probe with Telegram alerts.
- `docs/SETUP.md` — first-time setup steps and per-service wiring.
