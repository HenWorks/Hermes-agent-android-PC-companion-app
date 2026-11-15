"""
mesh_broker multi-turn follow-ups — runs on pure python3 (no pytest dependency):
    python3 handoff/tests/test_mesh_threads.py

Verifies:
- a task with a thread id runs as `hermes chat -Q --query-file - -c <title> --create-if-missing`,
  the prompt goes on stdin (not argv), and the second turn of the same thread continues the first
- the push reply says whether the task really is part of a conversation (`thread: true/false`)
- a computer whose hermes lacks `--create-if-missing` (< 0.22.0) falls back to a standalone task
- malformed thread ids are ignored; tasks without a thread keep the one-shot behaviour
- an old queue.db (no `thread` column) is migrated in place
"""
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairing as pr          # noqa: E402
import handoff_server as hs   # noqa: E402
import mesh_broker as mb      # noqa: E402
from test_mesh_broker import _client_op, _poll_until_result  # noqa: E402

FAKE_HERMES = textwrap.dedent('''
    import os, sys
    args = sys.argv[1:]
    state = os.environ["FAKE_STATE"]
    if args[:2] == ["chat", "--help"]:
        print("usage: hermes chat ... " + ("--create-if-missing" if os.environ.get("FAKE_SUPPORTS") == "1" else ""))
        sys.exit(0)
    if args and args[0] == "chat":
        title = args[args.index("-c") + 1]
        assert "--create-if-missing" in args and args[args.index("--query-file") + 1] == "-"
        prompt = sys.stdin.read()
        path = os.path.join(state, title)
        turns = (open(path).read().splitlines() if os.path.exists(path) else []) + [prompt]
        open(path, "w").write("\\n".join(turns))
        print(f"TURN {len(turns)} of {title}: {prompt}")
        sys.exit(0)
    if args and args[0] == "-z":
        print("ONESHOT:" + args[1])
        sys.exit(0)
    sys.exit(2)
''')


def _broker(tmp, phone, supports: bool):
    script = os.path.join(tmp, "fake_hermes.py")
    open(script, "w").write(FAKE_HERMES)
    os.makedirs(os.path.join(tmp, "state"), exist_ok=True)
    os.environ["FAKE_STATE"] = os.path.join(tmp, "state")
    os.environ["FAKE_SUPPORTS"] = "1" if supports else "0"
    identity = pr.load_or_create_identity(os.path.join(tmp, "broker.key"))
    peers = hs.PeerStore(os.path.join(tmp, "peers.json"))
    peers.add(phone.device_id, bytes(phone.public_key))
    store = mb.MeshStore(os.path.join(tmp, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           hermes_cmd=[sys.executable, script, "-z"], host="127.0.0.1")
    broker.start(advertise=False)
    return broker, bytes(identity.public_key)


def _push_and_wait(broker, phone, bpk, prompt, thread=None):
    task = {"prompt": prompt}
    if thread is not None:
        task["thread"] = thread
    r = _client_op(broker.host, broker.port, phone, bpk, {"op": "push", "task": task}, encrypted_reply=False)
    assert r.get("ok"), r
    results = _poll_until_result(broker.host, broker.port, phone, bpk)
    assert len(results) == 1, results
    res = results[0]
    _client_op(broker.host, broker.port, phone, bpk, {"op": "ack", "ids": [res["id"]]}, encrypted_reply=False)
    assert res["ref"] == r["id"], (res, r)
    return r, res


def test_follow_up_continues_the_same_conversation():
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bpk = _broker(tmp, phone, supports=True)
        try:
            title = mb.thread_title(phone.device_id, "t-1")
            r1, res1 = _push_and_wait(broker, phone, bpk, "first $(rm -rf ~) question", thread="t-1")
            assert r1["thread"] is True, r1
            assert res1["text"] == f"TURN 1 of {title}: {mb.MESH_TASK_MARKER}first $(rm -rf ~) question", res1
            r2, res2 = _push_and_wait(broker, phone, bpk, "and then?", thread="t-1")
            assert r2["thread"] is True and res2["text"].startswith(f"TURN 2 of {title}:"), res2
            # another thread is another conversation
            _, res3 = _push_and_wait(broker, phone, bpk, "unrelated", thread="t-2")
            assert res3["text"].startswith("TURN 1 of " + mb.thread_title(phone.device_id, "t-2")), res3
            print("✓ test_follow_up_continues_the_same_conversation (stdin prompt, turn 2 sees turn 1, threads isolated)")
        finally:
            broker.stop()


def test_older_hermes_falls_back_to_a_standalone_task():
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bpk = _broker(tmp, phone, supports=False)
        try:
            r, res = _push_and_wait(broker, phone, bpk, "hello", thread="t-1")
            assert r["thread"] is False, r
            assert res["text"] == f"ONESHOT:{mb.MESH_TASK_MARKER}hello", res
            print("✓ test_older_hermes_falls_back_to_a_standalone_task (no --create-if-missing ⇒ thread:false, -z)")
        finally:
            broker.stop()


def test_bad_or_missing_thread_ids_run_one_shot():
    with tempfile.TemporaryDirectory() as tmp:
        phone = pr.load_or_create_identity(os.path.join(tmp, "phone.key"))
        broker, bpk = _broker(tmp, phone, supports=True)
        try:
            for bad in ["../etc", "a b", "x" * 65, 42, ""]:
                r, res = _push_and_wait(broker, phone, bpk, "p", thread=bad)
                assert r["thread"] is False and res["text"].startswith("ONESHOT:"), (bad, r, res)
            r, res = _push_and_wait(broker, phone, bpk, "p")
            assert r["thread"] is False and res["text"].startswith("ONESHOT:"), (r, res)
            print("✓ test_bad_or_missing_thread_ids_run_one_shot")
        finally:
            broker.stop()


def test_an_old_queue_db_gains_the_thread_column():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "queue.db")
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, from_did TEXT NOT NULL, prompt TEXT NOT NULL,"
                   "status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL)")
        db.execute("INSERT INTO tasks VALUES('old', 'd', 'p', 'pending', ?)", (time.time(),))
        db.commit(); db.close()
        store = mb.MeshStore(path)
        try:
            assert store.claim_next_task() == {"id": "old", "from_did": "d", "prompt": "p", "thread": None}
            store.add_task("new", "d", "q", "t-9")
            assert store.claim_next_task()["thread"] == "t-9"
            print("✓ test_an_old_queue_db_gains_the_thread_column")
        finally:
            store.close()


if __name__ == "__main__":
    test_follow_up_continues_the_same_conversation()
    test_older_hermes_falls_back_to_a_standalone_task()
    test_bad_or_missing_thread_ids_run_one_shot()
    test_an_old_queue_db_gains_the_thread_column()
    print("\nall mesh thread tests passed ✅")
