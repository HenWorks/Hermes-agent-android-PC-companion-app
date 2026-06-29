"""
handoff_core 單元測試 — 純 python3 可跑（無 pytest 相依）：
    python3 handoff/tests/test_handoff_core.py

驗證：export→import round-trip、parent 鏈整條帶、自然鍵去重、不覆蓋本機既有、
冪等（重複匯入無重複）、FTS trigger 不阻斷匯入、memory append-union 安全合併。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handoff_core as hc  # noqa: E402

# 代表性 schema（含 AUTOINCREMENT message id + FTS5 trigger，貼近真實 state.db）
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

    # 桌面：一則被壓縮切成 parent→child 的對話鏈
    _add_session(desktop, "S-root", None, "Trip planning", 100.0)
    _add_session(desktop, "S-child", "S-root", "Trip planning (cont)", 200.0)
    _add_msg(desktop, "S-root", "user", "plan a trip", 101.0)
    _add_msg(desktop, "S-root", "assistant", "sure!", 102.0)
    _add_msg(desktop, "S-child", "user", "add hotels", 201.0)
    _add_msg(desktop, "S-child", "assistant", "done", 202.0)

    # 手機：自己已有一則無關對話（驗證不被覆蓋）
    _add_session(phone, "P-own", None, "Phone note", 50.0)
    _add_msg(phone, "P-own", "user", "remind me", 51.0)

    # 1. 匯出（從 child 觸發，應帶整條鏈）
    bundle = hc.export_session(desktop, "S-child", source_device="desk1")
    assert bundle is not None, "export 應成功"
    assert set(bundle["session_ids"]) == {"S-root", "S-child"}, \
        f"parent 鏈應整條帶，實得 {bundle['session_ids']}"
    assert len(bundle["sessions"]) == 2
    assert len(bundle["messages"]) == 4
    assert all("id" not in m for m in bundle["messages"]), "messages 不應含來源自增 id"

    # 2. 匯入手機
    st = hc.import_bundle(phone, bundle)
    assert st["sessions_added"] == 2, st
    assert st["messages_added"] == 4, st
    assert st["messages_skipped"] == 0, st

    # 3. 驗證合併結果
    assert _msgs(phone, "S-root") == [("user", "plan a trip", 101.0),
                                      ("assistant", "sure!", 102.0)]
    assert _msgs(phone, "S-child") == [("user", "add hotels", 201.0),
                                       ("assistant", "done", 202.0)]
    # 手機自己的對話原封不動
    assert _msgs(phone, "P-own") == [("user", "remind me", 51.0)], "本機既有不可被動到"
    # FTS 由 trigger 自動建：4 (匯入) + 1 (本機) = 5
    assert _fts_count(phone) == 5, f"FTS 應自動維護，實得 {_fts_count(phone)}"

    # 4. 冪等：同 bundle 重匯入 → 全去重、零新增
    st2 = hc.import_bundle(phone, bundle)
    assert st2["messages_added"] == 0 and st2["messages_skipped"] == 4, st2
    assert st2["sessions_existing"] == 2, st2
    assert _fts_count(phone) == 5, "重匯入不應產生重複"

    # 5. 增量：桌面新增一則訊息 → 再匯出匯入 → 只加新的
    _add_msg(desktop, "S-child", "user", "any flights?", 203.0)
    st3 = hc.import_bundle(phone, hc.export_session(desktop, "S-root"))
    assert st3["messages_added"] == 1 and st3["messages_skipped"] == 4, st3

    # 6. 找不到 session
    assert hc.export_session(desktop, "NOPE") is None

    # 7. memory append-union：不覆蓋、只加新行、原有保留
    local_mem = "- user likes tea\n- user is in Taipei\n"
    incoming = "- user likes tea\n- user prefers dark mode\n"
    merged = hc.merge_memory_text(local_mem, incoming)
    assert "user likes tea" in merged
    assert "user is in Taipei" in merged, "本機既有 memory 不可丟"
    assert "user prefers dark mode" in merged, "新 memory 應併入"
    assert merged.count("user likes tea") == 1, "重複行不應再加"
    # 無新增時原樣回傳
    assert hc.merge_memory_text(local_mem, "- user likes tea\n") == local_mem

    # 8. import_memory：新檔建立、既有檔合併、無新增不動、路徑穿越拒絕
    home = tempfile.mkdtemp()
    mem_dir = os.path.join(home, "memories")
    os.makedirs(mem_dir)
    with open(os.path.join(mem_dir, "USER.md"), "w", encoding="utf-8") as f:
        f.write("- user is in Taipei\n")
    mst = hc.import_memory(home, {
        "USER.md": "- user is in Taipei\n- user prefers dark mode\n",  # 合併
        "NEW.md": "- a brand new note\n",                               # 新檔
        "../escape.md": "- should never be written\n",                 # 路徑穿越 → 拒
        "sub/dir.md": "- subdir not allowed\n",                        # 子目錄 → 拒
        "notmd.txt": "- wrong ext\n",                                  # 非 .md → 拒
    })
    assert mst["mem_added"] == 1 and mst["mem_merged"] == 1, mst
    user_after = open(os.path.join(mem_dir, "USER.md"), encoding="utf-8").read()
    assert "user is in Taipei" in user_after and "user prefers dark mode" in user_after
    assert os.path.isfile(os.path.join(mem_dir, "NEW.md"))
    assert not os.path.exists(os.path.join(home, "escape.md")), "路徑穿越不可寫出 memories/"
    assert not os.path.exists(os.path.join(mem_dir, "sub")), "子目錄不可建立"
    assert not os.path.exists(os.path.join(mem_dir, "notmd.txt"))
    # 冪等：同 memory 重匯入 → 全 unchanged
    mst2 = hc.import_memory(home, {"USER.md": user_after, "NEW.md": "- a brand new note\n"})
    assert mst2["mem_unchanged"] == 2 and mst2["mem_merged"] == 0 and mst2["mem_added"] == 0, mst2

    # 9. import_all：state.db + memories 一起，回傳合併統計
    home2 = tempfile.mkdtemp()
    _mkdb(os.path.join(home2, "state.db"))
    bundle_mem = hc.export_session(desktop, "S-root")
    bundle_mem["memory"] = {"NOTES.md": "- shared note\n"}
    allst = hc.import_all(home2, bundle_mem)
    assert allst["sessions_added"] >= 1 and allst["mem_added"] == 1, allst
    assert os.path.isfile(os.path.join(home2, "memories", "NOTES.md"))

    print("✅ 全部通過（9 組）：export/parent鏈/去重/不覆蓋/FTS/冪等/增量/memory-union/"
          "import_memory+路徑穿越防護/import_all")
    return 0


if __name__ == "__main__":
    sys.exit(run())
