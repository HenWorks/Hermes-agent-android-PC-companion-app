"""
desktop_export — desktop-side handoff export (M1, desktop -> phone).

Pure standard library. Responsibilities:
1. Take a "consistent snapshot" of the live state.db via sqlite3.backup() (does not
   touch WAL, immune to concurrent writes), then extract a single session's connected
   chain from the snapshot -> bundle (reuses handoff_core.export_session).
2. Collect a snapshot of memories/*.md into the bundle (phone side does append-union).
3. Single-owner lock: the export itself is **read-only**; only after transfer is
   confirmed do we call mark_handed_off() to mark the source session
   handoff_state='completed' + archived=1 (after which the desktop no longer writes to
   that session). On failure, release_handoff() unlocks it for retry.

🔴 Secrets never enter the bundle: this module only reads state.db + memories/, and
never touches auth.json/.env.

Later, #5b will wrap these functions into a hermes plugin (register(ctx) background
service); M1 verifies via CLI first:
    python3 desktop_export.py --home ~/.hermes --session <uuid> --out bundle.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff_core as hc  # noqa: E402


def _snapshot(state_db: str) -> str:
    """Take a consistent snapshot of state.db into a temp file via sqlite3.backup(),
    returning the snapshot path.

    Mirrors upstream backup.py: open the source read-only + backup() API -> safe even
    while hermes is writing, and does not copy -wal/-shm. The caller is responsible for
    deleting the returned temp file.
    """
    fd, snap = tempfile.mkstemp(prefix="hermes-handoff-snap-", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return snap


def _read_memory(hermes_home: str) -> dict:
    """Read memories/*.md into {filename: content}. Returns empty if the dir is absent.

    Security (review P2): **skip symlinks / non-regular files**, and each file's realpath
    must reside within memories/ — guards against a symlink like
    `memories/SECRET.md -> ../.env` reading secrets into the bundle.
    """
    mem_dir = os.path.join(hermes_home, "memories")
    out = {}
    if not os.path.isdir(mem_dir):
        return out
    real_mem = os.path.realpath(mem_dir)
    for path in sorted(glob.glob(os.path.join(mem_dir, "*.md"))):
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue  # skip symlinks and non-regular files
            rp = os.path.realpath(path)
            if os.path.commonpath([rp, real_mem]) != real_mem:
                continue  # realpath escapes memories/ -> refuse to read
            with open(path, encoding="utf-8") as f:
                out[os.path.basename(path)] = f.read()
        except (OSError, ValueError):
            pass
    return out


def export_for_handoff(hermes_home: str, session_id: str,
                       source_device: str = "", include_memory: bool = True) -> dict | None:
    """Produce a handoff bundle (read-only, does not touch the live db). Returns None
    if the session is not found."""
    state_db = os.path.join(hermes_home, "state.db")
    if not os.path.isfile(state_db):
        raise FileNotFoundError(f"state.db not found: {state_db}")
    snap = _snapshot(state_db)
    try:
        # Only 0.16.0+ desktops are supported: fail-fast on schema mismatch (no bundle)
        guard = sqlite3.connect(snap)
        try:
            _require_handoff_schema(guard)
        finally:
            guard.close()
        bundle = hc.export_session(
            snap, session_id, source_device=source_device, exported_at=time.time())
        if bundle is None:
            return None
        if include_memory:
            bundle["memory"] = _read_memory(hermes_home)
        return bundle
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass


# ── Supported-version guard (only 0.16.0+ desktops, schema_version >= 15) ──────────────
# Policy: only 0.16.0+ desktops are supported -> if handoff columns are missing, fail-fast
# with a clear error (no more sidecar soft-lock).
_REQUIRED_LOCK_COLS = ("handoff_state", "handoff_platform", "archived")
_MIN_SCHEMA_VERSION = 15  # hermes 0.16.0


class UnsupportedDesktopError(RuntimeError):
    """The desktop hermes version is too old (< 0.16.0) and does not support handoff."""


def _require_handoff_schema(conn: sqlite3.Connection) -> None:
    """handoff "column capability" guard: sessions must have
    handoff_state/handoff_platform/archived.

    These three columns were introduced in 0.16.0 (schema v15), so missing columns
    ≈ desktop < 0.16.0. The actual check looks at whether the columns exist rather than a
    strict schema_version (even without a schema_version table, handoff is allowed as long
    as the three columns are present); schema_version is only used to enrich the error
    message.
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    missing = [c for c in _REQUIRED_LOCK_COLS if c not in have]
    if missing:
        ver = None
        try:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            ver = row[0] if row else None
        except sqlite3.Error:
            pass
        raise UnsupportedDesktopError(
            f"Desktop hermes-agent is missing handoff columns {missing} "
            f"(schema_version={ver}, requires 0.16.0+ / schema v{_MIN_SCHEMA_VERSION}). "
            f"Please update the desktop version.")


# ── Single-owner lock (writes the live db; only called after transfer is confirmed) ────
# Mark/unlock "all" sessions in the bundle's entire chain together (the export carries the
# whole chain, so marking only one would miss some).
# Assumes a 0.16.0+ schema (columns guaranteed to exist by the guard).

def _chain_for(conn: sqlite3.Connection, session_id: str) -> list:
    return hc.resolve_chain(conn, session_id) or [session_id]


def _set_lock(state_db: str, session_id: str, *, state: str, archived: int,
              platform: str | None) -> None:
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row  # required by resolve_chain
    try:
        _require_handoff_schema(conn)
        with conn:
            for sid in _chain_for(conn, session_id):
                if platform is not None:
                    # Lock: record the current handoff target platform
                    conn.execute(
                        "UPDATE sessions SET handoff_state=?, handoff_platform=?, "
                        "archived=? WHERE id=?", (state, platform, archived, sid))
                else:
                    # Unlock (release): clear handoff_platform (semantics = no locked platform)
                    conn.execute(
                        "UPDATE sessions SET handoff_state=?, handoff_platform=NULL, "
                        "archived=? WHERE id=?", (state, archived, sid))
    finally:
        conn.close()


def mark_handed_off(state_db: str, session_id: str, platform: str = "android") -> None:
    """After a successful transfer: mark the entire chain as handed off + archived
    (single owner; the desktop no longer continues writing)."""
    _set_lock(state_db, session_id, state="completed", archived=1, platform=platform)


def release_handoff(state_db: str, session_id: str) -> None:
    """On a failed transfer: unlock the entire chain (retryable)."""
    _set_lock(state_db, session_id, state="failed", archived=0, platform=None)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hermes handoff: desktop-side export of a single conversation bundle")
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"),
                    help="HERMES_HOME (default ~/.hermes)")
    ap.add_argument("--session", required=True, help="session id to hand off (UUID)")
    ap.add_argument("--out", required=True, help="output bundle.json path")
    ap.add_argument("--device", default="desktop", help="source device id")
    ap.add_argument("--no-memory", action="store_true", help="exclude memory")
    ap.add_argument("--mark", action="store_true",
                    help="mark as handed off immediately after export (for testing; in "
                         "production this should happen after transfer is confirmed)")
    a = ap.parse_args(argv)

    bundle = export_for_handoff(a.home, a.session, source_device=a.device,
                                include_memory=not a.no_memory)
    if bundle is None:
        print(f"session not found: {a.session}", file=sys.stderr)
        return 1
    # Security (review P2): the bundle contains conversation + memory -> must not become
    # 0644 (locally readable) under umask 022. Write to a 0600 temp file in the same
    # directory (mkstemp defaults to 0600), then os.replace atomically.
    out_dir = os.path.dirname(os.path.abspath(a.out)) or "."
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False)
        os.replace(tmp, a.out)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    mem = len(bundle.get("memory", {}))
    print(f"export OK -> {a.out}: chain {len(bundle['session_ids'])} session(s), "
          f"{len(bundle['messages'])} message(s), {mem} memory file(s)")
    if a.mark:
        mark_handed_off(os.path.join(a.home, "state.db"), a.session)
        print(f"marked {a.session} as handed off (archived)")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
