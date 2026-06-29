"""
desktop_export tests — pure python3:
    python3 handoff/tests/test_desktop_export.py

Covers review P1/P2 fixes:
- read-only snapshot does not touch the live db, bundle includes memory, temp snapshot cleaned up
- mark/release the entire parent chain together (not just a single session)
- when schema lacks archived/handoff columns, use a sidecar table, no OperationalError
- secrets do not enter the bundle: symlink-following leaks are blocked
- CLI bundle output file permission is 0600
"""
import os
import sqlite3
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import desktop_export as de  # noqa: E402

# schema A: has archived + handoff columns (≈ 0.16.0)
_DDL_FULL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL,
    title TEXT, message_count INTEGER DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
    handoff_state TEXT, handoff_platform TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content,''));
END;
"""

# schema B: old version, sessions has no archived / handoff columns (reproduces reviewer P1)
_DDL_OLD = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL, title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, timestamp REAL NOT NULL
);
"""


def _mk(home, ddl, chain=False):
    db = os.path.join(home, "state.db")
    c = sqlite3.connect(db)
    c.executescript(ddl)
    c.execute("INSERT INTO sessions(id,source,started_at,title) VALUES('S-root','cli',1.0,'t')")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('S-root','user','hi',1.1)")
    if chain:
        c.execute("INSERT INTO sessions(id,source,parent_session_id,started_at,title) "
                  "VALUES('S-child','cli','S-root',2.0,'t2')")
        c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('S-child','assistant','yo',2.1)")
    c.commit(); c.close()
    return db


def _sval(db, sid, col):
    c = sqlite3.connect(db)
    try:
        v = c.execute(f"SELECT {col} FROM sessions WHERE id=?", (sid,)).fetchone()
        return v[0] if v else None
    finally:
        c.close()


def run():
    # ── Test A: full schema, chain, read-only, memory, full-chain marking ──
    home = tempfile.mkdtemp()
    db = _mk(home, _DDL_FULL, chain=True)
    md = os.path.join(home, "memories"); os.makedirs(md)
    open(os.path.join(md, "MEMORY.md"), "w").write("- fact A\n")
    open(os.path.join(home, ".env"), "w").write("OPENAI_API_KEY=sk-secret\n")

    bundle = de.export_for_handoff(home, "S-child", source_device="desk")
    assert set(bundle["session_ids"]) == {"S-root", "S-child"}
    assert bundle["memory"] == {"MEMORY.md": "- fact A\n"}
    assert "sk-secret" not in str(bundle)
    assert _sval(db, "S-root", "archived") == 0, "read-only export must not touch the live db"
    assert not [f for f in os.listdir(tempfile.gettempdir())
                if f.startswith("hermes-handoff-snap-")], "snapshot temp file should be cleaned up"

    # full-chain marking (P2): marking child → root must be marked too
    de.mark_handed_off(db, "S-child", platform="android")
    for sid in ("S-root", "S-child"):
        assert _sval(db, sid, "archived") == 1, f"{sid} should be archived (entire chain)"
        assert _sval(db, sid, "handoff_state") == "completed", sid
    de.release_handoff(db, "S-child")
    for sid in ("S-root", "S-child"):
        assert _sval(db, sid, "archived") == 0 and _sval(db, sid, "handoff_state") == "failed"
        assert _sval(db, sid, "handoff_platform") is None, "release should clear handoff_platform"

    # ── Test B: old schema (< 0.16.0, no handoff columns) → fail-fast, clear error ──
    # Policy: only support 0.16.0+ desktop. Both export and mark must be blocked and
    # raise UnsupportedDesktopError; no OperationalError, and no more sidecar soft-lock.
    home2 = tempfile.mkdtemp()
    db2 = _mk(home2, _DDL_OLD, chain=True)
    for fn in (lambda: de.export_for_handoff(home2, "S-child"),
               lambda: de.mark_handed_off(db2, "S-child")):
        try:
            fn()
            assert False, "old schema should raise UnsupportedDesktopError"
        except de.UnsupportedDesktopError as e:
            assert "0.16.0" in str(e), f"error message should mention 0.16.0+ required, got: {e}"

    # ── Test C: symlink does not leak secrets (reviewer P2 security) ──
    home3 = tempfile.mkdtemp()
    _mk(home3, _DDL_FULL)
    md3 = os.path.join(home3, "memories"); os.makedirs(md3)
    open(os.path.join(home3, ".env"), "w").write("OPENAI_API_KEY=sk-LEAK\n")
    open(os.path.join(md3, "real.md"), "w").write("- ok\n")
    os.symlink(os.path.join(home3, ".env"), os.path.join(md3, "SECRET.md"))
    b3 = de.export_for_handoff(home3, "S-root")
    assert b3["memory"] == {"real.md": "- ok\n"}, f"symlink should be blocked, got {b3['memory']}"
    assert "sk-LEAK" not in str(b3), "secrets must not leak into the bundle via symlink"

    # ── Test D: CLI bundle output 0600 ──
    home4 = tempfile.mkdtemp()
    _mk(home4, _DDL_FULL)
    out = os.path.join(home4, "bundle.json")
    rc = de._main(["--home", home4, "--session", "S-root", "--out", out])
    assert rc == 0
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o600, f"bundle should be 0600, got {oct(mode)}"

    print("✅ all passed (A read-only/memory/full-chain marking · B old-schema-fail-fast · C symlink leak prevention · D 0600)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
