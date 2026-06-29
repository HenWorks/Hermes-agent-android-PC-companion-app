"""
mesh_broker unit tests — runs on pure python3 (no pytest dependency):
    python3 handoff/tests/test_mesh_broker.py

Verifies:
- pairing trust: unpaired client is rejected
- push → worker runs hermes(stub) oneshot → poll receives encrypted result → cleared after ack
- payload e2e: poll result is encrypted with Box(broker_sk→phone_pk), decryptable on the phone
- task prompt is passed into the subprocess as an "argument" (not via shell, prevents injection)
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

# stub hermes: echoes the prompt back verbatim (worker appends prompt as the last argv)
ECHO_CMD = ["python3", "-c", "import sys;print('ECHO:'+sys.argv[1])"]


def _client_op(host, port, phone, broker_pk, req: dict, encrypted_reply: bool):
    """Simulate the phone: hello → encrypted request → receive reply (decrypt depends on encrypted_reply)."""
    with socket.create_connection((host, port), timeout=5) as c:
        # hello
        hs._send_frame(c, json.dumps({"did": phone.device_id, "pk": phone.public_b64}).encode())
        ack = json.loads(hs._recv_frame(c).decode())
        assert ack.get("ok"), f"handshake rejected: {ack}"
        # encrypted request
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
        broker, bid = _make_broker(tmp, paired_phone=None)  # not paired
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
            # 1. push task
            r = _client_op(broker.host, broker.port, phone, bpk,
                           {"op": "push", "task": {"prompt": "hello-mesh"}}, encrypted_reply=False)
            assert r.get("ok") and r.get("id"), r
            tid = r["id"]

            # 2. wait for the worker to finish (poll until there is a result, up to 10s)
            results = []
            for _ in range(50):
                resp = _client_op(broker.host, broker.port, phone, bpk,
                                  {"op": "poll"}, encrypted_reply=True)
                assert resp.get("ok"), resp
                results = resp.get("results", [])
                if results:
                    break
                time.sleep(0.2)
            assert results, "worker produced no result (timeout)"
            res = results[0]
            assert res["ref"] == tid, res
            assert res["ok"] is True, res
            # stub hermes echoes the prompt verbatim; worker prepends an identifiable marker (desktop recognizes the source)
            assert res["text"] == f"ECHO:{mb.MESH_TASK_MARKER}hello-mesh", res
            print("✓ test_push_run_poll_ack (push→worker→poll encrypted round-trip)")

            # 3. after ack, poll should be cleared
            _client_op(broker.host, broker.port, phone, bpk,
                       {"op": "ack", "ids": [res["id"]]}, encrypted_reply=False)
            resp = _client_op(broker.host, broker.port, phone, bpk,
                              {"op": "poll"}, encrypted_reply=True)
            assert resp.get("results") == [], resp
            print("✓ poll cleared after ack")
        finally:
            broker.stop()


def test_prompt_not_shell_injected():
    """A prompt containing shell metacharacters is not executed (passed as an argument, not shell-concatenated)."""
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
            assert results, "timeout"
            # echo stub echoes the entire prompt verbatim; "PWNED" only appears inside the echo text, not executed by a shell
            assert results[0]["text"] == f"ECHO:{mb.MESH_TASK_MARKER}{danger}", results[0]
            print("✓ test_prompt_not_shell_injected (prompt passed as an argument, not via shell)")
        finally:
            broker.stop()


def test_pair_then_push():
    """Within the pairing window: unpaired phone pairs → added to trust → push works."""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=True)  # open pairing window, not pre-paired
        bpk = bytes(bid.public_key)
        try:
            # pair (not paired + within window → should succeed)
            r = _client_op(broker.host, broker.port, phone, bpk, {"op": "pair"}, encrypted_reply=False)
            assert r.get("ok") and r.get("did") == bid.device_id, r
            assert broker.peers.is_paired(phone.device_id, bytes(phone.public_key)), "not stored in trust after pair"
            # push works after pairing
            r2 = _client_op(broker.host, broker.port, phone, bpk,
                            {"op": "push", "task": {"prompt": "after-pair"}}, encrypted_reply=False)
            assert r2.get("ok"), r2
            results = _poll_until_result(broker.host, broker.port, phone, bpk)
            assert results and results[0]["text"] == f"ECHO:{mb.MESH_TASK_MARKER}after-pair", results
            print("✓ test_pair_then_push (pair within window → push round-trip)")
        finally:
            broker.stop()


def test_pair_rejected_outside_window():
    """Pairing window not open (or already closed): unpaired phone is rejected on connect, and pair cannot succeed."""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=False)  # do not open window
        try:
            with socket.create_connection((broker.host, broker.port), timeout=5) as c:
                hs._send_frame(c, json.dumps(
                    {"did": phone.device_id, "pk": phone.public_b64}).encode())
                ack = json.loads(hs._recv_frame(c).decode())
            assert ack.get("ok") is False and "not paired" in ack.get("err", ""), ack
            assert not broker.peers.is_paired(phone.device_id, bytes(phone.public_key))
            print("✓ test_pair_rejected_outside_window (unpaired connection outside the window is rejected)")
        finally:
            broker.stop()


def test_ack_bound_to_owner():
    """ack is bound to the authenticated identity: phone B cannot delete phone A's result."""
    with tempfile.TemporaryDirectory() as tmp:
        a = pr.load_or_create_identity(os.path.join(tmp, "a.key"))
        b = pr.load_or_create_identity(os.path.join(tmp, "b.key"))
        broker, bid = _make_broker(tmp, paired_phone=a)
        broker.peers.add(b.device_id, bytes(b.public_key))  # B is also paired
        bpk = bytes(bid.public_key)
        try:
            # A push → produces a result belonging to A
            _client_op(broker.host, broker.port, a, bpk,
                       {"op": "push", "task": {"prompt": "for-a"}}, encrypted_reply=False)
            a_results = _poll_until_result(broker.host, broker.port, a, bpk)
            assert a_results, "A has no result"
            a_res_id = a_results[0]["id"]

            # B tries to ack A's result id → must not delete A's result
            _client_op(broker.host, broker.port, b, bpk,
                       {"op": "ack", "ids": [a_res_id]}, encrypted_reply=False)
            still = _client_op(broker.host, broker.port, a, bpk, {"op": "poll"}, encrypted_reply=True)
            assert any(x["id"] == a_res_id for x in still["results"]), "B actually deleted A's result!"
            print("✓ test_ack_bound_to_owner (B cannot delete A's result)")
        finally:
            broker.stop()


def test_requeue_running_on_restart():
    """Startup requeue: tasks stuck in running are restored to pending."""
    with tempfile.TemporaryDirectory() as tmp:
        store = mb.MeshStore(os.path.join(tmp, "q.db"))
        store.add_task("t1", "didX", "p")
        store.claim_next_task()  # → running
        n = store.requeue_running()
        assert n == 1, n
        again = store.claim_next_task()  # should be claimable again
        assert again and again["id"] == "t1", again
        print("✓ test_requeue_running_on_restart")


def _pull(host, port, phone, bpk, session_id):
    """Handoff client flow: hello → Box({op:pull,session_id}) → receive {ok} → (only on success) receive Box(bundle).
    Returns (ack_dict, bundle_or_None). Matches broker._op_pull's two-frame response protocol."""
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
    """Handoff op (unified server): paired phone pulls a session → receives {ok} + Box(bundle) decrypts to bundle;
    a session not found honestly returns {ok:false}. Collaboration and handoff share the same connection protocol + same trust domain."""
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
            print("✓ test_pull_session_handoff (handoff pull round-trip + honest report when session not found)")
        finally:
            mb.de.export_for_handoff = orig
            broker.stop()


def test_pull_requires_pairing():
    """Handoff does not bypass trust: pairing window open but not paired → pull still rejected (op != pair requires being paired)."""
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bid = _make_broker(tmp, paired_phone=None, pairing=True)  # window open, but not paired
        bpk = bytes(bid.public_key)
        try:
            ack, bundle = _pull(broker.host, broker.port, phone, bpk, "s1")
            assert ack.get("ok") is False and "not paired" in ack.get("err", ""), ack
            assert bundle is None
            print("✓ test_pull_requires_pairing (cannot hand off unpaired, even with pairing window open)")
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
    print("\nall mesh_broker tests passed ✅")
