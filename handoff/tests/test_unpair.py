"""
撤銷已配對裝置（`PeerStore.remove` + 主控台的 `/api/unpair`）。

## 為什麼需要這個功能

**信任原本只能增不能減。** `PeerStore` 只有 `add / pubkey / is_paired`——一支手機的公鑰
進了 `~/.hermes/mesh/peers.json`，就永遠被接受，沒有任何 GUI 或 CLI 撤銷得了它；
使用者唯一的辦法是自己去手改那個 JSON 檔。

觸發點是一個已出貨的問題：手機的備份匯出檔在 2026-09-04 之前**含有手機的 NaCl 私鑰**
（`~/.hermes/mesh/id.key`）。那個 zip 的設計用途就是「丟雲端硬碟、用 IM 傳給自己」，
拿到它的人只要在同一個區網／tailnet 內，就能冒充那支手機對這台電腦派工——
而派工內容會以 `hermes -z <prompt>` 在這裡執行。

🚨 **手機端重新產生身分救不了這件事。** 信任清單在**電腦**這一端，電腦的 peers.json
存的是手機的公鑰；手機換了新身分，電腦照樣接受舊的那把。撤銷必須發生在這裡。

同一個「只能增不能減」的形狀在手機端也有一份（`HandoffPeerStore` 同樣沒有 remove），
那是另一條要修的線。

## CSRF

`/api/unpair` 是這台伺服器**第一個會改變狀態**的路由。它綁 127.0.0.1，但那擋不住
使用者自己瀏覽器裡的惡意分頁——任何網頁都能對 127.0.0.1 發 POST。
防線是要求自訂 header：跨來源帶自訂 header 的 fetch 會先觸發 CORS preflight，
而我們不回應 preflight ⇒ 瀏覽器直接擋下。表單提交（唯一不需要 preflight 的跨來源 POST）
帶不了自訂 header。
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import companion_web as cw  # noqa: E402
import handoff_server as hs  # noqa: E402
import mesh_broker as mb  # noqa: E402
import pairing as pr  # noqa: E402


def _console(tmp, phones=()):
    identity = pr.load_or_create_identity(os.path.join(tmp, "broker.key"))
    peers = hs.PeerStore(os.path.join(tmp, "peers.json"))
    for ph in phones:
        peers.add(ph.device_id, bytes(ph.public_key))
    store = mb.MeshStore(os.path.join(tmp, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           home=tmp, host="127.0.0.1", port=8765)
    host, port = cw.serve_web(broker, "127.0.0.1", 0)
    time.sleep(0.3)
    return broker, f"http://{host}:{port}"


def _post(url, body, headers=None):
    req = urllib.request.Request(url + "/api/unpair", method="POST",
                                 data=json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_peer_store_can_forget_a_device():
    """純函式層：remove 要真的落盤，而且要誠實回報「它本來在不在」。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "peers.json")
        ph = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        store = hs.PeerStore(path)
        store.add(ph.device_id, bytes(ph.public_key))
        assert store.is_paired(ph.device_id, bytes(ph.public_key))

        assert store.remove(ph.device_id) is True
        assert not store.is_paired(ph.device_id, bytes(ph.public_key)), "撤銷後仍被信任"
        assert store.remove(ph.device_id) is False, "第二次撤銷應回報「本來就不在」"

        # 落盤了嗎 —— 重新開一個 store（模擬重啟 broker）
        assert not hs.PeerStore(path).is_paired(ph.device_id, bytes(ph.public_key)), (
            "撤銷沒有寫進檔案，重啟 broker 之後那支手機又被信任了"
        )
        print("✓ test_peer_store_can_forget_a_device")


def test_console_unpair_removes_the_device():
    """主控台端到端：POST 之後 /api/status 不再列出它，broker 也真的不再信任。"""
    with tempfile.TemporaryDirectory() as tmp:
        a = pr.load_or_create_identity(os.path.join(tmp, "a.key"))
        b = pr.load_or_create_identity(os.path.join(tmp, "b.key"))
        broker, url = _console(tmp, phones=(a, b))
        try:
            st = json.loads(urllib.request.urlopen(url + "/api/status", timeout=5).read())
            assert st["paired_count"] == 2, st
            # did 必須是完整的 —— 截成 8 碼的話按鈕不知道要撤銷誰
            dids = {p["did"] for p in st["paired"]}
            assert a.device_id in dids and b.device_id in dids, st["paired"]
            assert all(len(p["did"]) > 8 for p in st["paired"]), "did 被截斷了"

            code, body = _post(url, {"did": a.device_id}, {"X-Hermes-Console": "1"})
            assert (code, body.get("ok")) == (200, True), (code, body)

            st2 = json.loads(urllib.request.urlopen(url + "/api/status", timeout=5).read())
            assert st2["paired_count"] == 1, st2
            assert {p["did"] for p in st2["paired"]} == {b.device_id}
            assert not broker.peers.is_paired(a.device_id, bytes(a.public_key)), "broker 仍信任它"
            assert broker.peers.is_paired(b.device_id, bytes(b.public_key)), "誤刪了別台"
            print("✓ test_console_unpair_removes_the_device")
        finally:
            broker.stop()


def test_unpair_requires_the_console_header():
    """
    🚨 CSRF：沒有自訂 header 一律拒絕。

    這條擋的是「使用者瀏覽器裡的惡意分頁對 127.0.0.1 發 POST」。
    帶自訂 header 的跨來源 fetch 會先觸發 CORS preflight（我們不回應）⇒ 送不出來；
    而表單提交這種不需要 preflight 的方式**帶不了**自訂 header。
    """
    with tempfile.TemporaryDirectory() as tmp:
        ph = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, url = _console(tmp, phones=(ph,))
        try:
            code, body = _post(url, {"did": ph.device_id})           # 沒有 header
            assert code == 403, (code, body)
            assert broker.peers.is_paired(ph.device_id, bytes(ph.public_key)), (
                "沒有 console header 的請求竟然撤銷成功 —— 任何網頁都能踢掉使用者的手機"
            )
            # 值不對也要擋
            code2, _ = _post(url, {"did": ph.device_id}, {"X-Hermes-Console": "0"})
            assert code2 == 403, code2
            assert broker.peers.is_paired(ph.device_id, bytes(ph.public_key))
            print("✓ test_unpair_requires_the_console_header")
        finally:
            broker.stop()


def test_unpair_of_an_unknown_device_is_honest():
    """撤銷一個不存在的 did 要回 ok:false，不是假裝成功、也不是丟例外。"""
    with tempfile.TemporaryDirectory() as tmp:
        ph = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, url = _console(tmp, phones=(ph,))
        try:
            code, body = _post(url, {"did": "0" * 16}, {"X-Hermes-Console": "1"})
            assert (code, body.get("ok")) == (200, False), (code, body)
            code2, body2 = _post(url, {}, {"X-Hermes-Console": "1"})     # 沒帶 did
            assert (code2, body2.get("ok")) == (200, False), (code2, body2)
            assert broker.peers.is_paired(ph.device_id, bytes(ph.public_key)), "誤刪"
            print("✓ test_unpair_of_an_unknown_device_is_honest")
        finally:
            broker.stop()


if __name__ == "__main__":
    test_peer_store_can_forget_a_device()
    test_console_unpair_removes_the_device()
    test_unpair_requires_the_console_header()
    test_unpair_of_an_unknown_device_is_honest()
    print("all unpair tests passed")
