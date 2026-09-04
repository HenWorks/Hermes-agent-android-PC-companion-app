"""
Broker shutdown ordering — the SQLite handle must be released, but never before the
in-flight task has written its result.

## 這一檔為什麼存在

PR #5 修的是真 bug：`MeshStore` 從不釋放 SQLite 連線，Windows 上開著的 WAL 會鎖住
`queue.db`，暫存目錄清不掉（`PermissionError: [WinError 32]`）。

但它原本的寫法是在 `stop()` **一開頭**就 `store.close()`，那會引入一個更糟的問題。
實測重現過：

    1. worker 領到任務，進入 `_run_hermes`（上限 900 秒）
    2. 使用者退出 companion → `stop()` → DB 被關掉
    3. worker 跑完，呼叫 `add_result` → ProgrammingError: Cannot operate on a closed database
    4. `_worker_loop` **沒有 try/except** ⇒ 未捕捉例外 + **結果永久遺失**

而 `_worker_loop` 的迴圈主體是**跑完整輪**（含 `add_result` / `finish_task`）才回頭檢查
`_running` 的——所以只要等 worker 真的退出再關，結果必然已經落地。

取捨寫清楚：等不到（任務還在跑）就**不關**。Windows 的檔案鎖只是清不掉暫存目錄，
比弄丟使用者的答案輕得多；行程結束時 OS 本來就會釋放 handle。
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import handoff_server as hs  # noqa: E402
import mesh_broker as mb  # noqa: E402
import pairing as pr  # noqa: E402


def _broker(cfg):
    identity = pr.load_or_create_identity(os.path.join(cfg, "identity.json"))
    peers = hs.PeerStore(os.path.join(cfg, "peers.json"))
    store = mb.MeshStore(os.path.join(cfg, "queue.db"))
    return mb.MeshBroker(identity=identity, peers=peers, store=store,
                         home=cfg, host="127.0.0.1", port=0)


def test_store_is_closed_when_idle():
    """閒置時關得掉——這才是 Windows 檔案鎖的實際情境（測試跑完清暫存目錄）。"""
    with tempfile.TemporaryDirectory() as cfg:
        b = _broker(cfg)
        b.start(advertise=False)
        time.sleep(0.2)                     # 讓 worker 進到 sleep(1.0)
        b.stop()
        # 關掉之後任何 DB 操作都該失敗——那正是「handle 真的釋放了」的證據
        try:
            b.store.add_task("x", "did", "p")
            raise AssertionError("store 沒有被關閉 ⇒ Windows 上 queue.db 仍被鎖住")
        except Exception as e:
            assert "closed database" in str(e).lower(), f"預期 closed database，實際 {e!r}"
        print("✓ test_store_is_closed_when_idle")


def test_inflight_task_result_survives_shutdown():
    """
    🚨 這條是 PR #5 原版會紅的那一條。

    模擬「任務跑到一半使用者退出」：worker 卡在一個慢的 hermes 指令裡，這時呼叫
    `stop()`。結果必須**寫得進去**——手機才拿得到答案。
    """
    with tempfile.TemporaryDirectory() as cfg:
        b = _broker(cfg)
        started = threading.Event()
        release = threading.Event()

        def slow_hermes(prompt):
            started.set()
            release.wait(timeout=30)        # 假裝 hermes 跑很久（真實上限 900 秒）
            return True, "答案在這裡"

        b._run_hermes = slow_hermes
        b.start(advertise=False)
        b.store.add_task("t1", "didPhone", "做一件事")
        assert started.wait(timeout=10), "worker 沒有領到任務"

        # 使用者在任務執行中退出。grace 設小一點，測試不必真的等 5 秒。
        b.stop(worker_grace_sec=0.5)

        # worker 這時才跑完並寫結果——DB 必須還開著
        release.set()
        time.sleep(1.0)
        rows = b.store.pending_results("didPhone")
        assert len(rows) == 1, (
            f"在途任務的結果不見了（實際 {len(rows)} 筆）。"
            "stop() 一定是在 worker 收尾前就把 DB 關掉了 —— 手機永遠等不到這則回覆。"
        )
        assert rows[0]["text"] == "答案在這裡"
        print("✓ test_inflight_task_result_survives_shutdown")


def test_store_is_a_context_manager():
    """PR #5 的另一半：測試自己要能關掉 store，否則 Windows 上暫存目錄清不掉。"""
    with tempfile.TemporaryDirectory() as cfg:
        with mb.MeshStore(os.path.join(cfg, "q.db")) as store:
            store.add_task("t1", "did", "p")
            assert store.claim_next_task()["id"] == "t1"
        try:
            store.add_task("t2", "did", "p")
            raise AssertionError("離開 with 之後 store 應該已關閉")
        except Exception as e:
            assert "closed database" in str(e).lower()
        print("✓ test_store_is_a_context_manager")


if __name__ == "__main__":
    test_store_is_closed_when_idle()
    test_inflight_task_result_survives_shutdown()
    test_store_is_a_context_manager()
    print("all shutdown tests passed")
