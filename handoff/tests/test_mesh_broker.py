"""
mesh_broker 單元測試 — 純 python3 可跑（無 pytest 相依）：
    python3 handoff/tests/test_mesh_broker.py

驗證：
- 配對信任：未配對 client 被拒
- push → worker 跑 hermes(stub) oneshot → poll 收到加密結果 → ack 後清空
- payload e2e：poll 結果用 Box(broker_sk→phone_pk) 加密、手機端可解
- 任務 prompt 以「參數」帶入子程序（不經 shell，防注入）
"""
import json
import os
import socket
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pairing as pr          # noqa: E402
import handoff_server as hs   # noqa: E402
import mesh_broker as mb      # noqa: E402

# stub hermes：把 prompt 原樣回 echo（worker 會 append prompt 成最後一個 argv）
ECHO_CMD = ["python3", "-c", "import sys;print('ECHO:'+sys.argv[1])"]


def _client_op(host, port, phone, broker_pk, req: dict, encrypted_reply: bool):
    """模擬手機：hello → 加密請求 → 收回應（依 encrypted_reply 決定是否解密）。"""
    with socket.create_connection((host, port), timeout=5) as c:
        # hello
        hs._send_frame(c, json.dumps({"did": phone.device_id, "pk": phone.public_b64}).encode())
        ack = json.loads(hs._recv_frame(c).decode())
        assert ack.get("ok"), f"握手被拒：{ack}"
        # 加密請求
        hs._send_frame(c, pr.box_encrypt(phone.private_key, broker_pk, json.dumps(req).encode()))
        raw = hs._recv_frame(c)
        if encrypted_reply:
            return json.loads(pr.box_decrypt(phone.private_key, broker_pk, raw))
        return json.loads(raw.decode())


def _make_broker(tmp, paired_phone=None, host="127.0.0.1", pairing=False):
    identity = pr.load_or_create_identity(os.path.join(tmp, "broker.key"))
    peers = hs.PeerStore(os.path.join(tmp, "peers.json"))
    if paired_phone is not None:
        peers.add(paired_phone.device_id, bytes(paired_phone.public_key))
    store = mb.MeshStore(os.path.join(tmp, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           hermes_cmd=ECHO_CMD, host=host)
    broker.start(advertise=False)
    if pairing:
        broker.open_pairing(300)
    return broker, identity


def _poll_until_result(host, port, phone, bpk, tries=50):
    for _ in range(tries):
        resp = _client_op(host, port, phone, bpk, {"op": "poll"}, encrypted_reply=True)
        assert resp.get("ok"), resp
        if resp.get("results"):
            return resp["results"]
        time.sleep(0.2)
    return []


def test_unpaired_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None)  # 不配對
        try:
            with socket.create_connection((broker.host, broker.port), timeout=5) as c:
                hs._send_frame(c, json.dumps(
                    {"did": phone.device_id, "pk": phone.public_b64}).encode())
                ack = json.loads(hs._recv_frame(c).decode())
            assert ack.get("ok") is False and "not paired" in ack.get("err", ""), ack
            print("✓ test_unpaired_rejected")
        finally:
            broker.stop()


def test_push_run_poll_ack():
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        bpk = bytes(bid.public_key)
        try:
            # 1. push 任務
            r = _client_op(broker.host, broker.port, phone, bpk,
                           {"op": "push", "task": {"prompt": "hello-mesh"}}, encrypted_reply=False)
            assert r.get("ok") and r.get("id"), r
            tid = r["id"]

            # 2. 等 worker 跑完（poll 直到有結果，最多 10s）
            results = []
            for _ in range(50):
                resp = _client_op(broker.host, broker.port, phone, bpk,
                                  {"op": "poll"}, encrypted_reply=True)
                assert resp.get("ok"), resp
                results = resp.get("results", [])
                if results:
                    break
                time.sleep(0.2)
            assert results, "worker 未產生結果（逾時）"
            res = results[0]
            assert res["ref"] == tid, res
            assert res["ok"] is True, res
            # stub hermes 把 prompt 原樣回；worker 會在 prompt 前加可辨識前綴（桌面端認來源）
            assert res["text"] == f"ECHO:{mb.MESH_TASK_MARKER}hello-mesh", res
            print("✓ test_push_run_poll_ack（push→worker→poll 加密往返）")

            # 3. ack 後 poll 應清空
            _client_op(broker.host, broker.port, phone, bpk,
                       {"op": "ack", "ids": [res["id"]]}, encrypted_reply=False)
            resp = _client_op(broker.host, broker.port, phone, bpk,
                              {"op": "poll"}, encrypted_reply=True)
            assert resp.get("results") == [], resp
            print("✓ ack 後 poll 清空")
        finally:
            broker.stop()


def test_prompt_not_shell_injected():
    """prompt 含 shell metachar 不會被執行（以參數帶入，非 shell 拼接）。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        bpk = bytes(bid.public_key)
        try:
            danger = "x; echo PWNED"
            _client_op(broker.host, broker.port, phone, bpk,
                       {"op": "push", "task": {"prompt": danger}}, encrypted_reply=False)
            results = []
            for _ in range(50):
                resp = _client_op(broker.host, broker.port, phone, bpk,
                                  {"op": "poll"}, encrypted_reply=True)
                results = resp.get("results", [])
                if results:
                    break
                time.sleep(0.2)
            assert results, "逾時"
            # echo stub 把整個 prompt 原樣回；"PWNED" 只會出現在 echo 文字內、非被 shell 執行
            assert results[0]["text"] == f"ECHO:{mb.MESH_TASK_MARKER}{danger}", results[0]
            print("✓ test_prompt_not_shell_injected（prompt 以參數帶入、未經 shell）")
        finally:
            broker.stop()


def test_pair_then_push():
    """配對視窗內：未配對手機 pair → 加入信任 → push 可用。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=True)  # 開配對視窗、未預先配對
        bpk = bytes(bid.public_key)
        try:
            # pair（未配對 + 視窗內 → 應成功）
            r = _client_op(broker.host, broker.port, phone, bpk, {"op": "pair"}, encrypted_reply=False)
            assert r.get("ok") and r.get("did") == bid.device_id, r
            assert broker.peers.is_paired(phone.device_id, bytes(phone.public_key)), "pair 後未存入信任"
            # 配對後 push 可用
            r2 = _client_op(broker.host, broker.port, phone, bpk,
                            {"op": "push", "task": {"prompt": "after-pair"}}, encrypted_reply=False)
            assert r2.get("ok"), r2
            results = _poll_until_result(broker.host, broker.port, phone, bpk)
            assert results and results[0]["text"] == f"ECHO:{mb.MESH_TASK_MARKER}after-pair", results
            print("✓ test_pair_then_push（視窗內 pair → push 往返）")
        finally:
            broker.stop()


def test_pair_rejected_outside_window():
    """配對視窗未開（或已關）：未配對手機連線即被拒、pair 也不得逞。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=False)  # 不開窗
        try:
            with socket.create_connection((broker.host, broker.port), timeout=5) as c:
                hs._send_frame(c, json.dumps(
                    {"did": phone.device_id, "pk": phone.public_b64}).encode())
                ack = json.loads(hs._recv_frame(c).decode())
            assert ack.get("ok") is False and "not paired" in ack.get("err", ""), ack
            assert not broker.peers.is_paired(phone.device_id, bytes(phone.public_key))
            print("✓ test_pair_rejected_outside_window（窗外未配對連線即拒）")
        finally:
            broker.stop()


def test_ack_bound_to_owner():
    """ack 綁認證身分：手機 B 不能刪手機 A 的結果。"""
    with tempfile.TemporaryDirectory() as tmp:
        a = pr.load_or_create_identity(os.path.join(tmp, "a.key"))
        b = pr.load_or_create_identity(os.path.join(tmp, "b.key"))
        broker, bid = _make_broker(tmp, paired_phone=a)
        broker.peers.add(b.device_id, bytes(b.public_key))  # B 也已配對
        bpk = bytes(bid.public_key)
        try:
            # A push → 產生屬於 A 的結果
            _client_op(broker.host, broker.port, a, bpk,
                       {"op": "push", "task": {"prompt": "for-a"}}, encrypted_reply=False)
            a_results = _poll_until_result(broker.host, broker.port, a, bpk)
            assert a_results, "A 無結果"
            a_res_id = a_results[0]["id"]

            # B 嘗試 ack A 的 result id → 不該刪掉 A 的結果
            _client_op(broker.host, broker.port, b, bpk,
                       {"op": "ack", "ids": [a_res_id]}, encrypted_reply=False)
            still = _client_op(broker.host, broker.port, a, bpk, {"op": "poll"}, encrypted_reply=True)
            assert any(x["id"] == a_res_id for x in still["results"]), "B 竟刪掉了 A 的結果！"
            print("✓ test_ack_bound_to_owner（B 無法刪 A 的結果）")
        finally:
            broker.stop()


def test_requeue_running_on_restart():
    """啟動 requeue：卡 running 的任務還原 pending。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = mb.MeshStore(os.path.join(tmp, "q.db"))
        store.add_task("t1", "didX", "p")
        store.claim_next_task()  # → running
        n = store.requeue_running()
        assert n == 1, n
        again = store.claim_next_task()  # 應能再次取到
        assert again and again["id"] == "t1", again
        print("✓ test_requeue_running_on_restart")


def _pull(host, port, phone, bpk, session_id):
    """接力 client 流程：hello → Box({op:pull,session_id}) → 收 {ok} →（成功才）收 Box(bundle)。
    回 (ack_dict, bundle_or_None)。對齊 broker._op_pull 的雙 frame 回應協定。"""
    with socket.create_connection((host, port), timeout=5) as c:
        hs._send_frame(c, json.dumps({"did": phone.device_id, "pk": phone.public_b64}).encode())
        if not json.loads(hs._recv_frame(c).decode()).get("ok"):
            return {"ok": False, "err": "handshake"}, None
        hs._send_frame(c, pr.box_encrypt(phone.private_key, bpk,
                       json.dumps({"op": "pull", "session_id": session_id}).encode()))
        ack = json.loads(hs._recv_frame(c).decode())
        if not ack.get("ok"):
            return ack, None
        bundle = json.loads(pr.box_decrypt(phone.private_key, bpk, hs._recv_frame(c)))
        return ack, bundle


def test_pull_session_handoff():
    """接力 op（統一 server）：已配對手機 pull session → 收 {ok} + Box(bundle) 解得 bundle；
    找不到的 session 誠實回 {ok:false}。協作與接力共用同一連線協定 + 同一信任域。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=phone)
        bpk = bytes(bid.public_key)
        fake = {"session_ids": ["s1"], "messages": [{"role": "user", "content": "hi"}], "memory": {}}
        orig = mb.de.export_for_handoff
        mb.de.export_for_handoff = lambda home, sid, **kw: fake if sid == "s1" else None
        try:
            ack, bundle = _pull(broker.host, broker.port, phone, bpk, "s1")
            assert ack.get("ok"), ack
            assert bundle == fake, bundle
            ack2, b2 = _pull(broker.host, broker.port, phone, bpk, "nope")
            assert ack2.get("ok") is False and "not found" in ack2.get("err", ""), ack2
            assert b2 is None
            print("✓ test_pull_session_handoff（接力 pull 往返 + 找不到 session 誠實回報）")
        finally:
            mb.de.export_for_handoff = orig
            broker.stop()


def test_pull_requires_pairing():
    """接力不繞過信任：配對視窗開但未配對 → pull 仍被拒（op != pair 時要求已配對）。"""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=True)  # 視窗開、但未配對
        bpk = bytes(bid.public_key)
        try:
            ack, bundle = _pull(broker.host, broker.port, phone, bpk, "s1")
            assert ack.get("ok") is False and "not paired" in ack.get("err", ""), ack
            assert bundle is None
            print("✓ test_pull_requires_pairing（未配對不可接力，即使配對視窗開）")
        finally:
            broker.stop()


if __name__ == "__main__":
    test_unpaired_rejected()
    test_push_run_poll_ack()
    test_prompt_not_shell_injected()
    test_pair_then_push()
    test_pair_rejected_outside_window()
    test_ack_bound_to_owner()
    test_requeue_running_on_restart()
    test_pull_session_handoff()
    test_pull_requires_pairing()
    print("\n所有 mesh_broker 測試通過 ✅")
