"""
hermes handoff — desktop plugin entry point (#5b).

Place under `~/.hermes/plugins/handoff/`. When hermes starts and loads the plugin
it calls register(ctx) → starts a HandoffServer in the background (mDNS advertising +
encrypted TCP serve), handing off conversations to paired phones.
Does not touch upstream Electron; only reads/writes ~/.hermes.

Can also run standalone without the plugin system (testing/manual):
    python -m handoff serve            # start server (foreground)
    python -m handoff pair             # start and print pairing QR content
Dependencies: PyNaCl, zeroconf (see requirements.txt).
"""
from __future__ import annotations

import os
import sys
import threading

# Keep flat imports for in-package modules (import handoff_core / pairing ..., consistent with tests)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import desktop_export as _de  # noqa: E402
import handoff_server as _hs  # noqa: E402
import pairing as _pr         # noqa: E402

__all__ = ["register", "serve", "pairing_qr", "handoff_qr"]

_HANDOFF_SUBDIR = "handoff"   # ~/.hermes/handoff/ (identity key + peer list)


def _hermes_home(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def serve(home: str | None = None, advertise: bool = True) -> _hs.HandoffServer:
    """Start the handoff server (bound to local ~/.hermes). Returns the started HandoffServer."""
    home = _hermes_home(home)
    cfg_dir = os.path.join(home, _HANDOFF_SUBDIR)
    os.makedirs(cfg_dir, exist_ok=True)
    identity = _pr.load_or_create_identity(os.path.join(cfg_dir, "id.key"))
    peers = _hs.PeerStore(os.path.join(cfg_dir, "peers.json"))
    server = _hs.HandoffServer(
        identity=identity, peers=peers,
        export_fn=lambda sid: _de.export_for_handoff(
            home, sid, source_device=identity.device_id))  # host binds to LAN IP by default, never 0.0.0.0
    server.start(advertise=advertise)
    return server


def pairing_qr(server: _hs.HandoffServer) -> str:
    """Produce this server's pure pairing QR content (for the phone to scan, establishes trust only)."""
    return _pr.build_pair_qr(server.identity, _hs._local_ip(), server.port)


def handoff_qr(server: _hs.HandoffServer, session_id: str) -> str:
    """Produce this server's handoff QR content: pairing info + the given session_id. The phone's
    first scan both pairs and selects the conversation to receive."""
    return _pr.build_handoff_qr(server.identity, _hs._local_ip(), server.port, session_id)


def register(ctx) -> None:  # noqa: ARG001 — ctx interface per the hermes plugin system
    """hermes plugin entry point: start the handoff server (background daemon thread, tied to the hermes process lifecycle)."""
    def _start():
        try:
            srv = serve()
            # Log device_id / port so the user can find pairing info in the desktop UI/logs
            print(f"[handoff] handoff server started device_id={srv.identity.device_id} "
                  f"port={srv.port} (mDNS advertising)")
        except Exception as e:  # noqa: BLE001 — a plugin startup failure should not bring down hermes
            print(f"[handoff] startup failed: {e}", file=sys.stderr)
    threading.Thread(target=_start, name="handoff-server", daemon=True).start()
