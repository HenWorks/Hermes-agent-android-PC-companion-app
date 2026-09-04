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
    # 🚨 argv 一定要往下傳。原本兩條路都是無參數呼叫，於是打包版在任何有 GUI 的桌面上
    # 都會靜默吃掉 --session / --host / --port / --home（公開 repo issue #1）。
    argv = sys.argv[1:]
    try:
        import companion_app
        return companion_app.main(argv)
    except Exception as e:  # noqa: BLE001 — no display / no tray backend → console mode
        print(f"(tray unavailable: {e}; running in console mode)")
        import mesh_broker
        return mesh_broker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
