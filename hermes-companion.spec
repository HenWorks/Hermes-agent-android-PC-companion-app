# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Hermes companion (one-file, cross-platform).
# The handoff package uses flat imports (import pairing, import handoff_server, ...),
# so we add handoff/ to pathex and declare the submodules as hidden imports.

block_cipher = None

hidden = [
    "mesh_broker", "companion_web", "pairing", "handoff_server",
    "desktop_export", "handoff_core",
    "nacl", "nacl.public", "nacl.signing", "nacl.bindings", "nacl.encoding",
    "zeroconf", "qrcode", "PIL", "PIL.Image",
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
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="hermes-companion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # keep a console so first-run logs / pairing text are visible; browser console is the GUI
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
