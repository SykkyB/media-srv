#!/usr/bin/env bash
# Per-minute health probe for the media stack.
# Alerts to Telegram (flint2_watchdog_bot) on:
#   - any expected container not running
#   - any HTTP probe failing twice in a row
# State (last status per service) is kept in /var/tmp so we only alert on transitions.
set -euo pipefail

# --- config ---
SERVICES=(jellyfin qbittorrent prowlarr sonarr radarr bazarr jellyseerr searcharr)
declare -A HTTP_PROBES=(
  [jellyfin]="http://127.0.0.1:8096/health"
  [qbittorrent]="http://127.0.0.1:8080/"
  [prowlarr]="http://127.0.0.1:9696/ping"
  [sonarr]="http://127.0.0.1:8989/ping"
  [radarr]="http://127.0.0.1:7878/ping"
  [bazarr]="http://127.0.0.1:6767/"
  [jellyseerr]="http://127.0.0.1:5055/api/v1/status"
)
STATE_DIR="${STATE_DIR:-/var/tmp/media-srv-watchdog}"
mkdir -p "$STATE_DIR"

# --- load Telegram creds (BOT_TOKEN, CHAT_ID) ---
# Same locations as the existing ryzen4700-watchdog and flint2-watchdog.
for f in "$HOME/watchdog/config.env" /etc/watchdog/telegram.env "$HOME/.config/watchdog/telegram.env"; do
  [[ -f "$f" ]] && source "$f" && break
done
: "${BOT_TOKEN:?BOT_TOKEN not set (looked in ~/watchdog/config.env, /etc/watchdog/telegram.env, ~/.config/watchdog/telegram.env)}"
: "${CHAT_ID:?CHAT_ID not set}"

HOST="$(hostname)"

notify() {
  local msg="$1"
  curl -fsS --max-time 10 \
    -d "chat_id=${CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=<b>[${HOST}/media-srv]</b> ${msg}" \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" >/dev/null
}

set_state() { echo "$2" > "${STATE_DIR}/${1}.state"; }
get_state() { cat "${STATE_DIR}/${1}.state" 2>/dev/null || echo "ok"; }

transition() {
  local svc="$1" new="$2" reason="$3"
  local old; old="$(get_state "$svc")"
  if [[ "$old" != "$new" ]]; then
    set_state "$svc" "$new"
    if [[ "$new" == "down" ]]; then
      notify "❌ <b>${svc}</b> DOWN — ${reason}"
    else
      notify "✅ <b>${svc}</b> recovered"
    fi
  fi
}

for svc in "${SERVICES[@]}"; do
  # 1) container running?
  if ! docker inspect -f '{{.State.Running}}' "$svc" 2>/dev/null | grep -q true; then
    transition "$svc" down "container not running"
    continue
  fi
  # 2) HTTP probe (accept any 2xx/3xx/401 — qBit/Bazarr may 401 without auth, that still means up)
  url="${HTTP_PROBES[$svc]:-}"
  if [[ -n "$url" ]]; then
    code="$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || echo 000)"
    if [[ "$code" =~ ^(2|3)[0-9][0-9]$ ]] || [[ "$code" == "401" ]] || [[ "$code" == "403" ]]; then
      transition "$svc" ok ""
    else
      transition "$svc" down "HTTP ${code} on ${url}"
    fi
  else
    transition "$svc" ok ""
  fi
done

# --- disk space probe for /mnt/media (warn at <10% free) ---
free_pct=$(df --output=pcent /mnt/media | tail -1 | tr -d ' %')
used_pct=$free_pct
if (( used_pct >= 90 )); then
  transition "_disk" down "/mnt/media is ${used_pct}% full"
else
  transition "_disk" ok ""
fi
