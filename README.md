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

## Ports / URLs

| Service     | Direct (LAN)            | Pretty (via Caddy + LAN DNS)             |
|-------------|-------------------------|------------------------------------------|
| Jellyfin    | http://192.168.100.5:8096 | https://jellyfin.media.sys-lab.xyz     |
| qBittorrent | http://192.168.100.5:8080 | https://qbit.media.sys-lab.xyz         |
| Prowlarr    | http://192.168.100.5:9696 | https://prowlarr.media.sys-lab.xyz     |
| Sonarr      | http://192.168.100.5:8989 | https://sonarr.media.sys-lab.xyz       |
| Radarr      | http://192.168.100.5:7878 | https://radarr.media.sys-lab.xyz       |
| Bazarr      | http://192.168.100.5:6767 | https://bazarr.media.sys-lab.xyz       |
| Jellyseerr  | http://192.168.100.5:5055 | https://jellyseerr.media.sys-lab.xyz   |
| Searcharr   | — (Telegram-only, no HTTP) | —                                      |

The "pretty" URLs go through:

```
AdGuard Home on flint2 (DNS rewrite *.media.sys-lab.xyz → 192.168.100.5)
   ↓
Caddy on ryzen :443 (wildcard cert *.media.sys-lab.xyz from Let's Encrypt DNS-01 via Cloudflare)
   ↓
service on its localhost port
```

Access only via LAN / WireGuard (vpn.sys-lab.xyz). No public exposure — the pretty URLs resolve to 192.168.100.5 only inside the home network and over the WireGuard tunnel.

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
