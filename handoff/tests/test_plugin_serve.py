"""
plugin serve tests — requires PyNaCl+zeroconf, run with a venv:
    /tmp/handoff-venv/bin/python handoff/tests/test_plugin_serve.py

Verifies that the plugin entry point serve() wires things up correctly (identity/peers/export_fn
bound to ~/.hermes) + pairing_qr(), and pulls one conversation end-to-end with the client.
"""
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

import handoff            # package (its __init__ adds handoff/ to sys.path)  # noqa: E402
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

    # plugin serve() (advertise=False for test determinism)
    srv = handoff.serve(home, advertise=False)
    try:
        # serve should create the identity + peers under ~/.hermes/handoff/
        assert os.path.isfile(os.path.join(home, "handoff", "id.key"))
        # pairing_qr is parseable, device_id matches the server
        peer = pr.parse_pair_qr(handoff.pairing_qr(srv))
        assert peer.device_id == srv.identity.device_id
        assert peer.public_key == bytes(srv.identity.public_key)

        # pair a phone + end-to-end pull
        phone = pr.load_or_create_identity(os.path.join(home, "phone.key"))
        srv.peers.add(phone.device_id, bytes(phone.public_key))
        # the server binds _local_ip() (never 0.0.0.0/127.0.0.1) → connect to its actual host
        bundle = hs.pull_session(srv.host, srv.port, phone,
                                 bytes(srv.identity.public_key), "SID")
        assert bundle["session_ids"] == ["SID"]
        assert bundle["memory"] == {"MEMORY.md": "- m1\n"}, "serve should carry memory"
        # verify import
        phone_db = os.path.join(home, "phone_state.db")
        pc = sqlite3.connect(phone_db); pc.executescript(_DDL)
        pc.execute("DELETE FROM sessions"); pc.execute("DELETE FROM messages")
        pc.commit(); pc.close()
        st = hc.import_bundle(phone_db, bundle)
        assert st["sessions_added"] == 1 and st["messages_added"] == 1, st
    finally:
        srv.stop()

    print("✅ all passed: serve wiring(identity/peers/export_fn+memory) · pairing_qr · end-to-end pull+import")
    return 0


if __name__ == "__main__":
    sys.exit(run())
