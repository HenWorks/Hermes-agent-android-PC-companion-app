"""hermes 接力（桌面側）獨立 CLI：
    python -m handoff serve [--home ~/.hermes]                  # 啟動 server（前景）
    python -m handoff pair  [--home ~/.hermes]                  # 啟動並印出配對 QR
    python -m handoff handoff --session <id> [--home ~/.hermes] # 啟動並印出接力 QR
"""
from __future__ import annotations

import argparse
import sys
import time

from . import handoff_qr, pairing_qr, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="handoff", description="hermes 接力（桌面側）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("serve", "啟動接力 server（前景）"),
                        ("pair", "啟動並印出配對 QR 內容"),
                        ("handoff", "啟動並印出指定 session 的接力 QR")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--home", default=None, help="HERMES_HOME（預設 ~/.hermes）")
        if name == "handoff":
            s.add_argument("--session", required=True, help="要接力給手機的 session_id")
    a = ap.parse_args(argv)

    srv = serve(a.home)
    print(f"[handoff] device_id={srv.identity.device_id} port={srv.port}（mDNS 廣告中）")
    if a.cmd == "pair":
        print("配對 QR 內容（給手機掃）：")
        print(pairing_qr(srv))
    elif a.cmd == "handoff":
        print(f"接力 QR 內容（給手機掃，session={a.session}）：")
        print(handoff_qr(srv, a.session))
    print("server 執行中，Ctrl+C 結束。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
