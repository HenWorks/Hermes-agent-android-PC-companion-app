"""hermes handoff (desktop side) standalone CLI:
    python -m handoff serve [--home ~/.hermes]                  # start server (foreground)
    python -m handoff pair  [--home ~/.hermes]                  # start and print pairing QR
    python -m handoff handoff --session <id> [--home ~/.hermes] # start and print handoff QR
"""
from __future__ import annotations

import argparse
import sys
import time

from . import handoff_qr, pairing_qr, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="handoff", description="hermes handoff (desktop side)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("serve", "start the handoff server (foreground)"),
                        ("pair", "start and print the pairing QR content"),
                        ("handoff", "start and print the handoff QR for a given session")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--home", default=None, help="HERMES_HOME (default ~/.hermes)")
        if name == "handoff":
            s.add_argument("--session", required=True, help="the session_id to hand off to the phone")
    a = ap.parse_args(argv)

    srv = serve(a.home)
    print(f"[handoff] device_id={srv.identity.device_id} port={srv.port} (mDNS advertising)")
    if a.cmd == "pair":
        print("Pairing QR content (for the phone to scan):")
        print(pairing_qr(srv))
    elif a.cmd == "handoff":
        print(f"Handoff QR content (for the phone to scan, session={a.session}):")
        print(handoff_qr(srv, a.session))
    print("Server running, press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
