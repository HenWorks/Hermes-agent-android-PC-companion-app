"""System-tray launcher for the Hermes companion (cross-platform).

Runs the broker (mesh + handoff) and the local browser console in the background,
and puts a tray / menu-bar icon with: status, Open console, Reopen pairing, Quit.
Menu labels are localized (see i18n.py). Falls back cleanly if there is no display
(e.g. a headless server) — the caller can then run the CLI instead.

Requires pystray + pillow (declared in requirements.txt). On Linux a tray backend
(AppIndicator/GTK or Xorg) must be present; otherwise this raises and the caller
should fall back to the console CLI.
"""
from __future__ import annotations

import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mesh_broker as mb       # noqa: E402
import companion_web as cw     # noqa: E402
from i18n import t             # noqa: E402


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(59, 130, 207, 255))   # brand blue disc
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))  # simple inner dot
    return img


def main() -> int:
    # pystray import first so a missing module surfaces before we start the broker.
    import pystray

    broker = mb.serve()
    # From here the broker owns the fixed port. If the tray fails to come up (no display
    # backend, Icon/run() raises), stop the broker before propagating, so the caller's CLI
    # fallback isn't blocked by the still-bound port 51379.
    try:
        broker.open_pairing(300)
        try:
            web_host, web_port = cw.serve_web(broker)
            url = f"http://{web_host}:{web_port}/"
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — console is optional; tray still works without it
            url = None

        bind = f"{broker.host}:{broker.port}"
        print(t("started", id=broker.identity.device_id, bind=bind))

        def on_open(icon, item):
            if url:
                webbrowser.open(url)

        def on_reopen(icon, item):
            broker.open_pairing(300)

        def on_quit(icon, item):
            try:
                broker.stop()
            finally:
                icon.stop()

        items = [pystray.MenuItem(t("tray_running", bind=bind), None, enabled=False)]
        if url:
            items.append(pystray.MenuItem(t("tray_open_console"), on_open, default=True))
        items.append(pystray.MenuItem(t("tray_reopen_pairing"), on_reopen))
        items.append(pystray.MenuItem(t("tray_quit"), on_quit))

        icon = pystray.Icon("hermes-companion", _icon_image(), t("tray_title"), pystray.Menu(*items))
        icon.run()   # blocks on the main thread until Quit (on_quit already stopped the broker)
        return 0
    except BaseException:
        broker.stop()   # release the port so the caller can fall back to CLI cleanly
        raise


if __name__ == "__main__":
    sys.exit(main())
