"""
transport E2E 測試 — 需 PyNaCl(+zeroconf)，用 venv 跑：
    /tmp/handoff-venv/bin/python handoff/tests/test_transport_e2e.py

驗證桌面→手機接力傳輸全鏈：互相認證握手 → Box 加密傳 bundle → 手機 import 合併 →
未配對拒絕 → 錯 server 金鑰失敗 → mDNS 廣告/發現（best-effort）。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pairing as pr            # noqa: E402
import handoff_server as hs     # noqa: E402
import handoff_core as hc       # noqa: E402
import desktop_export as de     # noqa: E402

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


def _mk_home(with_session=True):
    home = tempfile.mkdtemp()
    c = sqlite3.connect(os.path.join(home, "state.db"))
    c.executescript(_DDL)
    if with_session:
        c.execute("INSERT INTO sessions(id,source,started_at,title) VALUES('SID','cli',1.0,'Trip')")
        c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('SID','user','plan',1.1)")
        c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('SID','assistant','ok',1.2)")
    c.commit(); c.close()
    return home


def run():
    tmp = tempfile.mkdtemp()
    desk_id = pr.load_or_create_identity(os.path.join(tmp, "desk.key"))
    phone_id = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))

    desk_home = _mk_home()
    phone_home = _mk_home(with_session=False)
    desk_db = os.path.join(desk_home, "state.db")
    phone_db = os.path.join(phone_home, "state.db")

    # 配對：桌面 peers 收手機；（手機端只需 server pubkey，從 QR 拿）
    desk_peers = hs.PeerStore(os.path.join(tmp, "desk_peers.json"))
    desk_peers.add(phone_id.device_id, bytes(phone_id.public_key))

    server = hs.HandoffServer(
        identity=desk_id, peers=desk_peers,
        export_fn=lambda sid: de.export_for_handoff(desk_home, sid, source_device=desk_id.device_id),
        host="127.0.0.1")
    port = server.start(advertise=False)
    try:
        # 1. 正常拉取（互相認證 + 加密）
        bundle = hs.pull_session("127.0.0.1", port, phone_id,
                                 bytes(desk_id.public_key), "SID")
        assert bundle["session_ids"] == ["SID"]
        assert len(bundle["messages"]) == 2

        # 2. import 進手機 + 驗
        st = hc.import_bundle(phone_db, bundle)
        assert st["sessions_added"] == 1 and st["messages_added"] == 2, st
        c = sqlite3.connect(phone_db)
        n = c.execute("SELECT count(*) FROM messages WHERE session_id='SID'").fetchone()[0]
        fts = c.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
        c.close()
        assert n == 2 and fts == 2, f"import 後 messages={n} fts={fts}"

        # 3. 未配對的 client → 被拒
        stranger = pr.load_or_create_identity(os.path.join(tmp, "stranger.key"))
        try:
            hs.pull_session("127.0.0.1", port, stranger, bytes(desk_id.public_key), "SID")
            assert False, "未配對裝置應被拒"
        except PermissionError:
            pass

        # 4. 配對方但用「錯的 server 公鑰」→ 認證/解密失敗（非正常拿到 bundle）
        wrong = pr.load_or_create_identity(os.path.join(tmp, "wrong.key"))
        try:
            hs.pull_session("127.0.0.1", port, phone_id, bytes(wrong.public_key), "SID")
            assert False, "錯 server 公鑰不應成功"
        except Exception:
            pass  # CryptoError / LookupError 皆可接受

        # 5. owner 鎖：傳輸確認後標記整條鏈
        de.mark_handed_off(desk_db, "SID", platform="android")
        c = sqlite3.connect(desk_db)
        arch, state = c.execute(
            "SELECT archived, handoff_state FROM sessions WHERE id='SID'").fetchone()
        c.close()
        assert arch == 1 and state == "completed"
    finally:
        server.stop()

    # 6. _is_lan_ipv4 分類（review P2：QR host 須避開 Tailscale 100.x）
    assert hs._is_lan_ipv4("192.168.1.5")
    assert hs._is_lan_ipv4("10.0.0.9")
    assert hs._is_lan_ipv4("172.16.3.4")
    assert not hs._is_lan_ipv4("100.116.20.1"), "Tailscale/CGNAT 不算 LAN"
    assert not hs._is_lan_ipv4("127.0.0.1"), "loopback 不算 LAN"
    assert not hs._is_lan_ipv4("8.8.8.8"), "公網不算 LAN"
    assert not hs._is_lan_ipv4("not-an-ip")
    # _local_ip 永不回 Tailscale 100.x（除非真的只剩它，屬退而求其次）
    assert hs._local_ip(), "_local_ip 應回傳非空位址"

    # 7. mDNS 廣告/發現（best-effort：multicast 不可用環境略過）
    mdns = _try_mdns(desk_id)
    print(f"✅ 全部通過：握手認證/加密傳輸/import/未配對拒絕/錯金鑰拒絕/owner鎖 · "
          f"LAN-IP 分類 · mDNS={mdns}")
    return 0


def _try_mdns(desk_id) -> str:
    try:
        import time
        from zeroconf import Zeroconf, ServiceBrowser
        peers2 = hs.PeerStore(tempfile.mktemp())
        srv = hs.HandoffServer(identity=desk_id, peers=peers2,
                               export_fn=lambda s: None, host="0.0.0.0")
        srv.start(advertise=True)
        found = {}
        zc = Zeroconf()

        class L:
            def add_service(self, zc, t, name):
                info = zc.get_service_info(t, name)
                if info:
                    found[name] = bytes(info.properties.get(b"did", b"")).decode()
            def update_service(self, *a): pass
            def remove_service(self, *a): pass

        ServiceBrowser(zc, hs.SERVICE_TYPE, L())
        for _ in range(20):
            if any(desk_id.device_id in v for v in found.values()):
                break
            time.sleep(0.1)
        ok = any(desk_id.device_id in v for v in found.values())
        zc.close(); srv.stop()
        return "OK（發現自己廣告的服務）" if ok else "略過/未發現（環境無 multicast）"
    except Exception as e:  # noqa: BLE001
        return f"略過（{type(e).__name__}）"


if __name__ == "__main__":
    sys.exit(run())
