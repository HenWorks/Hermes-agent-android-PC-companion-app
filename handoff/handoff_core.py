"""
handoff_core — merge core for hermes-agent conversation "handoff" (M1).

Zero external dependencies (standard library only: sqlite3 / json / hashlib), so the
desktop side and tests share one piece of verified logic; the mobile side (Kotlin)
implements an equivalent version against this spec.

Design rationale (see android/docs/plan-desktop-mobile-sync.md):
- sessions.id is TEXT(UUID) → globally unique → cross-device upsert
- messages.id is INTEGER AUTOINCREMENT → local, not global → drop the source id on
  import and dedup by natural key (session_id, timestamp, role, content) for append-union
- long conversations get compacted into a parent_session_id chain → moving one
  conversation means carrying the whole connected chain
- FTS5 is maintained automatically by DB triggers → a plain INSERT suffices, the
  importer never touches the index by hand
- secrets (auth.json/.env) are never in the payload (this module only touches
  state.db and memories/*.md)

bundle format (single session connected chain):
{
  "schema": 1,
  "source_device": "<id>",
  "root_session_id": "<uuid>",
  "session_ids": ["<uuid>", ...],          # the entire connected chain
  "sessions": [ {col: val, ...}, ... ],     # dynamic columns (per source schema)
  "messages": [ {col: val, ...}, ... ],     # source id already removed
  "exported_at": <float>,
}
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Set

SCHEMA = 1
# messages natural-key columns (for cross-device dedup; excludes the local autoincrement id)
_MSG_NATURAL_KEY = ("session_id", "timestamp", "role", "content")


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Read a table's column names dynamically (adapts to different schema versions; no hardcoded 30 columns)."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── connected-chain resolution ────────────────────────────────────────────

def resolve_chain(conn: sqlite3.Connection, session_id: str) -> List[str]:
    """Return the full parent/child chain of session ids connected to session_id (deduped, including itself).

    Compaction splits a long conversation into a chain linked by parent_session_id; what
    the user sees as "one conversation" may be several session rows. First walk up via
    parent to find the root, then collect all descendants from the root.
    """
    # 1. walk up to the root
    root = session_id
    seen_up: Set[str] = set()
    while True:
        if root in seen_up:
            break  # cycle guard
        seen_up.add(root)
        row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (root,)
        ).fetchone()
        if row is None:
            # session does not exist; return empty if it's the starting point, otherwise stop at the highest known level
            if root == session_id:
                return []
            break
        parent = row["parent_session_id"]
        if not parent:
            break
        root = parent

    # 2. breadth-collect all descendants from the root
    chain: List[str] = []
    seen: Set[str] = set()
    stack = [root]
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        chain.append(sid)
        children = conn.execute(
            "SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)
        ).fetchall()
        stack.extend(c["id"] for c in children)
    return chain


# ── export ────────────────────────────────────────────────────────────────

def export_session(db_path: str, session_id: str, source_device: str = "",
                   exported_at: float = 0.0) -> Optional[Dict[str, Any]]:
    """Extract one conversation's (connected chain) sessions + messages from state.db into a bundle.

    Returns None if the session is not found. messages already have the source autoincrement id removed.
    """
    conn = _connect(db_path)
    try:
        chain = resolve_chain(conn, session_id)
        if not chain:
            return None
        scols = _columns(conn, "sessions")
        mcols = [c for c in _columns(conn, "messages") if c != "id"]  # drop source id

        placeholders = ",".join("?" * len(chain))
        sessions = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(scols)} FROM sessions WHERE id IN ({placeholders})",
                chain,
            ).fetchall()
        ]
        messages = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(mcols)} FROM messages "
                f"WHERE session_id IN ({placeholders}) ORDER BY session_id, timestamp",
                chain,
            ).fetchall()
        ]
        return {
            "schema": SCHEMA,
            "source_device": source_device,
            "root_session_id": chain[0],
            "session_ids": chain,
            "sessions": sessions,
            "messages": messages,
            "exported_at": exported_at,
        }
    finally:
        conn.close()


def export_all(db_path: str, source_device: str = "",
               exported_at: float = 0.0) -> Dict[str, Any]:
    """Export every session + message in the whole state.db into a single bundle (#22 reverse sync).

    Used for eventual-consistency mobile→desktop sync: package up all local conversations
    at once, and the desktop merges them idempotently via import_bundle (by-id upsert +
    natural-key dedup). Same bundle format as export_session, except it does not resolve
    connected chains but dumps the entire tables. An empty db returns a valid bundle with
    empty sessions/messages (not None). messages already have the source autoincrement id
    dropped (so the peer's AUTOINCREMENT assigns new ids).
    """
    conn = _connect(db_path)
    try:
        scols = _columns(conn, "sessions")
        mcols = [c for c in _columns(conn, "messages") if c != "id"]
        sessions = [
            dict(r) for r in conn.execute(f"SELECT {','.join(scols)} FROM sessions").fetchall()
        ]
        messages = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(mcols)} FROM messages ORDER BY session_id, timestamp"
            ).fetchall()
        ]
        return {
            "schema": SCHEMA,
            "source_device": source_device,
            "session_ids": [s["id"] for s in sessions],
            "sessions": sessions,
            "messages": messages,
            "exported_at": exported_at,
        }
    finally:
        conn.close()


# ── import merge ────────────────────────────────────────────────────────────

def _natural_key(m: Dict[str, Any]) -> str:
    raw = "\x1f".join(
        "" if m.get(k) is None else str(m.get(k)) for k in _MSG_NATURAL_KEY
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_bundle(db_path: str, bundle: Dict[str, Any]) -> Dict[str, int]:
    """Merge a bundle into the local state.db. Returns statistics.

    - sessions: upsert by id (INSERT OR IGNORE, does not overwrite local existing → preserves local-side metadata)
    - messages: INSERT after natural-key dedup (drop source id, let local AUTOINCREMENT assign a new id)
    - FTS5 maintained automatically by triggers
    Idempotent: importing the same bundle repeatedly produces no duplicate messages.
    """
    if bundle.get("schema") != SCHEMA:
        raise ValueError(f"unsupported bundle schema: {bundle.get('schema')}")

    conn = _connect(db_path)
    stats = {"sessions_added": 0, "sessions_existing": 0,
             "messages_added": 0, "messages_skipped": 0}
    try:
        local_scols = set(_columns(conn, "sessions"))
        local_mcols = set(_columns(conn, "messages"))

        with conn:  # single transaction (all-or-nothing)
            # 1. sessions: upsert (does not overwrite existing)
            for s in bundle.get("sessions", []):
                cols = [c for c in s.keys() if c in local_scols]
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (s["id"],)
                ).fetchone()
                if exists:
                    stats["sessions_existing"] += 1
                    continue
                conn.execute(
                    f"INSERT OR IGNORE INTO sessions ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [s[c] for c in cols],
                )
                stats["sessions_added"] += 1

            # 2. messages: natural-key dedup → insert only new messages
            #    first load the natural-key set of existing messages for each target session
            target_sids = {m["session_id"] for m in bundle.get("messages", [])}
            existing_keys: Set[str] = set()
            for sid in target_sids:
                for r in conn.execute(
                    "SELECT session_id, timestamp, role, content "
                    "FROM messages WHERE session_id = ?", (sid,)
                ).fetchall():
                    existing_keys.add(_natural_key(dict(r)))

            for m in bundle.get("messages", []):
                key = _natural_key(m)
                if key in existing_keys:
                    stats["messages_skipped"] += 1
                    continue
                cols = [c for c in m.keys() if c in local_mcols and c != "id"]
                conn.execute(
                    f"INSERT INTO messages ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [m[c] for c in cols],
                )
                existing_keys.add(key)  # guard against duplicates within the bundle itself
                stats["messages_added"] += 1
        return stats
    finally:
        conn.close()


# ── memory append-union (M1: safe, non-overwriting, conflict-preserving) ──────

def merge_memory_text(local: str, incoming: str) -> str:
    """Merge incoming memory entries into local (line-level union), preserving local existing and only adding new lines.

    M1 safety policy: no deletion, no overwriting; "non-empty lines" present in incoming
    but not in local are appended to the end, marked with a source block. True 3-way
    (with deletion propagation) is deferred to M3 (which will use .sync-base as the common
    ancestor). Returns the merged text; if nothing is added, returns it unchanged.
    """
    local_lines = local.splitlines()
    local_set = {ln.strip() for ln in local_lines if ln.strip()}
    new_lines = [
        ln for ln in incoming.splitlines()
        if ln.strip() and ln.strip() not in local_set
    ]
    if not new_lines:
        return local
    sep = "" if local.endswith("\n") or local == "" else "\n"
    block = "\n<!-- merged from handoff -->\n" + "\n".join(new_lines) + "\n"
    return local + sep + block


def _atomic_write_text(path: str, text: str) -> None:
    """Atomically write a text file (tmp + os.replace) to avoid half-written state."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mem-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def import_memory(hermes_home: str, memory: Dict[str, str]) -> Dict[str, int]:
    """Append-union the bundle's memory ({filename: content}) into the local memories/*.md.

    Safety (filenames come from the bundle and are untrusted): only accept "plain filename"
    .md files (reject path traversal `../` and subdirectories); skip existing files that are
    symlinks (to prevent writing through to .env etc.); before writing, confirm the
    directory's realpath is still inside memories/. No deletion, no overwriting (reuses
    merge_memory_text's M1 policy). Returns statistics.
    """
    stats = {"mem_added": 0, "mem_merged": 0, "mem_unchanged": 0}
    if not memory:
        return stats
    mem_dir = os.path.join(hermes_home, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    real_mem = os.path.realpath(mem_dir)
    for name, incoming in memory.items():
        if not isinstance(name, str) or not name.endswith(".md"):
            continue
        if name in (".", "..") or os.path.basename(name) != name:
            continue  # reject path traversal / subdirectory
        path = os.path.join(mem_dir, name)
        if os.path.islink(path):
            continue  # existing symlink → unsafe, skip
        if os.path.commonpath([os.path.realpath(os.path.dirname(path)), real_mem]) != real_mem:
            continue
        existed = os.path.isfile(path)
        local = ""
        if existed:
            try:
                with open(path, encoding="utf-8") as f:
                    local = f.read()
            except OSError:
                continue
        merged = merge_memory_text(local, incoming or "")
        if existed and merged == local:
            stats["mem_unchanged"] += 1
            continue
        _atomic_write_text(path, merged)
        stats["mem_merged" if existed else "mem_added"] += 1
    return stats


def import_all(hermes_home: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Single mobile-side import entry point: merge state.db (sessions/messages) + memories/ together. Returns merge statistics."""
    db = os.path.join(hermes_home, "state.db")
    stats: Dict[str, Any] = dict(import_bundle(db, bundle))
    stats.update(import_memory(hermes_home, bundle.get("memory") or {}))
    return stats
