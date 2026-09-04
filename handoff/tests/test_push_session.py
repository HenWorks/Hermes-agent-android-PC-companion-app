"""
`push_session`（手機 → PC 反向同步）的測試。

## 為什麼這一檔到現在才出現

公開 companion repo 的 issue #3：反向同步在 PC 端掛在
`CryptoError: An error occurred trying to decrypt the message`，而**同一次連線**的
`op=pair` / `op=pull` 都正常。使用者把 Android app 完全解除安裝重裝才恢復。

查下去發現 `push_session` **一條測試都沒有**（`grep -rn push_session handoff/tests/` 零命中），
而 `pull` 有。這是它能潛伏的原因之一。

## 兩個必須釘住的東西

**① 大小不是原因。** 回報者失敗的 frame 是 1985300 byte、成功的是 1588186 byte，
很容易讓人去猜「超過某個界線」。**那個猜測是錯的**：

    2^20 = 1048576  <  1588186（成功）  <  1985300（失敗）  <  2^21 = 2097152

兩個數字在同一個 2 的冪次區間，任何 frame 界線都切不開它們；而且兩端的上限都是
64 MB（`handoff_server._recv_frame` 的 `max_len` / Kotlin 的 `MAX_FRAME`）。
`test_push_session_handles_a_two_megabyte_bundle` 把這件事永久釘死，
下次再有人往「大小」猜的時候有現成的反證。

**② 真正的原因是金鑰對不上。** 手機的 `syncBack()` 用 `peers.all().firstOrNull()`
取 broker 公鑰（**最舊的那一筆**），而 `pair` / `pull` 用剛掃到的 QR peer。
桌面換過身分（例如同一台電腦的 handoff plugin 與 mesh broker 是兩個 id.key）之後，
push_session 就用舊公鑰加密，broker 用現行 desk_sk 解 → CryptoError。

`test_a_bundle_encrypted_to_a_stale_key_is_reported_clearly` 釘住的是**使用者體感**：
這種情況必須回一句看得懂的話，不是一個裸的密碼學例外。
"""
import json
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handoff_server as hs  # noqa: E402
import mesh_broker as mb  # noqa: E402
import pairing as pr  # noqa: E402

ECHO_CMD = [sys.executable, "-c", "import sys; print('echo:', sys.argv[1])"]


def _make_broker(tmp, paired_phone=None):
    identity = pr.load_or_create_identity(os.path.join(tmp, "broker.key"))
    peers = hs.PeerStore(os.path.join(tmp, "peers.json"))
    if paired_phone is not None:
        peers.add(paired_phone.device_id, bytes(paired_phone.public_key))
    store = mb.MeshStore(os.path.join(tmp, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           hermes_cmd=ECHO_CMD, home=tmp, host="127.0.0.1")
    broker.start(advertise=False)
    return broker, identity


def _push(host, port, phone, broker_pk, bundle, encrypt_to=None):
    """模擬手機送 push_session。`encrypt_to` 可指定**另一把**公鑰來重現金鑰對不上。"""
    target = encrypt_to if encrypt_to is not None else broker_pk
    with socket.create_connection((host, port), timeout=10) as c:
        hs._send_frame(c, json.dumps({"did": phone.device_id, "pk": phone.public_b64}).encode())
        ack = json.loads(hs._recv_frame(c).decode())
        assert ack.get("ok"), f"handshake rejected: {ack}"
        req = json.dumps({"op": "push_session", "bundle": bundle}).encode()
        hs._send_frame(c, pr.box_encrypt(phone.private_key, target, req))
        return json.loads(hs._recv_frame(c).decode()), ack


def test_push_session_round_trip():
    """基本往返：已配對手機上傳 bundle → broker 合併 → 回 {ok, stats}。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        try:
            bundle = {"schema": 1, "source_device": phone.device_id,
                      "session_ids": ["s1"], "sessions": [], "messages": [], "memory": {}}
            captured = {}

            def fake_import(home, b):
                captured["b"] = b
                return {"sessions": 1}

            orig = mb.hc.import_all
            mb.hc.import_all = fake_import
            try:
                reply, _ = _push(broker.host, broker.port, phone, bytes(bid.public_key), bundle)
            finally:
                mb.hc.import_all = orig
            assert reply.get("ok") is True, reply
            assert reply.get("stats") == {"sessions": 1}, reply
            assert captured["b"] == bundle, "broker 收到的 bundle 與送出的不同"
            print("✓ test_push_session_round_trip")
        finally:
            broker.stop()


def test_push_session_rejects_a_missing_bundle():
    """沒帶 bundle 要誠實回報，不是丟例外。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        try:
            reply, _ = _push(broker.host, broker.port, phone, bytes(bid.public_key), None)
            assert reply.get("ok") is False and "bundle" in reply.get("err", ""), reply
            print("✓ test_push_session_rejects_a_missing_bundle")
        finally:
            broker.stop()


def test_push_session_handles_a_two_megabyte_bundle():
    """
    🚨 **大小不是 issue #3 的原因**，這條把它永久釘死。

    回報的失敗長度 1985300 與成功長度 1588186 都落在 2^20~2^21 之間，
    任何 frame 界線都切不開；兩端上限都是 64 MB。這裡直接送一個比失敗值**更大**的
    bundle，證明這條路徑對這個量級毫無問題。
    """
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        try:
            # 明文 > 2 MiB，比回報的 1985300 還大
            big = {"schema": 1, "session_ids": ["s1"], "sessions": [], "memory": {},
                   "messages": [{"role": "user", "content": "x" * 1024} for _ in range(2200)]}
            raw = json.dumps({"op": "push_session", "bundle": big}).encode()
            assert len(raw) > 2 * 1024 * 1024, f"fixture 只有 {len(raw)} byte，測不到量級"
            assert len(raw) > 1985300, "fixture 必須大過回報的失敗長度才有意義"

            seen = {}

            def fake_import(home, b):
                seen["n"] = len(b["messages"])
                return {"ok": 1}

            orig = mb.hc.import_all
            mb.hc.import_all = fake_import
            try:
                reply, _ = _push(broker.host, broker.port, phone, bytes(bid.public_key), big)
            finally:
                mb.hc.import_all = orig
            assert reply.get("ok") is True, f"2 MB bundle 失敗了：{reply}"
            assert seen["n"] == 2200, "bundle 在傳輸中被截斷"
            print(f"✓ test_push_session_handles_a_two_megabyte_bundle ({len(raw)} byte)")
        finally:
            broker.stop()


def test_a_bundle_encrypted_to_a_stale_key_is_reported_clearly():
    """
    🚨 issue #3 的真正失敗路徑：手機用**舊的** broker 公鑰加密。

    重現方式與現實一致——手機仍持有舊桌面身分的公鑰（`peers.all().firstOrNull()`），
    而 broker 已經是新身分。握手會過（broker 檢查的是**手機**的公鑰），
    解密才失敗。

    這條釘住的是**使用者體感**：broker 必須回一句看得懂、可行動的話。
    裸的 `CryptoError: An error occurred trying to decrypt the message` 對使用者
    毫無資訊，他唯一想得到的辦法就是把整個 App 重裝——回報者正是這樣做的。
    """
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        stale = pr.load_or_create_identity(os.path.join(tmp, "old_desktop.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        try:
            reply, _ = _push(broker.host, broker.port, phone, bytes(bid.public_key),
                             {"schema": 1}, encrypt_to=bytes(stale.public_key))
            assert reply.get("ok") is False, f"用錯金鑰竟然成功了：{reply}"
            err = reply.get("err", "")
            assert "CryptoError" not in err and "decrypt the message" not in err, (
                f"回給使用者的是裸的密碼學例外：{err!r}\n"
                "使用者看到這個只能猜，實際上他需要的動作是「重新掃描配對碼」。"
            )
            assert "identity" in err.lower() or "re-pair" in err.lower(), (
                f"錯誤訊息沒有指出該做什麼：{err!r}"
            )
            print("✓ test_a_bundle_encrypted_to_a_stale_key_is_reported_clearly")
        finally:
            broker.stop()


if __name__ == "__main__":
    test_push_session_round_trip()
    test_push_session_rejects_a_missing_bundle()
    test_push_session_handles_a_two_megabyte_bundle()
    test_a_bundle_encrypted_to_a_stale_key_is_reported_clearly()
    print("all push_session tests passed")
