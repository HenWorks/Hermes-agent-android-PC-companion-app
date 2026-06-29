"""
desktop_export 測試 — 純 python3：
    python3 handoff/tests/test_desktop_export.py

涵蓋 review P1/P2 修復：
- 唯讀快照不動 live db、bundle 含 memory、暫存快照清除
- 整條 parent 鏈一起標記/解鎖（非只單一 session）
- schema 缺 archived/handoff 欄位時用 sidecar 表、不 OperationalError
- 機密不入 bundle：跟隨 symlink 的洩漏被擋
- CLI bundle 輸出檔權限為 0600
"""
import os
import sqlite3
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import desktop_export as de  # noqa: E402

# schema A：有 archived + handoff 欄位（≈ 0.16.0）
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

# schema B：舊版，sessions 無 archived / handoff 欄位（重現 reviewer P1）
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
    # ── Test A：full schema、chain、唯讀、memory、整條鏈標記 ──
    home = tempfile.mkdtemp()
    db = _mk(home, _DDL_FULL, chain=True)
    md = os.path.join(home, "memories"); os.makedirs(md)
    open(os.path.join(md, "MEMORY.md"), "w").write("- fact A\n")
    open(os.path.join(home, ".env"), "w").write("OPENAI_API_KEY=sk-secret\n")

    bundle = de.export_for_handoff(home, "S-child", source_device="desk")
    assert set(bundle["session_ids"]) == {"S-root", "S-child"}
    assert bundle["memory"] == {"MEMORY.md": "- fact A\n"}
    assert "sk-secret" not in str(bundle)
    assert _sval(db, "S-root", "archived") == 0, "匯出唯讀不可動 live db"
    assert not [f for f in os.listdir(tempfile.gettempdir())
                if f.startswith("hermes-handoff-snap-")], "快照暫存應清除"

    # 整條鏈標記（P2）：標 child → root 也要被標
    de.mark_handed_off(db, "S-child", platform="android")
    for sid in ("S-root", "S-child"):
        assert _sval(db, sid, "archived") == 1, f"{sid} 應 archived（整條鏈）"
        assert _sval(db, sid, "handoff_state") == "completed", sid
    de.release_handoff(db, "S-child")
    for sid in ("S-root", "S-child"):
        assert _sval(db, sid, "archived") == 0 and _sval(db, sid, "handoff_state") == "failed"
        assert _sval(db, sid, "handoff_platform") is None, "release 應清掉 handoff_platform"

    # ── Test B：舊 schema（< 0.16.0，無 handoff 欄位）→ fail-fast、清楚報錯 ──
    # 政策：只支援 0.16.0+ 桌面。export 與 mark 都要擋下、丟 UnsupportedDesktopError，
    # 不可 OperationalError、也不再 sidecar 軟鎖。
    home2 = tempfile.mkdtemp()
    db2 = _mk(home2, _DDL_OLD, chain=True)
    for fn in (lambda: de.export_for_handoff(home2, "S-child"),
               lambda: de.mark_handed_off(db2, "S-child")):
        try:
            fn()
            assert False, "舊 schema 應丟 UnsupportedDesktopError"
        except de.UnsupportedDesktopError as e:
            assert "0.16.0" in str(e), f"錯誤訊息應提示需 0.16.0+，實得：{e}"

    # ── Test C：symlink 不洩機密（reviewer P2 security）──
    home3 = tempfile.mkdtemp()
    _mk(home3, _DDL_FULL)
    md3 = os.path.join(home3, "memories"); os.makedirs(md3)
    open(os.path.join(home3, ".env"), "w").write("OPENAI_API_KEY=sk-LEAK\n")
    open(os.path.join(md3, "real.md"), "w").write("- ok\n")
    os.symlink(os.path.join(home3, ".env"), os.path.join(md3, "SECRET.md"))
    b3 = de.export_for_handoff(home3, "S-root")
    assert b3["memory"] == {"real.md": "- ok\n"}, f"symlink 應被擋，實得 {b3['memory']}"
    assert "sk-LEAK" not in str(b3), "機密不可經 symlink 洩入 bundle"

    # ── Test D：CLI bundle 輸出 0600 ──
    home4 = tempfile.mkdtemp()
    _mk(home4, _DDL_FULL)
    out = os.path.join(home4, "bundle.json")
    rc = de._main(["--home", home4, "--session", "S-root", "--out", out])
    assert rc == 0
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o600, f"bundle 應為 0600，實得 {oct(mode)}"

    print("✅ 全部通過（A 唯讀/memory/整鏈標記 · B 舊schema-fail-fast · C symlink防洩 · D 0600）")
    return 0


if __name__ == "__main__":
    sys.exit(run())
