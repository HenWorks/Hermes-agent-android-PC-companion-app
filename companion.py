"""PyInstaller entry point for the Hermes companion.

Double-clicking the packaged binary tries to show a system-tray / menu-bar icon
(companion_app). If there's no display / tray backend (e.g. a headless server),
it falls back to console mode (mesh_broker.main), which runs the broker and opens
the local browser console. Either way the broker runs and the browser console is
the GUI; flat package imports are made importable the same way the package __init__ does.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "handoff"))


def main() -> int:
    try:
        import companion_app
        return companion_app.main()
    except Exception as e:  # noqa: BLE001 — no display / no tray backend → console mode
        print(f"(tray unavailable: {e}; running in console mode)")
        import mesh_broker
        return mesh_broker.main()


if __name__ == "__main__":
    sys.exit(main())
