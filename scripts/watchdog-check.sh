#!/usr/bin/env bash
# Per-minute health probe for the media stack.
# Alerts to Telegram (flint2_watchdog_bot) on transitions:
#   - container not running → recovered
#   - HTTP probe failing → recovered
#   - /mnt/media disk usage ≥90% → recovered
# State (last status per service) is kept in /var/tmp so we only alert on
# transitions; state files are refreshed every run so the directory always
# reflects current state, not just history.
set -euo pipefail

[[ -f /var/tmp/media-srv-watchdog/.paused ]] && exit 0

# --- config ---
SERVICES=(jellyfin qbittorrent prowlarr sonarr radarr bazarr jellyseerr searcharr janitorr)
declare -A HTTP_PROBES=(
  [jellyfin]="http://127.0.0.1:8096/health"
  [qbittorrent]="http://127.0.0.1:8080/"
  [prowlarr]="http://127.0.0.1:9696/ping"
  [sonarr]="http://127.0.0.1:8989/ping"
  [radarr]="http://127.0.0.1:7878/ping"
  [bazarr]="http://127.0.0.1:6767/"
  [jellyseerr]="http://127.0.0.1:5055/api/v1/status"
  [janitorr]="http://127.0.0.1:8978/actuator/health"
)
STATE_DIR="${STATE_DIR:-/var/tmp/media-srv-watchdog}"
mkdir -p "$STATE_DIR"

# --- load Telegram creds ---
# The existing ryzen4700-watchdog uses TG_TOKEN / TG_CHAT_ID / HOST_LABEL.
# Accept either TG_* or BOT_TOKEN/CHAT_ID names so either schema works.
for f in "$HOME/watchdog/config.env" /etc/watchdog/telegram.env "$HOME/.config/watchdog/telegram.env"; do
  [[ -f "$f" ]] && source "$f" && break
done
BOT_TOKEN="${BOT_TOKEN:-${TG_TOKEN:-}}"
CHAT_ID="${CHAT_ID:-${TG_CHAT_ID:-}}"
: "${BOT_TOKEN:?BOT_TOKEN/TG_TOKEN not set (looked in ~/watchdog/config.env, /etc/watchdog/telegram.env, ~/.config/watchdog/telegram.env)}"
: "${CHAT_ID:?CHAT_ID/TG_CHAT_ID not set}"

HOST="${HOST_LABEL:-$(hostname)}"
SOURCE="media-srv (docker+http probe)"
SEP="━━━━━━━━━━━━━━━━"
# Force a sensible local TZ for timestamps even if the host is UTC.
# Honour explicit TZ_NAME from watchdog config.env if present.
TZ_NAME="${TZ_NAME:-Asia/Tbilisi}"

# --- Telegram card sender ---
# Args: 1=status (down|up|warn), 2=service/label, 3=detail line
send_card() {
  local status="$1" svc="$2" detail="$3"
  local title icon
  case "$status" in
    down) icon="🔴"; title="SERVICE DOWN — INTERNAL" ;;
    up)   icon="🟢"; title="SERVICE UP — INTERNAL" ;;
    warn) icon="⚠️"; title="DISK ALERT — INTERNAL" ;;
    *)    icon="ℹ️"; title="NOTICE — INTERNAL" ;;
  esac
  local ts; ts="$(TZ="$TZ_NAME" date '+%Y-%m-%d %H:%M:%S %Z')"
  local text
  text="${icon} <b>${title}</b> (media-srv)
${SEP}
🖥 <b>From:</b> ${HOST}
🔍 <b>Source:</b> ${SOURCE}
📦 <b>Service:</b> ${svc}
ℹ️ <b>Detail:</b> ${detail}
🕒 <b>Time:</b> ${ts}"

  curl -fsS --max-time 10 \
    -d "chat_id=${CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${text}" \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" >/dev/null || true
}

set_state() { echo "$2" > "${STATE_DIR}/${1}.state"; }
get_state() { cat "${STATE_DIR}/${1}.state" 2>/dev/null || echo "ok"; }

# Args: 1=svc, 2=new_state (ok|down|warn), 3=detail (used in alerts)
transition() {
  local svc="$1" new="$2" detail="$3"
  local old; old="$(get_state "$svc")"
  # always refresh state so the dir always shows current state
  set_state "$svc" "$new"
  # only alert on real transitions
  [[ "$old" == "$new" ]] && return 0
  case "$new" in
    down) send_card down "$svc" "$detail" ;;
    warn) send_card warn "$svc" "$detail" ;;
    ok)
      if [[ "$old" == "down" ]] || [[ "$old" == "warn" ]]; then
        send_card up "$svc" "back to healthy state"
      fi
      ;;
  esac
}

# --- per-service probes ---
for svc in "${SERVICES[@]}"; do
  # 1) container running?
  if ! docker inspect -f '{{.State.Running}}' "$svc" 2>/dev/null | grep -q true; then
    transition "$svc" down "container not running"
    continue
  fi
  # 2) HTTP probe (accept 2xx/3xx, plus 401/403 which still mean "up").
  # Note: no -f flag so curl still writes the code on non-2xx; we want to see it.
  url="${HTTP_PROBES[$svc]:-}"
  if [[ -n "$url" ]]; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    [[ -z "$code" ]] && code="000"
    if [[ "$code" =~ ^(2|3)[0-9][0-9]$ ]] || [[ "$code" == "401" ]] || [[ "$code" == "403" ]]; then
      transition "$svc" ok ""
    elif [[ "$code" == "000" ]]; then
      transition "$svc" down "no HTTP response on ${url} (container starting or port closed)"
    else
      transition "$svc" down "HTTP ${code} on ${url}"
    fi
  else
    # no HTTP probe defined → container running is enough
    transition "$svc" ok ""
  fi
done

# --- disk space probe for /mnt/media (warn at ≥90% full) ---
used_pct=$(df --output=pcent /mnt/media | tail -1 | tr -d ' %')
if (( used_pct >= 90 )); then
  transition "_disk_media" warn "/mnt/media is ${used_pct}% full"
else
  transition "_disk_media" ok "/mnt/media is ${used_pct}% full"
fi
