# media-srv setup

Step-by-step bring-up on `home-server` (Ryzen 4700U, Ubuntu 24.04).

## 1. Host prep (one-off)

External WD My Passport 2TB as `/dev/sda`:

```bash
# GPT + single ext4 partition, label "media", root reserve 1%
sudo parted /dev/sda --script mklabel gpt
sudo parted /dev/sda --script mkpart primary ext4 0% 100%
sudo mkfs.ext4 -L media -m 1 /dev/sda1

# Mount via fstab
sudo mkdir -p /mnt/media
UUID=$(sudo blkid -s UUID -o value /dev/sda1)
echo "UUID=$UUID  /mnt/media  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a

# Folder layout — single filesystem so Sonarr/Radarr hardlink instead of copy
sudo mkdir -p /mnt/media/{downloads/{complete,incomplete},movies,tv}
sudo chown -R sykkyb:sykkyb /mnt/media
sudo chmod -R 775 /mnt/media

# Stop USB-HDD spindown (WD bridge ignores APM, hd-idle works)
sudo apt install -y hd-idle
echo 'HD_IDLE_OPTS="-i 0 -a sda -i 0"' | sudo tee /etc/default/hd-idle
sudo systemctl enable --now hd-idle

# Configs dir on NVMe
sudo mkdir -p /opt/appdata/{jellyfin,sonarr,radarr,prowlarr,qbittorrent,bazarr,jellyseerr}
sudo chown -R sykkyb:sykkyb /opt/appdata

# VA-API for Jellyfin HW transcoding (Vega iGPU)
sudo apt install -y vainfo mesa-va-drivers
sudo usermod -aG render,video sykkyb   # log out + back in for groups to apply
getent group render                    # note the GID → RENDER_GID in .env
```

## 2. Clone and configure

```bash
cd /opt
sudo git clone <repo-url> media-srv
sudo chown -R sykkyb:sykkyb media-srv
cd media-srv
cp .env.example .env
$EDITOR .env
```

## 3. First deploy

```bash
./scripts/deploy.sh
docker compose ps   # all 7 containers should be Up
```

## 4. Wire the services (in this order)

### 4.1 Prowlarr — http://192.168.100.5:9696

1. Set an admin password.
2. **Indexers** → Add your trackers.
3. **Settings → Apps** → Add Sonarr and Radarr (use container names as host: `http://sonarr:8989`, `http://radarr:7878`, API keys from those services' UIs once you set them up below — easiest to come back to Prowlarr after step 4.3).

### 4.2 qBittorrent — http://192.168.100.5:8080

Default login: `admin` / temp password printed in `docker compose logs qbittorrent` (search for "temporary password").

1. **Tools → Options → Web UI** → set permanent password.
2. **Downloads:**
   - Default save path: `/data/downloads/complete`
   - Incomplete downloads: `/data/downloads/incomplete`
   - Keep incomplete `.!qB` extension: on
3. **Categories** (right-click "Categories" in sidebar):
   - `tv-sonarr` → save path: `/data/downloads/complete/tv`
   - `radarr` → save path: `/data/downloads/complete/movies`

### 4.3 Sonarr — http://192.168.100.5:8989

1. Set authentication.
2. **Settings → Media Management:**
   - Rename Episodes: on
   - Standard format: `{Series Title} - S{season:00}E{episode:00} - {Episode Title}`
   - Use Hardlinks instead of Copy: **ON** (this is the whole point)
   - Import Extra Files: `srt,sub`
3. **Settings → Download Clients → +qBittorrent:**
   - Host: `qbittorrent`, Port: `8080`
   - Username/password: from 4.2
   - Category: `tv-sonarr`
4. **Root Folders → Add:** `/data/tv`
5. **Settings → General → API Key** — copy for Prowlarr.

### 4.4 Radarr — http://192.168.100.5:7878

Same shape as Sonarr:
- Hardlinks: ON
- Download client: qBittorrent at `qbittorrent:8080`, category `radarr`
- Root folder: `/data/movies`
- Copy API key.

Now go back to **Prowlarr** and finish wiring Sonarr + Radarr with their API keys.

### 4.5 Jellyfin — http://192.168.100.5:8096

1. First-run wizard: create admin, set language.
2. **Add libraries:**
   - Movies → `/data/movies`
   - Shows → `/data/tv`
3. **Dashboard → Playback → Transcoding:**
   - Hardware acceleration: **Video Acceleration API (VAAPI)**
   - VA-API device: `/dev/dri/renderD128`
   - Enable: HEVC, H264, VP9, AV1 (whichever vainfo reports as supported)
   - Enable Tone Mapping: on
4. Play any file and watch `Dashboard → Activity` — if it says "Direct Play" you're golden; "Transcoding" with low CPU usage means VAAPI is working.

If VAAPI fails: drop back to software transcoding (uncheck hardware acceleration). Ryzen 4700U handles 1-2 streams of 1080p on CPU comfortably.

### 4.6 Bazarr — http://192.168.100.5:6767

1. **Settings → Languages → Profiles:** create one (e.g., Russian + English).
2. **Settings → Sonarr** → connect to `sonarr:8989` with API key.
3. **Settings → Radarr** → connect to `radarr:7878` with API key.
4. **Settings → Providers** → enable OpenSubtitles, Subscene, etc.

### 4.7 Jellyseerr — http://192.168.100.5:5055

1. Sign in with a Jellyfin user.
2. Connect Sonarr (`sonarr:8989`) and Radarr (`radarr:7878`).
3. Set default quality profiles and root folders.

Done — request a movie/show in Jellyseerr → Radarr/Sonarr search via Prowlarr → qBittorrent downloads to `/mnt/media/downloads/complete/...` → *arr hardlinks to `/mnt/media/{movies,tv}/...` → Jellyfin picks it up.

## 5. Backup setup (one-off)

```bash
# Install restic
sudo apt install -y restic

# Initialize the repo on /backup (CIFS mount to flint2)
./scripts/restic-init.sh

# Save the repo password somewhere safe (1Password etc.)
# Run first backup manually to verify
./scripts/backup.sh

# Add to root's crontab — daily at 03:30
sudo crontab -e
# 30 3 * * * /opt/media-srv/scripts/backup.sh >> /var/log/media-srv-backup.log 2>&1
```

## 6. Watchdog setup (one-off)

The existing Telegram watchdog (cron 1min, `flint2_watchdog_bot`) just checks ping. We extend it with a container-level check:

```bash
# Copy or symlink the script to a stable path
sudo ln -s /opt/media-srv/scripts/watchdog-check.sh /usr/local/bin/media-srv-watchdog

# Add to sykkyb's crontab — every minute
crontab -e
# * * * * * /usr/local/bin/media-srv-watchdog
```

It uses `~/watchdog/config.env` (the same file as the existing ryzen4700-watchdog) for `BOT_TOKEN` + `CHAT_ID`. No new credential file needed — just reuse what's already there.
