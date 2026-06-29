"""
hermes 接力 — 桌面 plugin 入口（#5b）。

放到 `~/.hermes/plugins/handoff/`，hermes 啟動載入 plugin 時呼叫 register(ctx) →
在背景啟動 HandoffServer（mDNS 廣告 + 加密 TCP serve），把對話接力給已配對的手機。
不碰上游 Electron，只讀寫 ~/.hermes。

也可不靠 plugin 系統獨立執行（測試/手動）：
    python -m handoff serve            # 啟動 server（前景）
    python -m handoff pair             # 啟動並印出配對 QR 內容
相依：PyNaCl, zeroconf（見 requirements.txt）。
"""
from __future__ import annotations

import os
import sys
import threading

# 讓套件內模組維持 flat import（import handoff_core / pairing ...，與測試一致）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import desktop_export as _de  # noqa: E402
import handoff_server as _hs  # noqa: E402
import pairing as _pr         # noqa: E402

__all__ = ["register", "serve", "pairing_qr", "handoff_qr"]

_HANDOFF_SUBDIR = "handoff"   # ~/.hermes/handoff/（身分金鑰 + 配對方清單）


def _hermes_home(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def serve(home: str | None = None, advertise: bool = True) -> _hs.HandoffServer:
    """啟動接力 server（綁本機 ~/.hermes）。回傳已啟動的 HandoffServer。"""
    home = _hermes_home(home)
    cfg_dir = os.path.join(home, _HANDOFF_SUBDIR)
    os.makedirs(cfg_dir, exist_ok=True)
    identity = _pr.load_or_create_identity(os.path.join(cfg_dir, "id.key"))
    peers = _hs.PeerStore(os.path.join(cfg_dir, "peers.json"))
    server = _hs.HandoffServer(
        identity=identity, peers=peers,
        export_fn=lambda sid: _de.export_for_handoff(
            home, sid, source_device=identity.device_id))  # host 預設綁 LAN IP，絕不 0.0.0.0
    server.start(advertise=advertise)
    return server


def pairing_qr(server: _hs.HandoffServer) -> str:
    """產生此 server 的純配對 QR 內容（給手機掃，僅建立信任）。"""
    return _pr.build_pair_qr(server.identity, _hs._local_ip(), server.port)


def handoff_qr(server: _hs.HandoffServer, session_id: str) -> str:
    """產生此 server 的接力 QR 內容：配對資訊 + 指定 session_id。手機首掃即配對 +
    選定要接收的對話。"""
    return _pr.build_handoff_qr(server.identity, _hs._local_ip(), server.port, session_id)


def register(ctx) -> None:  # noqa: ARG001 — ctx 介面依 hermes plugin 系統
    """hermes plugin 進入點：啟動接力 server（背景 daemon thread，隨 hermes 程序生命週期）。"""
    def _start():
        try:
            srv = serve()
            # 記錄 device_id / port，方便使用者在桌面 UI/日誌找配對資訊
            print(f"[handoff] 接力 server 啟動 device_id={srv.identity.device_id} "
                  f"port={srv.port}（mDNS 廣告中）")
        except Exception as e:  # noqa: BLE001 — plugin 啟動失敗不該拖垮 hermes
            print(f"[handoff] 啟動失敗：{e}", file=sys.stderr)
    threading.Thread(target=_start, name="handoff-server", daemon=True).start()
