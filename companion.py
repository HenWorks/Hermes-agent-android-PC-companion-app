"""PyInstaller entry point for the Hermes companion.

Double-clicking the packaged binary runs the broker (mesh + handoff) and opens the
local browser console automatically — that console is the GUI. The flat package
imports are made importable the same way the package's __init__ does.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "handoff"))

import mesh_broker  # noqa: E402  (resolved via the path insert above)

if __name__ == "__main__":
    sys.exit(mesh_broker.main())
