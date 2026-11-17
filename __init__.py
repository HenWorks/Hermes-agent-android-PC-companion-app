"""Hermes plugin entry for the PC companion.

The implementation lives in ``handoff/`` (see ``handoff/__init__.py``). This shim
exists only so the repository root is a loadable Hermes plugin: without it,
``hermes plugins install HenWorks/Hermes-agent-android-PC-companion-app`` warns
"It may not be a valid Hermes plugin" and installs an inert folder that never
registers anything.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from handoff import handoff_qr, pairing_qr, register, serve  # noqa: E402

__all__ = ["register", "serve", "pairing_qr", "handoff_qr"]
