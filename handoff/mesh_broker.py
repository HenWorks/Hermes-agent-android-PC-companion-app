"""
mesh_broker — 桌面側 mesh broker + worker（LAN-first，M1+M2）。

定位（見 android/docs/mesh-design.md）：讓手機 app 把任務非同步派給桌面 hermes 執行、收回結果。
M2 單機情境：**broker 就是 worker 節點本身**——手機↔桌面直接 e2e（broker 即收件人，不中繼解密）。
多節點中繼（broker 轉發給其他 worker、不解密）是未來事，本檔不做。

複用 handoff 底座（零重造）：
- pairing.py：DeviceIdentity / load_or_create_identity / box_encrypt|decrypt / build_pair_qr
- handoff_server.py：PeerStore（配對信任）/ _send_frame|_recv_frame（4-byte framing）/ _local_ip

協定（每連線一個 op，沿用 handoff 握手+認證模式）：
  1. client 明文送 {did, pk}（hello）
  2. broker 查 is_paired → {ok, proto} 或 {ok:false, err}
  3. client 送 Box(client_sk→broker_pk)(請求 JSON)，op 之一：
       push  {op:"push", task:{id,prompt,created_at}}  → 入工作佇列；回 {ok, id}
       poll  {op:"poll"}                                → 回 Box(broker_sk→client_pk)({ok, results:[...]})
       ack   {op:"ack", ids:[...]}                      → 刪已收結果；回 {ok}
  worker 執行緒：取 pending task → 跑 `hermes -z <prompt>` oneshot → 結果寫 outbox（to=發起者 did）。

🔴 安全：broker 預設綁 LAN IP（非 0.0.0.0）；只接受已配對 node 公鑰；payload NaCl e2e；
   payload 絕不含憑證（只傳任務 prompt 與結果文字）。私鑰永不離機。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pairing as pr
import handoff_server as hs
import desktop_export as de   # 接力（handoff）：把 session 匯出成可加密傳輸的 bundle
import handoff_core as hc      # 反向同步（#22）：手機上傳 bundle → import_all 冪等合併進 PC

SERVICE_TYPE = "_hermes-mesh._tcp.local."
PROTO = 1
MAX_RESULT_CHARS = 64 * 1024  # 單一結果上限，避免異常長輸出撐爆通知/傳輸
# 手機派工的對話會以此前綴開頭，讓桌面 hermes（sessions list / browse / resume）一眼認出來源。
# 純 prompt 字串，照常呼叫 `hermes -z`——不碰上游套件、不改 state.db schema，上游更新不受影響。
MESH_TASK_MARKER = "📱 [手機派工] "
# 固定預設 port（⚠️ 必須固定，不可隨機）：手機 app 配對後把 host:port 存進 peer，之後一直用它連。
# 若 broker 每次重啟換 port，手機就連不到（顯示離線、派工失敗）。51379 為避開常用服務的高位埠。
DEFAULT_PORT = 51379


# ── 佇列儲存（SQLite，個人規模足夠）─────────────────────────────────────────

class MeshStore:
    """tasks（待跑）+ results（待手機收）。單檔 SQLite，broker 與 worker 共用。"""

    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        # check_same_thread=False：broker 連線執行緒與 worker 執行緒共用；用 _lock 串行化
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS tasks("
                "id TEXT PRIMARY KEY, from_did TEXT NOT NULL, prompt TEXT NOT NULL,"
                "status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS results("
                "id TEXT PRIMARY KEY, ref TEXT NOT NULL, to_did TEXT NOT NULL,"
                "ok INTEGER NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,"
                "delivered INTEGER NOT NULL DEFAULT 0)")
        # migration：舊 db 補 delivered 欄（已存在則忽略）→ ack 改標記送達、不刪，結果可在控制台留存
        with self._lock:
            try:
                with self._db:
                    self._db.execute(
                        "ALTER TABLE results ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    def add_task(self, task_id: str, from_did: str, prompt: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO tasks(id, from_did, prompt, status, created) "
                "VALUES(?,?,?,'pending',?)", (task_id, from_did, prompt, time.time()))

    def claim_next_task(self) -> Optional[dict]:
        """原子取一個 pending → 標 running，回 {id, from_did, prompt}。無則 None。"""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id, from_did, prompt FROM tasks WHERE status='pending' "
                "ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            self._db.execute("UPDATE tasks SET status='running' WHERE id=?", (row[0],))
            return {"id": row[0], "from_did": row[1], "prompt": row[2]}

    def finish_task(self, task_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))

    def requeue_running(self) -> int:
        """啟動時把卡在 'running' 的任務還原為 'pending'（broker 中途重啟的崩潰復原，
        at-least-once）。回重新排入的筆數。"""
        with self._lock, self._db:
            cur = self._db.execute("UPDATE tasks SET status='pending' WHERE status='running'")
            return cur.rowcount

    def add_result(self, ref: str, to_did: str, ok: bool, text: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO results(id, ref, to_did, ok, text, created) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, ref, to_did, 1 if ok else 0, text[:MAX_RESULT_CHARS], time.time()))

    def pending_results(self, to_did: str) -> list[dict]:
        """手機待收的結果：只回尚未送達（delivered=0）的，避免重複通知。"""
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, ref, ok, text, created FROM results "
                "WHERE to_did=? AND delivered=0 ORDER BY created", (to_did,)).fetchall()
        return [{"id": r[0], "ref": r[1], "ok": bool(r[2]), "text": r[3], "created": r[4]}
                for r in rows]

    def mark_delivered(self, ids: list[str], to_did: str) -> None:
        """標記結果為已送達（不刪除，保留供桌面控制台檢視），**綁定擁有者 to_did**：
        一個已配對 node 不能 ack/標記別人的結果。poll 之後就不再回這些 → 不重複通知。"""
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE results SET delivered=1 WHERE id=? AND to_did=?", [(i, to_did) for i in ids])


# ── Broker + Worker ──────────────────────────────────────────────────────────

@dataclass
class MeshBroker:
    identity: pr.DeviceIdentity
    peers: hs.PeerStore
    store: MeshStore
    # 跑任務的指令；{prompt} 由 worker 以參數帶入（不做 shell 字串拼接，防注入）
    hermes_cmd: list[str] = field(default_factory=lambda: ["hermes", "-z"])
    home: Optional[str] = None
    host: str = ""          # 預設綁 LAN IP（見 start）；絕不 0.0.0.0
    port: int = 0           # 0 = 自動選

    _sock: Optional[socket.socket] = None
    _running: bool = False
    _zc = None
    _zc_info = None
    _pairing_until: float = 0.0   # 配對視窗到期時間戳（time.time()）；之前為未開放

    # ---- 配對視窗 ----
    def open_pairing(self, window_sec: int = 300) -> None:
        """開放時限配對視窗：期間內未配對 node 可用 pair op 加入信任。"""
        self._pairing_until = time.time() + window_sec

    def _pairing_open(self) -> bool:
        return time.time() < self._pairing_until

    # ---- 連線處理（每連線一個 op）----
    def _handle(self, conn: socket.socket):
        try:
            _peer = conn.getpeername()[0] if conn.fileno() != -1 else "?"
        except OSError:
            _peer = "?"
        try:
            hello = json.loads(hs._recv_frame(conn).decode("utf-8"))
            cdid, cpk = hello["did"], pr._b64d(hello["pk"])
            paired = self.peers.is_paired(cdid, cpk)
            # 未配對：只有在配對視窗內才允許繼續（為了走 pair op）；否則拒絕。
            if not paired and not self._pairing_open():
                print(f"[mesh] ✗ 拒絕 {_peer} did={cdid[:8]}：未配對且配對視窗已關", flush=True)
                hs._send_frame(conn, json.dumps({"ok": False, "err": "not paired"}).encode())
                return
            hs._send_frame(conn, json.dumps({"ok": True, "proto": PROTO, "paired": paired}).encode())

            # 加密請求：box_decrypt(broker_sk, cpk) 成功 = 對方持有 cpk 對應私鑰（認證該公鑰）
            # 且加密給 broker_pk（證明掃過 QR 拿到 broker 公鑰）。pair 即靠這兩點 + 時限窗建立信任。
            req = json.loads(pr.box_decrypt(self.identity.private_key, cpk, hs._recv_frame(conn)))
            op = req.get("op")
            if op != "poll":  # poll 每數秒一次太頻繁，不印避免洗版；其餘 op 留診斷軌跡
                print(f"[mesh] ← {_peer} did={cdid[:8]} op={op} paired={paired}", flush=True)
            if op == "pair":
                self._op_pair(conn, cdid, cpk)
                return
            # 其餘 op 一律要求已配對（配對視窗不等於可派工）
            if not paired:
                hs._send_frame(conn, json.dumps({"ok": False, "err": "not paired"}).encode())
                return
            if op == "push":
                self._op_push(conn, cdid, req)
            elif op == "poll":
                self._op_poll(conn, cpk, cdid)
            elif op == "ack":
                self.store.mark_delivered(list(req.get("ids", [])), cdid)  # 標記送達(不刪)、綁認證身分
                hs._send_frame(conn, json.dumps({"ok": True}).encode())
            elif op == "pull":
                self._op_pull(conn, cpk, req)   # 接力：匯出指定 session bundle、加密回傳
            elif op == "push_session":
                self._op_push_session(conn, req)  # 反向同步：手機上傳 bundle、冪等合併進 PC
            else:
                hs._send_frame(conn, json.dumps({"ok": False, "err": f"bad op: {op}"}).encode())
        except Exception as e:  # noqa: BLE001 — 單連線錯誤不拖垮 broker
            print(f"[mesh] ✗ 連線 {_peer} 處理錯誤：{type(e).__name__}: {e}", flush=True)
            try:
                hs._send_frame(conn, json.dumps({"ok": False, "err": str(e)}).encode())
            except OSError:
                pass
        finally:
            conn.close()

    def _op_pair(self, conn, cdid: str, cpk: bytes):
        """把手機公鑰加入信任（反向配對）。已配對 → idempotent 放行（重掃接力 QR 不該因配對
        視窗過期而失敗）；未配對則須在時限視窗內，窗外拒絕。"""
        if self.peers.is_paired(cdid, cpk):
            hs._send_frame(conn, json.dumps({"ok": True, "did": self.identity.device_id}).encode())
            return
        if not self._pairing_open():
            hs._send_frame(conn, json.dumps({"ok": False, "err": "pairing window closed"}).encode())
            return
        self.peers.add(cdid, cpk)
        hs._send_frame(conn, json.dumps({"ok": True, "did": self.identity.device_id}).encode())

    def _op_push(self, conn, cdid: str, req: dict):
        task = req.get("task") or {}
        prompt = (task.get("prompt") or "").strip()
        if not prompt:
            hs._send_frame(conn, json.dumps({"ok": False, "err": "empty prompt"}).encode())
            return
        tid = task.get("id") or uuid.uuid4().hex
        self.store.add_task(tid, cdid, prompt)
        hs._send_frame(conn, json.dumps({"ok": True, "id": tid}).encode())

    def _op_poll(self, conn, cpk: bytes, cdid: str):
        results = self.store.pending_results(cdid)
        payload = json.dumps({"ok": True, "results": results}, ensure_ascii=False).encode("utf-8")
        # 結果經 Box(broker_sk→client_pk) 加密 → 只有該手機能解 + 驗來源
        hs._send_frame(conn, pr.box_encrypt(self.identity.private_key, cpk, payload))

    def _op_pull(self, conn, cpk: bytes, req: dict):
        """接力 op：把指定 session 的 bundle 加密回傳給已配對手機（複用桌面匯出）。

        與協作（push/poll/ack）共用同一信任域、同一連線協定 → 一個 server、一次配對同時
        支援接力 + 協作。機密永不入 bundle（desktop_export 只讀 state.db + memories/，
        絕不碰 auth.json/.env）。回應協定：{ok} frame + Box(bundle) frame（對齊手機端 pull）。"""
        session_id = req.get("session_id")
        if not session_id:
            hs._send_frame(conn, json.dumps({"ok": False, "err": "no session_id"}).encode())
            return
        home = self.home or os.path.expanduser("~/.hermes")
        try:
            bundle = de.export_for_handoff(home, session_id,
                                           source_device=self.identity.device_id)
        except Exception as e:  # noqa: BLE001 — 匯出失敗（找不到 db / schema 過舊）誠實回報
            hs._send_frame(conn, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        if bundle is None:
            hs._send_frame(conn, json.dumps({"ok": False, "err": "session not found"}).encode())
            return
        payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
        hs._send_frame(conn, json.dumps({"ok": True}).encode())
        # bundle 經 Box(broker_sk→client_pk) 加密 → 只有該手機能解 + 驗來源
        hs._send_frame(conn, pr.box_encrypt(self.identity.private_key, cpk, payload))

    def _op_push_session(self, conn, req: dict):
        """反向同步 op（#22）：手機上傳本機全部對話 bundle → 以 import_all 冪等合併進 PC
        state.db + memories（by-id upsert + 訊息自然鍵去重 + memory append-union）。

        bundle 已在加密 req 內（box_decrypt 已解，全程密文）。回 {ok, stats}（與接力匯入同統計）。
        機密永不被影響：import 只寫 state.db + memories/，不碰 auth.json/.env。"""
        bundle = req.get("bundle")
        if not isinstance(bundle, dict):
            hs._send_frame(conn, json.dumps({"ok": False, "err": "no bundle"}).encode())
            return
        home = self.home or os.path.expanduser("~/.hermes")
        try:
            stats = hc.import_all(home, bundle)
        except Exception as e:  # noqa: BLE001 — 匯入失敗（schema 不符 / db 鎖）誠實回報
            hs._send_frame(conn, json.dumps({"ok": False, "err": str(e)}).encode())
            return
        hs._send_frame(conn, json.dumps({"ok": True, "stats": stats}, ensure_ascii=False).encode())

    # ---- worker：跑 hermes oneshot ----
    def _worker_loop(self):
        while self._running:
            task = self.store.claim_next_task()
            if task is None:
                time.sleep(1.0)
                continue
            print(f"[mesh] ▶ 收到任務 {task['id'][:8]} from={task['from_did'][:8]}: "
                  f"{task['prompt'][:80]}  → 執行 {' '.join(self.hermes_cmd)} …", flush=True)
            t0 = time.time()
            # 加可辨識前綴 → 桌面 session 歷史一眼看出是手機派的；獨立 session、彼此不共享上下文。
            ok, text = self._run_hermes(MESH_TASK_MARKER + task["prompt"])
            print(f"[mesh] {'✓' if ok else '✗'} 任務 {task['id'][:8]} 完成（{time.time()-t0:.1f}s）"
                  f"：{text[:100].replace(chr(10), ' ')}", flush=True)
            self.store.add_result(task["id"], task["from_did"], ok, text)
            self.store.finish_task(task["id"])
            print(f"[mesh] ⇧ 結果已入手機收件匣，等手機 poll 取走", flush=True)

    def _run_hermes(self, prompt: str) -> tuple[bool, str]:
        env = dict(os.environ)
        if self.home:
            env["HERMES_HOME"] = self.home
        try:
            proc = subprocess.run(
                self.hermes_cmd + [prompt], capture_output=True, text=True,
                env=env, timeout=900)  # 15 分鐘上限（agent 長任務）
            out = (proc.stdout or "").strip()
            if proc.returncode != 0:
                return False, (out + "\n" + (proc.stderr or "")).strip()[:MAX_RESULT_CHARS] \
                    or f"hermes exited {proc.returncode}"
            return True, out or "(no output)"
        except FileNotFoundError:
            return False, f"找不到 hermes 指令：{self.hermes_cmd[0]}（請確認已安裝且在 PATH）"
        except subprocess.TimeoutExpired:
            return False, "任務逾時（>15 分鐘）"
        except Exception as e:  # noqa: BLE001
            return False, f"執行錯誤：{e}"

    # ---- 生命週期 ----
    def start(self, advertise: bool = True) -> int:
        bind_host = self.host or hs._local_ip()  # LAN IP；絕不 0.0.0.0
        self.host = bind_host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind_host, self.port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._running = True
        requeued = self.store.requeue_running()  # 崩潰復原：把上次卡 running 的任務還原 pending
        if requeued:
            print(f"[mesh] 重新排入 {requeued} 個上次未完成的任務")

        def accept_loop():
            while self._running:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

        threading.Thread(target=accept_loop, name="mesh-accept", daemon=True).start()
        threading.Thread(target=self._worker_loop, name="mesh-worker", daemon=True).start()
        if advertise:
            self._advertise()
        return self.port

    def _advertise(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return
        self._zc = Zeroconf()
        name = f"hermes-mesh-{self.identity.device_id}.{SERVICE_TYPE}"
        self._zc_info = ServiceInfo(
            SERVICE_TYPE, name,
            addresses=[socket.inet_aton(self.host)], port=self.port,
            properties={"did": self.identity.device_id, "ver": str(PROTO)})
        self._zc.register_service(self._zc_info)

    def stop(self):
        self._running = False
        if self._zc is not None:
            try:
                self._zc.unregister_service(self._zc_info)
                self._zc.close()
            except Exception:  # noqa: BLE001
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def pair_qr(self) -> str:
        """純配對 QR（給手機掃建立信任）。複用 handoff v1 schema。"""
        return pr.build_pair_qr(self.identity, self.host, self.port)

    def handoff_qr(self, session_id: str) -> str:
        """接力 QR：配對資訊 + 指定 session_id。手機首掃即配對 + 選定要接收的對話。
        統一 server 後，接力與協作共用此身分；掃這個 QR 既建立信任、又指定接力 session。"""
        return pr.build_handoff_qr(self.identity, self.host, self.port, session_id)


# ── 獨立啟動（桌面一行指令）──────────────────────────────────────────────────

_MESH_SUBDIR = "mesh"  # ~/.hermes/mesh/（mesh 身分 + 配對清單，與 handoff 分開信任域）


def serve(home: Optional[str] = None, advertise: bool = True,
          hermes_cmd: Optional[list[str]] = None, host: str = "",
          port: int = DEFAULT_PORT) -> MeshBroker:
    home = home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    cfg = os.path.join(home, _MESH_SUBDIR)
    os.makedirs(cfg, exist_ok=True)
    identity = pr.load_or_create_identity(os.path.join(cfg, "id.key"))
    peers = hs.PeerStore(os.path.join(cfg, "peers.json"))
    store = MeshStore(os.path.join(cfg, "queue.db"))
    cmd = hermes_cmd or _default_hermes_cmd()
    # host 來源優先序：參數 > MESH_HOST 環境變數 > 自動 LAN（_local_ip）。
    # 跨網路（手機在 4G / 不同 Wi-Fi）時用 Tailscale：MESH_HOST=<你的 100.x> 或 --host。
    bind_host = host or os.environ.get("MESH_HOST", "")
    # port 固定（見 DEFAULT_PORT）：手機 peer 存的 port 要一直有效，不可隨機。
    bind_port = port if port is not None else int(os.environ.get("MESH_PORT", DEFAULT_PORT))
    broker = MeshBroker(identity=identity, peers=peers, store=store,
                        hermes_cmd=cmd, home=home, host=bind_host, port=bind_port)
    broker.start(advertise=advertise)
    return broker


def _default_hermes_cmd() -> list[str]:
    if os.environ.get("HERMES_MESH_CMD"):
        return os.environ["HERMES_MESH_CMD"].split()
    return ["hermes", "-z"]


def add_peer_from_phone(broker: MeshBroker, phone_did: str, phone_pk_b64: str) -> None:
    """手機掃 broker QR 後，把手機公鑰加入信任（反向配對）。
    M1：手機端配對請求會帶自己的 did/pk；此函式供配對流程呼叫存入 PeerStore。"""
    broker.peers.add(phone_did, pr._b64d(phone_pk_b64))


def main(argv=None) -> int:
    import argparse
    try:
        import qrcode  # 可選：終端印 QR 圖；無則只印文字
    except ImportError:
        qrcode = None

    ap = argparse.ArgumentParser(
        prog="hermes-companion",
        description="hermes 桌面伴隨服務：協作派工（mesh）+ 對話接力（handoff），一個進程、一次配對")
    ap.add_argument("--home", default=None, help="HERMES_HOME（預設 ~/.hermes）")
    ap.add_argument("--host", default="", help="綁定/QR 位址（跨網路給 Tailscale 100.x；預設自動 LAN）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"broker 綁定 port（預設固定 {DEFAULT_PORT}；固定才能讓手機配對後一直連得到）")
    ap.add_argument("--session", default=None,
                    help="接力指定 session：印接力 QR（手機掃後配對 + 接收此對話）。省略則印純配對 QR。")
    a = ap.parse_args(argv)

    broker = serve(a.home, host=a.host, port=a.port)
    broker.open_pairing(300)  # 啟動即開 5 分鐘配對視窗，讓手機掃 QR 後能完成反向配對
    print(f"[companion] device_id={broker.identity.device_id} bind={broker.host}:{broker.port}"
          f"（協作 + 接力；mDNS 廣告中、worker 待命、配對視窗 5 分鐘）")
    # 瀏覽器本地控制台（北極星：PC 零終端）——綁 127.0.0.1 只給本機瀏覽器，跨平台用 webbrowser 開。
    try:
        from companion_web import serve_web
        web_host, web_port = serve_web(broker)
        url = f"http://{web_host}:{web_port}/"
        print(f"控制台：{url}（正在開啟瀏覽器；手機掃頁面上的 QR 即可連結）")
        import webbrowser
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001 — 控制台失敗不影響 broker 本身，退回終端 QR
        print(f"（瀏覽器控制台啟動失敗：{e}；改用下方終端 QR）")

    # 終端文字 / ASCII QR（fallback：無 GUI / SSH 連線時）
    if a.session:
        print(f"接力 QR（session={a.session}）：")
        qr = broker.handoff_qr(a.session)
    else:
        print("配對 QR（文字；或直接用瀏覽器控制台的圖）：")
        qr = broker.pair_qr()
    print(qr)
    if qrcode is not None:
        try:
            q = qrcode.QRCode(border=2, box_size=1)
            q.add_data(qr)
            q.print_ascii(invert=True)
        except Exception:  # noqa: BLE001
            pass
    print("companion 執行中，Ctrl+C 結束。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        broker.stop()
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
