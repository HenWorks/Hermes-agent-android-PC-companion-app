#!/usr/bin/env bash
# Hermes Mesh — 桌面一鍵啟動（北極星：PC 端最小化）。
#
#   ./mesh-start.sh              啟動 broker（前景，顯示配對 QR）
#   ./mesh-start.sh --autostart  安裝開機自啟（macOS launchd / Linux systemd --user）後啟動
#   ./mesh-start.sh --stop-autostart  移除開機自啟
#
# 自動建立隔離 venv（~/.hermes/mesh/venv）並安裝相依（PyNaCl/zeroconf/qrcode），不污染系統 python。
# 之後手機 app「電腦協作」掃 QR / 貼配對碼即可連結。broker 綁 LAN IP（絕不 0.0.0.0）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # handoff/
ROOT="$(cd "$HERE/.." && pwd)"                          # repo root（python -m handoff.* 需在此跑）
VENV="$HOME/.hermes/mesh/venv"
PY="$VENV/bin/python3"
LABEL="com.hermesagent.mesh"

log() { printf '\033[36m[mesh]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[mesh] %s\033[0m\n' "$*" >&2; }

ensure_venv() {
  command -v python3 >/dev/null 2>&1 || { err "找不到 python3，請先安裝 Python 3。"; exit 1; }
  if [ ! -x "$PY" ]; then
    log "建立隔離環境 $VENV …"
    python3 -m venv "$VENV"
  fi
  log "安裝/更新相依（PyNaCl, zeroconf, qrcode）…"
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
    [ -n "$ts" ] || { err "找不到 Tailscale IP（請先在桌面開 Tailscale）。"; exit 1; }
    export MESH_HOST="$ts"
    log "Tailscale 模式：broker 綁 $ts（手機需在同一 tailnet）。"
  fi
  log "啟動 broker（Ctrl+C 結束）。在手機 app「電腦協作」掃下方 QR 或貼配對碼："
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
    log "已安裝 macOS 開機自啟（launchd: $LABEL）。broker 已在背景執行。"
    log "配對 QR：在此跑一次 ./mesh-start.sh（前景）掃碼，或看 ~/.hermes/mesh/broker.log"
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
    log "已安裝 Linux 開機自啟（systemd --user: $LABEL）。broker 已在背景執行。"
    log "（如需開機即啟而非登入後：sudo loginctl enable-linger $USER）"
  fi
}

stop_autostart() {
  case "$(uname -s)" in
    Darwin) launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
            rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"; log "已移除 macOS 開機自啟。" ;;
    *) systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
       rm -f "$HOME/.config/systemd/user/$LABEL.service"; systemctl --user daemon-reload 2>/dev/null || true
       log "已移除 Linux 開機自啟。" ;;
  esac
}

case "${1:-}" in
  --autostart) ensure_venv; install_autostart ;;
  --stop-autostart) stop_autostart ;;
  --tailscale) ensure_venv; USE_TAILSCALE=1 run_broker ;;
  "" ) ensure_venv; run_broker ;;
  *) err "未知參數：$1（可用：--tailscale / --autostart / --stop-autostart / 無參數=LAN）"; exit 1 ;;
esac
