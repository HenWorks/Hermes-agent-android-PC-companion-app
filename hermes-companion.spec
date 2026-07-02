# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Hermes companion.
#   macOS  → a proper .app bundle (menu-bar / LSUIElement agent, double-clickable)
#   Win/Linux → a one-file executable
# The handoff package uses flat imports (import pairing, import handoff_server, ...),
# so we add handoff/ to pathex and declare the submodules as hidden imports.
import sys

is_mac = sys.platform == "darwin"

hidden = [
    "mesh_broker", "companion_web", "pairing", "handoff_server",
    "desktop_export", "handoff_core", "companion_app", "i18n",
    "nacl", "nacl.public", "nacl.signing", "nacl.bindings", "nacl.encoding",
    # PyNaCl talks to libsodium through cffi → the compiled backend must be bundled,
    # or the app crashes at launch with "No module named '_cffi_backend'".
    "cffi", "_cffi_backend",
    "zeroconf", "qrcode", "PIL", "PIL.Image", "PIL.ImageDraw", "pystray",
]

a = Analysis(
    ["companion.py"],
    pathex=["handoff"],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure, a.zipped_data)

if is_mac:
    # .app bundle: no console window; the tray icon + browser console are the UI.
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name="hermes-companion", debug=False, strip=False, upx=False,
        console=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, name="hermes-companion",
    )
    app = BUNDLE(
        coll,
        name="Hermes Companion.app",
        icon=None,
        bundle_identifier="com.henworks.hermes-companion",
        info_plist={
            "LSUIElement": True,          # menu-bar agent, no Dock icon
            "CFBundleName": "Hermes Companion",
            "CFBundleDisplayName": "Hermes Companion",
            "NSHighResolutionCapable": True,
        },
    )
else:
    # Windows / Linux: single-file executable.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        name="hermes-companion", debug=False, strip=False, upx=False,
        console=True,   # keep logs visible; the browser console is the GUI
        target_arch=None, codesign_identity=None, entitlements_file=None,
    )
