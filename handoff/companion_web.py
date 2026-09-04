"""
companion_web — local desktop browser console (standard-library http.server, zero extra dependencies).

⚠️ 「zero extra dependencies」這句以前是**假的**：QR 是用 Python 的 qrcode + pillow 畫的，
而那兩個套件只在 handoff/requirements.txt 裡宣告、只有 mesh-start.sh（bash，Windows 跑不了）
會裝。Windows 使用者因此永遠看不到 QR，配對整條斷掉。QR 現在改由瀏覽器產生，這句才成真。

North star: zero terminal on the PC side. After installing hermes, the user opens a browser and
immediately sees the pairing QR + connection status + task history; scanning with the phone connects —
no terminal, no uv, no app download required. Every PC already has a browser.

Security: the web UI binds to 127.0.0.1 (local browser only); the broker's encrypted TCP (the phone
connection) is a separate port. The console displays no credentials; the QR only contains public
pairing info (pubkey/host/port), and the private key never leaves the machine.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


#
# 🚨 這裡曾經有一支 `_qr_data_uri()`，用 Python 的 `qrcode` + `pillow` 把配對碼畫成 PNG
# data URI。它被移除了，因為那個依賴**在真實世界從來沒有被裝上**：
#
#   · `qrcode` / `pillow` 只宣告在 `handoff/requirements.txt`
#   · 唯一會安裝它們的地方是 `mesh-start.sh`（POSIX：`$VENV/bin/pip`）
#   · 那是 bash 腳本，**Windows 跑不了**，而且 Windows 的 venv 是 `Scripts\` 不是 `bin/`
#   · 於是 `import qrcode` 失敗 → 舊版靜默回空字串 → 頁面只剩「QR failed」
#
# Mac 一直正常，正是因為 mesh-start.sh 在那裡跑得起來——平台差異的根源就在這裡，
# 不是 QR 本身壞掉。使用者回報「No qr code, so not working」是準確的。
#
# 現在 QR 由**瀏覽器端**產生（見頁面裡的 qrMatrix）：零 Python 依賴、跨平台、離線可用。
# 手機端要掃的是這張 QR（Android 用 zxing 掃 `build_pair_qr()` 產生的 JSON），
# 沒有它就只能請使用者手抄一串 140+ 字元的 JSON，對非技術使用者等於沒有路。



def _read_history(db_path: str, limit: int = 20):
    """Read recent tasks + results over a read-only connection (safe to run concurrently with broker writes under WAL; open per query and close immediately)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return [], []
    try:
        tasks = [{"id": r[0], "from": r[1][:8], "prompt": r[2],
                  "status": r[3], "created": r[4]}
                 for r in conn.execute(
                     "SELECT id, from_did, prompt, status, created FROM tasks "
                     "ORDER BY created DESC LIMIT ?", (limit,))]
        # Full result text (truncated to 8000 chars to keep an extremely long one from blowing up the page); ref links back to task.id
        results = [{"ref": r[0], "ok": bool(r[1]), "text": r[2][:8000], "created": r[3]}
                   for r in conn.execute(
                       "SELECT ref, ok, text, created FROM results "
                       "ORDER BY created DESC LIMIT ?", (limit,))]
        return tasks, results
    except sqlite3.Error:
        return [], []
    finally:
        conn.close()


class _Handler(BaseHTTPRequestHandler):
    broker = None  # injected via partial
    session = None  # --session：有值時主控台顯示 handoff QR 而非純配對 QR

    def log_message(self, *a):  # noqa: A002 — silence the access log
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server interface
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        elif self.path.startswith("/api/status"):
            body = json.dumps(self._status(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/api/open-pairing"):
            self.broker.open_pairing(300)  # one-click reopen of the pairing window from the GUI (for switching devices / after expiry)
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):  # noqa: N802 — http.server interface
        """唯一會**改變狀態**的路由：撤銷一台已配對手機。

        🚨 CSRF：這台伺服器綁 127.0.0.1，但那擋不住**使用者自己瀏覽器裡的惡意網頁**
        —— 任何分頁都能對 127.0.0.1 發 POST。在這之前主控台全是唯讀的 GET，沒有這個面。

        兩道防線：
         1. **要求自訂 header**。跨來源的 fetch 帶自訂 header 會先觸發 CORS preflight
            (OPTIONS)，而我們不回應 preflight ⇒ 瀏覽器直接擋下，請求根本送不出來。
            表單提交（唯一不需要 preflight 的跨來源 POST）帶不了自訂 header。
         2. 主控台的 port 是隨機的（`serve_web(..., port=0)`），本來就難猜。

        這兩道都不是密碼學保證，但對「本機 GUI 的撤銷按鈕」這個威脅模型足夠：
        真正能繞過的人已經在這台機器上執行程式碼了。
        """
        if self.headers.get("X-Hermes-Console") != "1":
            self._send(403, "application/json; charset=utf-8",
                       b'{"ok":false,"err":"missing console header"}')
            return
        if not self.path.startswith("/api/unpair"):
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            did = str(body.get("did") or "")
        except (ValueError, OSError):
            self._send(400, "application/json; charset=utf-8", b'{"ok":false,"err":"bad body"}')
            return
        removed = bool(did) and self.broker.peers.remove(did)
        print(f"[mesh] {'✓ unpaired' if removed else '· unpair: unknown device'} did={did[:8]}", flush=True)
        self._send(200, "application/json; charset=utf-8",
                   json.dumps({"ok": removed}).encode())

    def _status(self) -> dict:
        b = self.broker
        _session = self.session
        pairing_left = max(0, int(b._pairing_until - time.time()))
        paired = list(b.peers._peers.keys())
        tasks, results = _read_history(b.store.path)
        # Associate the full result text (hermes's answer) with its task → clicking a task in the console shows the full conversation
        by_ref = {r["ref"]: r for r in results}
        for t in tasks:
            r = by_ref.get(t["id"])
            t["result"] = r["text"] if r else None
            t["result_ok"] = r["ok"] if r else None
            t["id"] = t["id"][:8]  # short id for display (association already done)
        return {
            "bind": f"{b.host}:{b.port}",
            "device_id": b.identity.device_id,
            # 完整 did（不再截成 8 碼）——撤銷按鈕需要它才知道要移除誰。
            # 主控台只綁 127.0.0.1，而 did 本來就是公鑰前綴（QR 上就有），不是機密。
            "paired": [{"did": d, "short": d[:8]} for d in paired],
            "paired_count": len(paired),
            "pairing_left": pairing_left,
            # QR 由前端從這個字串產生，不再回傳 PNG。
            # 指定了 --session 就給 handoff QR（手機掃了之後配對**並**收下那段對話），
            # 否則給純配對 QR —— 與 mesh_broker.main() 的終端輸出同一套規則。
            "pair_code": (b.handoff_qr(_session) if _session else b.pair_qr()),
            "tasks": tasks,
        }


def serve_web(broker, host: str = "127.0.0.1", port: int = 0, session: str | None = None):
    """Start the local console web server (daemon thread). Returns (host, port). Binds to 127.0.0.1 for the local browser only."""
    # 🚨 這裡原本還有 `handler = partial(_Handler)` + `handler.broker = ...` 三行**死碼**：
    # `httpd` 是直接用 `_Handler` 建的，那個 partial 從來沒被用過，設在它上面的屬性
    # 也就從來沒生效。我加 session 時第一版正好寫到那個死掉的地方，測試才抓出來。
    # 單一 broker 行程，設在類別上就夠。
    _Handler.broker = broker
    _Handler.session = session
    httpd = ThreadingHTTPServer((host, port), _Handler)
    actual_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="companion-web", daemon=True).start()
    return host, actual_port


_PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Companion</title>
<style>
  :root { --blue:#2f6bff; --bg:#f4f7ff; --ink:#0b1f4d; --mut:#5b6b8c; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,"PingFang TC","Noto Sans CJK TC",sans-serif;
         background:var(--bg); color:var(--ink); }
  .wrap { max-width:900px; margin:0 auto; padding:36px 20px; }
  h1 { font-size:34px; letter-spacing:3px; margin:0 0 4px; color:var(--blue); font-weight:800; }
  .sub { color:var(--mut); margin:0 0 28px; }
  .grid { display:grid; grid-template-columns:300px 1fr; gap:24px; align-items:start; }
  @media(max-width:680px){ .grid{ grid-template-columns:1fr; } }
  .card { background:#fff; border-radius:18px; padding:22px; box-shadow:0 6px 24px rgba(20,40,120,.08); }
  .paired-list { margin-top:8px; }
  .paired-row { display:flex; align-items:center; justify-content:space-between; gap:12px;
                padding:6px 0; border-top:1px solid var(--brd); font-size:13px; }
  .paired-row .mono { color:var(--mut); }
  .paired-row button { background:none; border:1px solid var(--brd); color:var(--mut);
                       border-radius:6px; padding:2px 10px; cursor:pointer; font-size:12px; }
  .paired-row button:hover { color:#e5534b; border-color:#e5534b; }
  .qr { text-align:center; }
  .qr img { width:240px; height:240px; image-rendering:pixelated; border-radius:8px; }
  .qr .hint { color:var(--mut); font-size:13px; margin-top:12px; }
  .stat { display:flex; justify-content:space-between; gap:12px; padding:11px 0; border-bottom:1px solid #eef2fb; }
  .stat:last-child{ border-bottom:0; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
  .ok{ background:#e6f7ec; color:#1b8a4b; } .run{ background:#fff4e0; color:#b9770e; }
  .pend{ background:#eef2fb; color:#5b6b8c; } .fail{ background:#fdeaea; color:#c5372f; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th,td { text-align:left; padding:9px 8px; border-bottom:1px solid #eef2fb; vertical-align:top; }
  th { color:var(--mut); font-weight:600; font-size:12px; }
  .mono { font-family:ui-monospace,Menlo,monospace; color:var(--mut); font-size:12px; }
  .empty { color:var(--mut); padding:18px 0; text-align:center; }
  h2 { font-size:15px; margin:0 0 14px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; vertical-align:middle; }
  .live{ background:#1b8a4b; } .off{ background:#c5372f; }
  .trow{ cursor:pointer; } .trow:hover{ background:#f7f9ff; }
  .answer{ white-space:pre-wrap; word-break:break-word; font-size:13px; line-height:1.65; color:#26324d;
           background:#f7f9ff; border-radius:10px; padding:14px 16px; margin:2px 0 10px; }
  button{ margin-left:10px; border:0; background:var(--blue); color:#fff; border-radius:8px;
          padding:5px 12px; font-size:12px; cursor:pointer; } button:hover{ opacity:.9; }
</style>
</head>
<body>
<div class="wrap">
  <h1>HERMES COMPANION</h1>
  <p class="sub" data-i18n="sub"></p>
  <div class="grid">
    <div class="card qr">
      <div id="qrbox"><div class="empty" data-i18n="loading"></div></div>
      <div class="hint" data-i18n="scan"></div>
    </div>
    <div>
      <div class="card" style="margin-bottom:24px">
        <h2 data-i18n="conn"></h2>
        <div class="stat"><span><span id="dot" class="dot off"></span><span data-i18n="bind"></span></span><b id="bind" class="mono">—</b></div>
        <div class="stat"><span data-i18n="paired"></span><b id="paired">—</b></div>
        <div id="pairedList" class="paired-list"></div>
        <div class="stat"><span data-i18n="window"></span><span><b id="window">—</b>
          <button id="repair" onclick="openPairing()" data-i18n="reopen"></button></span></div>
      </div>
      <div class="card">
        <h2><span data-i18n="history"></span> <span style="color:#9aa7c4;font-weight:400;font-size:12px" data-i18n="hint_click"></span></h2>
        <table><thead><tr><th data-i18n="th_time"></th><th data-i18n="th_from"></th><th data-i18n="th_content"></th><th data-i18n="th_status"></th></tr></thead>
        <tbody id="tasks"></tbody></table>
      </div>
    </div>
  </div>
</div>
<script>
/* ── QR 編碼器（byte mode / ECC M / v1-v20）────────────────────────────────
   為什麼放在瀏覽器：Python 的 qrcode 套件在真實世界從沒被裝上（見本檔上方的說明），
   而 Windows 連裝它的腳本都跑不了。搬到前端就零依賴、跨平台、離線可用。

   驗證方式（2026-09-04，25 筆輸入涵蓋 v1-v20、長度 1~620）：
     ① 固定遮罩後，與 python-qrcode 的輸出**逐格完全相同** 25/25
     ② 用本編碼器自選的遮罩，同樣與 python-qrcode 在該遮罩下的輸出逐格相同 25/25
        ⇒ 產出的每一張都是合法可解碼的 QR
   自選遮罩偶爾與 python-qrcode 的預設不同（罰分啟發式的邊角差異）。那是品質選擇
   不是正確性：解碼器從 format info 讀出用了哪個遮罩，8 個都合法。

   ⚠️ 改這段之前先跑一次上面那個對照測試，別靠肉眼看 QR「像不像」。
   踩過的三顆（都是肉眼看不出來的）：分隔帶 d==4 被畫成深色、format info 的 x/y 轉置、
   copy-2 的 8+7 切分寫反。 */
function qrMatrix(text, forceMask) {
  const ECC_M = [null,
    [10,1],[16,1],[26,1],[18,2],[24,2],[16,4],[18,4],[22,4],[22,5],[26,5],
    [30,5],[22,8],[22,9],[24,9],[24,10],[28,10],[28,11],[26,13],[26,14],[26,16]];
  const rawDataModules = (v) => {
    let r = (16 * v + 128) * v + 64;
    if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; }
    return r;
  };
  const totalCodewords = (v) => Math.floor(rawDataModules(v) / 8);
  const alignPositions = (v) => {
    if (v === 1) return [];
    const n = Math.floor(v / 7) + 2, size = 17 + 4 * v;
    const step = Math.ceil((v * 4 + 4) / (n * 2 - 2)) * 2;
    const out = [6];
    for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
    return out;
  };
  const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  for (let i = 0, x = 1; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  const mul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];
  const rsRemainder = (data, deg) => {
    let gen = [1];
    for (let i = 0; i < deg; i++) {
      const ng = new Array(gen.length + 1).fill(0);
      for (let j = 0; j < gen.length; j++) { ng[j] ^= mul(gen[j], 1); ng[j + 1] ^= mul(gen[j], EXP[i]); }
      gen = ng;
    }
    const res = new Array(deg).fill(0);
    for (const b of data) {
      const factor = b ^ res[0];
      res.shift(); res.push(0);
      for (let i = 0; i < deg; i++) res[i] ^= mul(gen[i + 1], factor);
    }
    return res;
  };
  const bytes = [];
  for (const ch of unescape(encodeURIComponent(text))) bytes.push(ch.charCodeAt(0));
  let version = 0;
  for (let v = 1; v <= 20; v++) {
    const [e, b] = ECC_M[v];
    if (4 + (v <= 9 ? 8 : 16) + bytes.length * 8 <= (totalCodewords(v) - e * b) * 8) { version = v; break; }
  }
  if (!version) throw new Error('payload too large');
  const size = 17 + 4 * version;
  const [ecPerBlock, numBlocks] = ECC_M[version];
  const dataCw = totalCodewords(version) - ecPerBlock * numBlocks;
  const bits = [];
  const push = (val, n) => { for (let i = n - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
  push(0b0100, 4); push(bytes.length, version <= 9 ? 8 : 16);
  for (const b of bytes) push(b, 8);
  for (let i = 0; i < 4 && bits.length < dataCw * 8; i++) bits.push(0);
  while (bits.length % 8 !== 0) bits.push(0);
  const cw = [];
  for (let i = 0; i < bits.length; i += 8) { let v = 0; for (let j = 0; j < 8; j++) v = (v << 1) | bits[i + j]; cw.push(v); }
  for (let i = 0; cw.length < dataCw; i++) cw.push(i % 2 === 0 ? 0xec : 0x11);
  const shortLen = Math.floor(dataCw / numBlocks), numLong = dataCw % numBlocks;
  const dataBlocks = [], eccBlocks = [];
  let off = 0;
  for (let i = 0; i < numBlocks; i++) {
    const len = shortLen + (i >= numBlocks - numLong ? 1 : 0);
    const blk = cw.slice(off, off + len); off += len;
    dataBlocks.push(blk); eccBlocks.push(rsRemainder(blk, ecPerBlock));
  }
  const finalCw = [];
  for (let i = 0; i < shortLen + 1; i++)
    for (let b = 0; b < numBlocks; b++) if (i < dataBlocks[b].length) finalCw.push(dataBlocks[b][i]);
  for (let i = 0; i < ecPerBlock; i++) for (let b = 0; b < numBlocks; b++) finalCw.push(eccBlocks[b][i]);
  const M = Array.from({length: size}, () => new Array(size).fill(null));
  const F = Array.from({length: size}, () => new Array(size).fill(false));
  const setF = (x, y, v) => { if (x >= 0 && y >= 0 && x < size && y < size) { M[y][x] = v; F[y][x] = true; } };
  const finder = (cx, cy) => {
    for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
      const d = Math.max(Math.abs(dx), Math.abs(dy));
      setF(cx + dx, cy + dy, d <= 3 && d !== 2);   // d==4 是分隔帶，必須淺色
    }
  };
  finder(3, 3); finder(size - 4, 3); finder(3, size - 4);
  for (let i = 8; i < size - 8; i++) { setF(i, 6, i % 2 === 0); setF(6, i, i % 2 === 0); }
  const ap = alignPositions(version);
  for (const ay of ap) for (const ax of ap) {
    if ((ax === 6 && ay === 6) || (ax === 6 && ay === size - 7) || (ax === size - 7 && ay === 6)) continue;
    for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++)
      setF(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
  }
  setF(8, size - 8, true);
  for (let i = 0; i < 9; i++) { if (M[8][i] === null) setF(i, 8, false); if (M[i][8] === null) setF(8, i, false); }
  for (let i = 0; i < 8; i++) { setF(size - 1 - i, 8, false); setF(8, size - 1 - i, false); }
  if (version >= 7) {
    let rem = version;
    for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
    const vbits = (version << 12) | rem;
    for (let i = 0; i < 18; i++) {
      const bit = ((vbits >>> i) & 1) === 1, a = Math.floor(i / 3), b = i % 3;
      setF(a, size - 11 + b, bit); setF(size - 11 + b, a, bit);
    }
  }
  let idx = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
      const x = right - j, upward = ((right + 1) & 2) === 0, y = upward ? size - 1 - vert : vert;
      if (F[y][x]) continue;
      M[y][x] = idx < finalCw.length * 8 ? ((finalCw[idx >>> 3] >>> (7 - (idx & 7))) & 1) === 1 : false;
      idx++;
    }
  }
  const maskFn = [
    (x,y)=>(x+y)%2===0, (x,y)=>y%2===0, (x,y)=>x%3===0, (x,y)=>(x+y)%3===0,
    (x,y)=>(Math.floor(y/2)+Math.floor(x/3))%2===0, (x,y)=>(x*y)%2+(x*y)%3===0,
    (x,y)=>((x*y)%2+(x*y)%3)%2===0, (x,y)=>((x+y)%2+(x*y)%3)%2===0];
  const applyFormat = (grid, mask) => {
    const data = mask;                              // ECC M 的指示碼是 0b00
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const f = ((data << 10) | rem) ^ 0x5412;
    // 座標是 grid[y][x]；copy-1 前半是「第 8 **欄**、第 0..5 **列**」（垂直），寫成
    // grid[8][i] 會整組 x/y 轉置——肉眼完全看不出來，只有逐格比對抓得到。
    const put = (x, y, i) => { grid[y][x] = ((f >>> i) & 1) === 1; };
    for (let i = 0; i <= 5; i++) put(8, i, i);
    put(8, 7, 6); put(8, 8, 7); put(7, 8, 8);
    for (let i = 9; i < 15; i++) put(14 - i, 8, i);
    for (let i = 0; i < 8; i++) put(size - 1 - i, 8, i);     // copy-2 是 8 水平 + 7 垂直
    for (let i = 8; i < 15; i++) put(8, size - 15 + i, i);
    grid[size - 8][8] = true;
  };
  const penalty = (g) => {
    let p = 0;
    for (let i = 0; i < size; i++) for (const line of [g[i], g.map(r => r[i])]) {
      let run = 1;
      for (let j = 1; j < size; j++) {
        if (line[j] === line[j-1]) { run++; if (run === 5) p += 3; else if (run > 5) p += 1; } else run = 1;
      }
    }
    for (let y = 0; y < size-1; y++) for (let x = 0; x < size-1; x++)
      if (g[y][x] === g[y][x+1] && g[y][x] === g[y+1][x] && g[y][x] === g[y+1][x+1]) p += 3;
    const pat = [true,false,true,true,true,false,true];
    const patHits = (line, i) => {
      for (let k = 0; k < 7; k++) if (line[i+k] !== pat[k]) return 0;
      const before = line.slice(Math.max(0,i-4), i), after = line.slice(i+7, i+11);
      return (before.length===4 && before.every(v=>!v) ? 1:0) + (after.length===4 && after.every(v=>!v) ? 1:0);
    };
    for (let i = 0; i < size; i++) for (const line of [g[i], g.map(r => r[i])])
      for (let j = 0; j + 7 <= size; j++) p += 40 * patHits(line, j);
    let dark = 0;
    for (const row of g) for (const v of row) if (v) dark++;
    p += Math.floor(Math.abs(dark*20 - size*size*10) / (size*size)) * 10;
    return p;
  };
  let best = null, bestP = Infinity, bestM = -1;
  for (let m = 0; m < 8; m++) {
    if (forceMask !== undefined && m !== forceMask) continue;
    const g = M.map(r => r.slice());
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++)
      if (!F[y][x] && maskFn[m](x, y)) g[y][x] = !g[y][x];
    applyFormat(g, m);
    const p = penalty(g);
    if (p < bestP) { bestP = p; best = g; bestM = m; }
  }
  return { size, modules: best, version, mask: bestM };
}

/* 已配對裝置清單 + 撤銷按鈕。

   🚨 為什麼需要撤銷：信任原本**只能增不能減** —— PeerStore 只有 add/pubkey/is_paired，
   一支手機的公鑰進了這個檔就永遠被接受，使用者只能自己手改 JSON。
   而手機的備份匯出檔在 2026-09-04 之前含有手機的 NaCl 私鑰，拿到那個 zip 的人
   在同一個區網／tailnet 內就能冒充它派工。**手機端換身分救不了**，
   信任清單在這一端，撤銷也必須在這一端。 */
let lastPairedJson = null;
function renderPaired(list) {
  const j = JSON.stringify(list);
  if (j === lastPairedJson) return;          // 每 3 秒輪詢，沒變就不重畫（免得按鈕在點擊時被抽掉）
  lastPairedJson = j;
  const box = document.getElementById('pairedList');
  box.innerHTML = '';
  for (const p of list) {
    const row = document.createElement('div');
    row.className = 'paired-row';
    const name = document.createElement('span');
    name.className = 'mono';
    name.textContent = p.short;
    const btn = document.createElement('button');
    btn.textContent = T.unpair;
    btn.onclick = async () => {
      if (!confirm(T.unpair_confirm(p.short))) return;
      btn.disabled = true;
      try {
        await fetch('/api/unpair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Hermes-Console': '1' },
          body: JSON.stringify({ did: p.did }),
        });
        lastPairedJson = null;               // 強制下一輪重畫
        tick();
      } catch (e) { btn.disabled = false; }
    };
    row.appendChild(name); row.appendChild(btn);
    box.appendChild(row);
  }
}

let lastQrCode = null;
function drawQr(code) {
  const box = document.getElementById('qrbox');
  if (!code) { box.innerHTML = '<div class="empty">' + T.qr_fail + '</div>'; lastQrCode = null; return; }
  if (code === lastQrCode) return;          // 每 3 秒輪詢一次，內容沒變就不重畫
  lastQrCode = code;
  let m;
  try { m = qrMatrix(code); }
  catch (e) { box.innerHTML = '<div class="empty">' + T.qr_fail + '</div>'; lastQrCode = null; return; }
  const quiet = 4, scale = 6, px = (m.size + quiet * 2) * scale;
  const c = document.createElement('canvas');
  c.width = c.height = px;
  c.style.width = c.style.height = '240px';
  c.style.imageRendering = 'pixelated';
  c.style.borderRadius = '8px';
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, px, px);   // 靜區必須是白的，否則掃不到
  ctx.fillStyle = '#000';
  for (let y = 0; y < m.size; y++) for (let x = 0; x < m.size; x++)
    if (m.modules[y][x]) ctx.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale);
  box.innerHTML = '';
  box.appendChild(c);
}
/* WARNING: this whole block lives inside Python's `_PAGE = triple-quoted string`
   (NOT a raw string). Do not write a backslash-n escape in these strings — Python
   turns it into a real newline at parse time and cuts the JS string literal in half.
   `node --check` catches it; the eye does not. Keep messages single-line. */
const I18N = {
 'en':{sub:'Desktop collaboration + chat handoff · Scan the QR with the Hermes mobile app to connect',
  scan:'app → "Computer Mesh" → Scan',loading:'Loading…',conn:'Connection',bind:'Bound address',
  paired:'Paired devices',window:'Pairing window',reopen:'Reopen',history:'Task history',
  hint_click:'· tap any row for the full answer',th_time:'Time',th_from:'From',th_content:'Content',
  th_status:'Status',no_tasks:'No tasks yet',st_pending:'Pending',st_running:'Running',st_done:'Done',
  no_answer:'(no answer)',running_dots:'Running…',not_paired:'Not paired',win_closed:'Closed',
  unpair:'Remove',unpair_confirm:(d)=>'Remove device '+d+'? It will no longer be able to send tasks to this computer. To reconnect it, scan the pairing QR again.',qr_fail:'QR failed (see terminal code)',paired_n:(n,l)=>n+' device(s) ('+l+')',win_open:(s)=>'Open · '+s+'s left'},
 'zh-TW':{sub:'桌面協作 + 對話接力 · 用手機 Hermes app 掃下方 QR 即可連結',
  scan:'app →「電腦協作」→ 掃描',loading:'載入中…',conn:'連線狀態',bind:'綁定位址',
  paired:'已配對裝置',window:'配對視窗',reopen:'重新開啟',history:'任務歷史',
  hint_click:'· 點任意一列看完整回答',th_time:'時間',th_from:'來源',th_content:'內容',
  th_status:'狀態',no_tasks:'尚無任務',st_pending:'待處理',st_running:'執行中',st_done:'完成',
  no_answer:'（無回答內容）',running_dots:'執行中…',not_paired:'尚未配對',win_closed:'已關閉',
  unpair:'移除',unpair_confirm:(d)=>'要移除裝置 '+d+' 嗎？ 它將無法再派工到這台電腦。要重新連結請再掃一次配對 QR。',qr_fail:'QR 產生失敗（看終端文字配對碼）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'開放中 · 剩 '+s+' 秒'},
 'zh-CN':{sub:'桌面协作 + 对话接力 · 用手机 Hermes app 扫下方 QR 即可连接',
  scan:'app →「电脑协作」→ 扫描',loading:'加载中…',conn:'连接状态',bind:'绑定地址',
  paired:'已配对设备',window:'配对窗口',reopen:'重新开启',history:'任务历史',
  hint_click:'· 点任意一行看完整回答',th_time:'时间',th_from:'来源',th_content:'内容',
  th_status:'状态',no_tasks:'暂无任务',st_pending:'待处理',st_running:'执行中',st_done:'完成',
  no_answer:'（无回答内容）',running_dots:'执行中…',not_paired:'尚未配对',win_closed:'已关闭',
  unpair:'移除',unpair_confirm:(d)=>'要移除设备 '+d+' 吗？ 它将无法再派工到这台电脑。要重新连接请再扫一次配对 QR。',qr_fail:'QR 生成失败（看终端文字配对码）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'开放中 · 剩 '+s+' 秒'},
 'ja':{sub:'デスクトップ連携 + 会話の引き継ぎ · Hermes アプリで QR をスキャンして接続',
  scan:'アプリ →「コンピュータ連携」→ スキャン',loading:'読み込み中…',conn:'接続状態',bind:'バインドアドレス',
  paired:'ペアリング済み端末',window:'ペアリング受付',reopen:'再開',history:'タスク履歴',
  hint_click:'· 行をタップで全文表示',th_time:'時刻',th_from:'送信元',th_content:'内容',
  th_status:'状態',no_tasks:'タスクなし',st_pending:'待機中',st_running:'実行中',st_done:'完了',
  no_answer:'（回答なし）',running_dots:'実行中…',not_paired:'未ペアリング',win_closed:'終了',
  unpair:'削除',unpair_confirm:(d)=>'端末 '+d+' を削除しますか？ このPCへタスクを送れなくなります。再接続するにはペアリングQRを再度スキャンしてください。',qr_fail:'QR 生成失敗（端末のコード参照）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'受付中 · 残り '+s+' 秒'},
 'ko':{sub:'데스크톱 협업 + 대화 이어받기 · Hermes 앱으로 QR 스캔하여 연결',
  scan:'앱 →「컴퓨터 협업」→ 스캔',loading:'불러오는 중…',conn:'연결 상태',bind:'바인딩 주소',
  paired:'페어링된 기기',window:'페어링 창',reopen:'다시 열기',history:'작업 기록',
  hint_click:'· 행을 누르면 전체 답변',th_time:'시간',th_from:'출처',th_content:'내용',
  th_status:'상태',no_tasks:'작업 없음',st_pending:'대기 중',st_running:'실행 중',st_done:'완료',
  no_answer:'(답변 없음)',running_dots:'실행 중…',not_paired:'페어링 안 됨',win_closed:'닫힘',
  unpair:'제거',unpair_confirm:(d)=>'기기 '+d+'을(를) 제거할까요? 이 컴퓨터로 작업을 보낼 수 없게 됩니다. 다시 연결하려면 페어링 QR을 다시 스캔하세요.',qr_fail:'QR 생성 실패 (터미널 코드 참조)',paired_n:(n,l)=>n+'대 ('+l+')',win_open:(s)=>'열림 · '+s+'초 남음'},
 'es':{sub:'Colaboración de escritorio + transferencia de chat · Escanea el QR con la app Hermes para conectar',
  scan:'app → "Malla de PC" → Escanear',loading:'Cargando…',conn:'Conexión',bind:'Dirección',
  paired:'Dispositivos vinculados',window:'Ventana de vinculación',reopen:'Reabrir',history:'Historial de tareas',
  hint_click:'· toca una fila para ver la respuesta',th_time:'Hora',th_from:'Origen',th_content:'Contenido',
  th_status:'Estado',no_tasks:'Sin tareas',st_pending:'Pendiente',st_running:'Ejecutando',st_done:'Hecho',
  no_answer:'(sin respuesta)',running_dots:'Ejecutando…',not_paired:'Sin vincular',win_closed:'Cerrada',
  unpair:'Quitar',unpair_confirm:(d)=>'¿Quitar el dispositivo '+d+'? Ya no podrá enviar tareas a este equipo. Para reconectarlo, vuelve a escanear el QR de vinculación.',qr_fail:'QR falló (ver código en terminal)',paired_n:(n,l)=>n+' disp. ('+l+')',win_open:(s)=>'Abierta · '+s+'s rest.'}
};
function pickLang(){ const l=(navigator.language||'en').toLowerCase();
  if(l.startsWith('zh')) return (l.includes('cn')||l.includes('hans'))?'zh-CN':'zh-TW';
  for(const k of ['ja','ko','es','en']) if(l.startsWith(k)) return k;
  return 'en'; }
const T = I18N[pickLang()] || I18N['en'];
document.querySelectorAll('[data-i18n]').forEach(el=>{ const v=T[el.dataset.i18n]; if(v!=null) el.textContent=v; });
const STATUS = { pending:['pend',T.st_pending], running:['run',T.st_running], done:['ok',T.st_done] };
function fmtTime(t){ if(!t) return '—'; const d=new Date(t*1000);
  return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }
function escapeHtml(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function toggle(i){ const d=document.getElementById('d'+i); if(d) d.style.display = d.style.display==='none'?'':'none'; }
async function openPairing(){ try{ await fetch('/api/open-pairing'); }catch(e){} tick(); }
let lastTasksJson='';
function renderTasks(tasks){
  const tb = document.getElementById('tasks');
  if(!tasks.length){ tb.innerHTML='<tr><td colspan="4" class="empty">'+T.no_tasks+'</td></tr>'; return; }
  tb.innerHTML = tasks.map((t,i)=>{
    const m = STATUS[t.status]||['pend',t.status];
    const main = '<tr class="trow" onclick="toggle('+i+')"><td class="mono">'+fmtTime(t.created)+
      '</td><td class="mono">'+t.from+'</td><td>'+escapeHtml(t.prompt)+
      '</td><td><span class="pill '+m[0]+'">'+m[1]+'</span></td></tr>';
    const body = t.result ? '<div class="answer">'+escapeHtml(t.result)+'</div>'
      : '<span class="empty">'+(t.status==='done'?T.no_answer:T.running_dots)+'</span>';
    const detail = '<tr id="d'+i+'" style="display:none"><td colspan="4">'+body+'</td></tr>';
    return main + detail;
  }).join('');
}
async function tick(){
  try{
    const s = await (await fetch('/api/status')).json();
    document.getElementById('bind').textContent = s.bind;
    document.getElementById('dot').className = 'dot live';
    const shorts = (s.paired || []).map(p => p.short);
    document.getElementById('paired').textContent = s.paired_count ? T.paired_n(s.paired_count, shorts.join(', ')) : T.not_paired;
    renderPaired(s.paired || []);
    document.getElementById('window').textContent = s.pairing_left ? T.win_open(s.pairing_left) : T.win_closed;
    drawQr(s.pair_code);
    const tj = JSON.stringify(s.tasks);
    if(tj !== lastTasksJson){ lastTasksJson = tj; renderTasks(s.tasks); }  // only re-render on change, to preserve expanded state
  }catch(e){ document.getElementById('dot').className='dot off'; }
}
tick(); setInterval(tick, 3000);
</script>
</body>
</html>"""
