#!/usr/bin/env bash
# Hermes Mesh — desktop one-click launcher (North Star: minimize the PC side).
#
#   ./mesh-start.sh              start broker in foreground (shows pairing QR, Ctrl+C to stop)
#   ./mesh-start.sh --tailscale  start in foreground, bound to Tailscale IP (cross-network)
#
#   Background daemon switch (start at boot + run in background, macOS launchd / Linux systemd --user):
#   ./mesh-start.sh daemon on      turn on background daemon (install autostart and run immediately in background)
#   ./mesh-start.sh daemon off     turn off background daemon (stop and remove autostart)
#   ./mesh-start.sh daemon status  check background daemon status (installed/running?)
#   (legacy flags --autostart / --stop-autostart still work, equivalent to daemon on / off)
#
# Automatically creates an isolated venv (~/.hermes/mesh/venv) and installs deps (PyNaCl/zeroconf/qrcode), without polluting the system python.
# Then the phone app "Computer Collaboration" can scan the QR / paste the pairing code to connect. The broker binds to the LAN IP (never 0.0.0.0).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # handoff/
ROOT="$(cd "$HERE/.." && pwd)"                          # repo root (python -m handoff.* must run here)
VENV="$HOME/.hermes/mesh/venv"
PY="$VENV/bin/python3"
LABEL="com.hermesagent.mesh"

log() { printf '\033[36m[mesh]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[mesh] %s\033[0m\n' "$*" >&2; }

ensure_venv() {
  command -v python3 >/dev/null 2>&1 || { err "python3 not found, please install Python 3 first."; exit 1; }
  if [ ! -x "$PY" ]; then
    log "Creating isolated environment $VENV …"
    python3 -m venv "$VENV"
  fi
  log "Installing/updating dependencies (PyNaCl, zeroconf, qrcode) …"
  "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/pip" install -q "PyNaCl>=1.5" "zeroconf>=0.130" "qrcode>=7" "pillow>=9" >/dev/null
}

tailscale_ip() {
  for t in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    command -v "$t" >/dev/null 2>&1 && { "$t" ip -4 2>/dev/null | head -1; return; }
  done
  ifconfig 2>/dev/null | grep -oE 'inet 100\.[0-9.]+' | awk '{print $2}' | head -1
}

run_broker() {
  cd "$ROOT"
  if [ "${USE_TAILSCALE:-0}" = "1" ]; then
    local ts; ts="$(tailscale_ip)"
    [ -n "$ts" ] || { err "Tailscale IP not found (please start Tailscale on the desktop first)."; exit 1; }
    export MESH_HOST="$ts"
    log "Tailscale mode: broker bound to $ts (phone must be on the same tailnet)."
  fi
  log "Starting broker (Ctrl+C to stop). In the phone app \"Computer Collaboration\", scan the QR below or paste the pairing code:"
  exec "$PY" -m handoff.mesh_broker
}

install_autostart() {
  local uname_s; uname_s="$(uname -s)"
  if [ "$uname_s" = "Darwin" ]; then
    local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>handoff.mesh_broker</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.hermes/mesh/broker.log</string>
  <key>StandardErrorPath</key><string>$HOME/.hermes/mesh/broker.log</string>
</dict></plist>
PLIST
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    log "Installed macOS start-at-boot (launchd: $LABEL). The broker is now running in the background."
    log "Pairing QR: run ./mesh-start.sh once here (foreground) to scan, or see ~/.hermes/mesh/broker.log"
  else
    # Linux: systemd --user
    local unit="$HOME/.config/systemd/user/$LABEL.service"
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$unit" <<UNIT
[Unit]
Description=Hermes Mesh broker
[Service]
ExecStart=$PY -m handoff.mesh_broker
WorkingDirectory=$ROOT
Restart=always
[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now "$LABEL.service"
    log "Installed Linux start-at-boot (systemd --user: $LABEL). The broker is now running in the background."
    log "(To start at boot rather than after login: sudo loginctl enable-linger $USER)"
  fi
}

stop_autostart() {
  case "$(uname -s)" in
    Darwin) launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"; log "Removed macOS start-at-boot." ;;
    *) systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
       rm -f "$HOME/.config/systemd/user/$LABEL.service"; systemctl --user daemon-reload 2>/dev/null || true
       log "Removed Linux start-at-boot." ;;
  esac
}

daemon_status() {
  case "$(uname -s)" in
    Darwin)
      local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
      if [ -f "$plist" ]; then
        if launchctl list 2>/dev/null | grep -q "$LABEL"; then
          log "Background daemon: ✅ on (autostart installed, running). log: ~/.hermes/mesh/broker.log"
        else
          log "Background daemon: ⚠️ autostart installed but not currently running (try ./mesh-start.sh daemon on to restart)."
        fi
      else
        log "Background daemon: ⭕ off (autostart not installed). Foreground start: ./mesh-start.sh"
      fi ;;
    *)
      if systemctl --user is-enabled "$LABEL.service" >/dev/null 2>&1; then
        if systemctl --user is-active "$LABEL.service" >/dev/null 2>&1; then
          log "Background daemon: ✅ on (enabled, running)."
        else
          log "Background daemon: ⚠️ enabled but not currently running (systemctl --user status $LABEL.service)."
        fi
      else
        log "Background daemon: ⭕ off (not enabled). Foreground start: ./mesh-start.sh"
      fi ;;
  esac
}

# Background daemon switch: daemon on/off/status (a clear switch interface, replacing bare --autostart)
case "${1:-}" in
  daemon)
    case "${2:-status}" in
      on)     ensure_venv; install_autostart ;;
      off)    stop_autostart ;;
      status) daemon_status ;;
      *) err "Usage: daemon on | off | status"; exit 1 ;;
    esac ;;
  --autostart) ensure_venv; install_autostart ;;   # legacy flag compat = daemon on
  --stop-autostart) stop_autostart ;;              # legacy flag compat = daemon off
  --tailscale) ensure_venv; USE_TAILSCALE=1 run_broker ;;
  "" ) ensure_venv; run_broker ;;
  *) err "Unknown argument: $1 (available: no args = foreground LAN / --tailscale / daemon on|off|status)"; exit 1 ;;
esac
