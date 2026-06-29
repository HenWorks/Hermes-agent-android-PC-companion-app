"""
handoff_core unit tests — runs on pure python3 (no pytest dependency):
    python3 handoff/tests/test_handoff_core.py

Verifies: export→import round-trip, the entire parent chain is carried, natural-key
dedup, no overwriting of existing local data, idempotency (repeated imports produce no
duplicates), the FTS trigger does not block imports, memory append-union safe merge.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handoff_core as hc  # noqa: E402

# representative schema (with AUTOINCREMENT message id + FTS5 trigger, close to the real state.db)
_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    parent_session_id TEXT,
    started_at REAL,
    title TEXT,
    message_count INTEGER DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content,''));
END;
"""


def _mkdb(path):
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.commit()
    conn.close()


def _add_session(path, sid, parent=None, title="", started=0.0):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO sessions(id, source, parent_session_id, started_at, title) "
        "VALUES (?,?,?,?,?)", (sid, "cli", parent, started, title))
    conn.commit(); conn.close()


def _add_msg(path, sid, role, content, ts):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
        (sid, role, content, ts))
    conn.commit(); conn.close()


def _msgs(path, sid):
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages WHERE session_id=? "
        "ORDER BY timestamp", (sid,)).fetchall()
    conn.close()
    return [(r["role"], r["content"], r["timestamp"]) for r in rows]


def _fts_count(path):
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
    conn.close(); return n


def run():
    tmp = tempfile.mkdtemp()
    desktop = os.path.join(tmp, "desktop.db")
    phone = os.path.join(tmp, "phone.db")
    _mkdb(desktop); _mkdb(phone)

    # desktop: a conversation chain compacted/split into parent→child
    _add_session(desktop, "S-root", None, "Trip planning", 100.0)
    _add_session(desktop, "S-child", "S-root", "Trip planning (cont)", 200.0)
    _add_msg(desktop, "S-root", "user", "plan a trip", 101.0)
    _add_msg(desktop, "S-root", "assistant", "sure!", 102.0)
    _add_msg(desktop, "S-child", "user", "add hotels", 201.0)
    _add_msg(desktop, "S-child", "assistant", "done", 202.0)

    # phone: already has an unrelated conversation of its own (verify it is not overwritten)
    _add_session(phone, "P-own", None, "Phone note", 50.0)
    _add_msg(phone, "P-own", "user", "remind me", 51.0)

    # 1. export (triggered from child, should carry the entire chain)
    bundle = hc.export_session(desktop, "S-child", source_device="desk1")
    assert bundle is not None, "export should succeed"
    assert set(bundle["session_ids"]) == {"S-root", "S-child"}, \
        f"the entire parent chain should be carried, got {bundle['session_ids']}"
    assert len(bundle["sessions"]) == 2
    assert len(bundle["messages"]) == 4
    assert all("id" not in m for m in bundle["messages"]), "messages should not contain the source autoincrement id"

    # 2. import into phone
    st = hc.import_bundle(phone, bundle)
    assert st["sessions_added"] == 2, st
    assert st["messages_added"] == 4, st
    assert st["messages_skipped"] == 0, st

    # 3. verify the merge result
    assert _msgs(phone, "S-root") == [("user", "plan a trip", 101.0),
                                      ("assistant", "sure!", 102.0)]
    assert _msgs(phone, "S-child") == [("user", "add hotels", 201.0),
                                       ("assistant", "done", 202.0)]
    # the phone's own conversation is untouched
    assert _msgs(phone, "P-own") == [("user", "remind me", 51.0)], "existing local data must not be touched"
    # FTS is built automatically by the trigger: 4 (imported) + 1 (local) = 5
    assert _fts_count(phone) == 5, f"FTS should be maintained automatically, got {_fts_count(phone)}"

    # 4. idempotency: re-importing the same bundle → all deduped, zero added
    st2 = hc.import_bundle(phone, bundle)
    assert st2["messages_added"] == 0 and st2["messages_skipped"] == 4, st2
    assert st2["sessions_existing"] == 2, st2
    assert _fts_count(phone) == 5, "re-import should not produce duplicates"

    # 5. incremental: desktop adds one message → re-export and import → only the new one is added
    _add_msg(desktop, "S-child", "user", "any flights?", 203.0)
    st3 = hc.import_bundle(phone, hc.export_session(desktop, "S-root"))
    assert st3["messages_added"] == 1 and st3["messages_skipped"] == 4, st3

    # 6. session not found
    assert hc.export_session(desktop, "NOPE") is None

    # 7. memory append-union: no overwrite, only append new lines, keep existing
    local_mem = "- user likes tea\n- user is in Taipei\n"
    incoming = "- user likes tea\n- user prefers dark mode\n"
    merged = hc.merge_memory_text(local_mem, incoming)
    assert "user likes tea" in merged
    assert "user is in Taipei" in merged, "existing local memory must not be dropped"
    assert "user prefers dark mode" in merged, "new memory should be merged in"
    assert merged.count("user likes tea") == 1, "duplicate lines should not be re-added"
    # when there is nothing new, return as-is
    assert hc.merge_memory_text(local_mem, "- user likes tea\n") == local_mem

    # 8. import_memory: create new file, merge existing file, no change when nothing new, reject path traversal
    home = tempfile.mkdtemp()
    mem_dir = os.path.join(home, "memories")
    os.makedirs(mem_dir)
    with open(os.path.join(mem_dir, "USER.md"), "w", encoding="utf-8") as f:
        f.write("- user is in Taipei\n")
    mst = hc.import_memory(home, {
        "USER.md": "- user is in Taipei\n- user prefers dark mode\n",  # merge
        "NEW.md": "- a brand new note\n",                               # new file
        "../escape.md": "- should never be written\n",                 # path traversal → reject
        "sub/dir.md": "- subdir not allowed\n",                        # subdirectory → reject
        "notmd.txt": "- wrong ext\n",                                  # not .md → reject
    })
    assert mst["mem_added"] == 1 and mst["mem_merged"] == 1, mst
    user_after = open(os.path.join(mem_dir, "USER.md"), encoding="utf-8").read()
    assert "user is in Taipei" in user_after and "user prefers dark mode" in user_after
    assert os.path.isfile(os.path.join(mem_dir, "NEW.md"))
    assert not os.path.exists(os.path.join(home, "escape.md")), "path traversal must not write outside memories/"
    assert not os.path.exists(os.path.join(mem_dir, "sub")), "subdirectory must not be created"
    assert not os.path.exists(os.path.join(mem_dir, "notmd.txt"))
    # idempotency: re-importing the same memory → all unchanged
    mst2 = hc.import_memory(home, {"USER.md": user_after, "NEW.md": "- a brand new note\n"})
    assert mst2["mem_unchanged"] == 2 and mst2["mem_merged"] == 0 and mst2["mem_added"] == 0, mst2

    # 9. import_all: state.db + memories together, returns merged stats
    home2 = tempfile.mkdtemp()
    _mkdb(os.path.join(home2, "state.db"))
    bundle_mem = hc.export_session(desktop, "S-root")
    bundle_mem["memory"] = {"NOTES.md": "- shared note\n"}
    allst = hc.import_all(home2, bundle_mem)
    assert allst["sessions_added"] >= 1 and allst["mem_added"] == 1, allst
    assert os.path.isfile(os.path.join(home2, "memories", "NOTES.md"))

    print("✅ all passed (9 groups): export/parent-chain/dedup/no-overwrite/FTS/idempotency/incremental/memory-union/"
          "import_memory+path-traversal-protection/import_all")
    return 0


if __name__ == "__main__":
    sys.exit(run())
