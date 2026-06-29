"""
plugin serve 測試 — 需 PyNaCl+zeroconf，用 venv 跑：
    /tmp/handoff-venv/bin/python handoff/tests/test_plugin_serve.py

驗證 plugin 入口 serve() 正確接線（identity/peers/export_fn 綁 ~/.hermes）+ pairing_qr()，
並用 client 端到端拉取一則對話。
"""
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import handoff            # package（其 __init__ 會把 handoff/ 加入 sys.path）  # noqa: E402
import handoff_server as hs  # noqa: E402
import handoff_core as hc    # noqa: E402
import pairing as pr         # noqa: E402

_DDL = """
CREATE TABLE schema_version (version INTEGER);
INSERT INTO schema_version(version) VALUES (15);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL,
    title TEXT, archived INTEGER NOT NULL DEFAULT 0,
    handoff_state TEXT, handoff_platform TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, timestamp REAL NOT NULL
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content,''));
END;
"""


def run():
    home = tempfile.mkdtemp()
    c = sqlite3.connect(os.path.join(home, "state.db"))
    c.executescript(_DDL)
    c.execute("INSERT INTO sessions(id,source,started_at,title) VALUES('SID','cli',1.0,'T')")
    c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('SID','user','hi',1.1)")
    c.commit(); c.close()
    # memory
    md = os.path.join(home, "memories"); os.makedirs(md)
    open(os.path.join(md, "MEMORY.md"), "w").write("- m1\n")

    # plugin serve()（advertise=False 求測試確定性）
    srv = handoff.serve(home, advertise=False)
    try:
        # serve 應在 ~/.hermes/handoff/ 建好身分 + peers
        assert os.path.isfile(os.path.join(home, "handoff", "id.key"))
        # pairing_qr 可解析、device_id 與 server 一致
        peer = pr.parse_pair_qr(handoff.pairing_qr(srv))
        assert peer.device_id == srv.identity.device_id
        assert peer.public_key == bytes(srv.identity.public_key)

        # 配對一台手機 + 端到端拉取
        phone = pr.load_or_create_identity(os.path.join(home, "phone.key"))
        srv.peers.add(phone.device_id, bytes(phone.public_key))
        bundle = hs.pull_session("127.0.0.1", srv.port, phone,
                                 bytes(srv.identity.public_key), "SID")
        assert bundle["session_ids"] == ["SID"]
        assert bundle["memory"] == {"MEMORY.md": "- m1\n"}, "serve 應帶 memory"
        # import 驗
        phone_db = os.path.join(home, "phone_state.db")
        pc = sqlite3.connect(phone_db); pc.executescript(_DDL)
        pc.execute("DELETE FROM sessions"); pc.execute("DELETE FROM messages")
        pc.commit(); pc.close()
        st = hc.import_bundle(phone_db, bundle)
        assert st["sessions_added"] == 1 and st["messages_added"] == 1, st
    finally:
        srv.stop()

    print("✅ 全部通過：serve 接線(identity/peers/export_fn+memory) · pairing_qr · 端到端拉取+import")
    return 0


if __name__ == "__main__":
    sys.exit(run())
