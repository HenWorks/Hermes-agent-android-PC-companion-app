"""
companion_web — 桌面瀏覽器本地控制台（標準庫 http.server，零外加相依）。

北極星：PC 端零終端。用戶（裝了 hermes 後）開瀏覽器即見配對 QR + 連線狀態 + 任務歷史，
手機掃碼就連上——不必看終端、不必裝 uv、不必下載 app。瀏覽器是每台 PC 都有的。

🔴 安全：web UI 綁 127.0.0.1（只給本機瀏覽器）；broker 的加密 TCP（手機連）是另一個 port。
   控制台不顯示任何憑證；QR 只含公開的配對資訊（pubkey/host/port），私鑰永不離機。
"""
from __future__ import annotations

import base64
import io
import json
import sqlite3
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _qr_data_uri(text: str) -> str:
    """把字串編成 QR PNG 的 data URI（給 <img src>）。無 qrcode/pillow 則回空字串。"""
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 — QR 是輔助；產不出來頁面仍可用（顯示文字配對碼）
        return ""


def _read_history(db_path: str, limit: int = 20):
    """唯讀連線讀最近 tasks + results（WAL 下與 broker 寫並行安全；每次查詢開後即關）。"""
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
        # 結果全文（截 8000 字防極端長撐爆頁面）；ref 關聯回 task.id
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
    broker = None  # 由 partial 注入

    def log_message(self, *a):  # noqa: A002 — 靜音 access log
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server 介面
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))
        elif self.path.startswith("/api/status"):
            body = json.dumps(self._status(), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif self.path.startswith("/api/open-pairing"):
            self.broker.open_pairing(300)  # GUI 一鍵重開配對視窗（換裝置 / 過期時用）
            self._send(200, "application/json; charset=utf-8", b'{"ok":true}')
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _status(self) -> dict:
        b = self.broker
        pairing_left = max(0, int(b._pairing_until - time.time()))
        paired = list(b.peers._peers.keys())
        tasks, results = _read_history(b.store.path)
        # 把結果全文（hermes 的回答）關聯到對應任務 → 控制台點任務即看完整對話
        by_ref = {r["ref"]: r for r in results}
        for t in tasks:
            r = by_ref.get(t["id"])
            t["result"] = r["text"] if r else None
            t["result_ok"] = r["ok"] if r else None
            t["id"] = t["id"][:8]  # 顯示用短 id（關聯已完成）
        return {
            "bind": f"{b.host}:{b.port}",
            "device_id": b.identity.device_id,
            "paired": [d[:8] for d in paired],
            "paired_count": len(paired),
            "pairing_left": pairing_left,
            "pair_code": b.pair_qr(),
            "qr": _qr_data_uri(b.pair_qr()),
            "tasks": tasks,
        }


def serve_web(broker, host: str = "127.0.0.1", port: int = 0):
    """啟動本地控制台 web server（daemon thread）。回 (host, port)。綁 127.0.0.1 只給本機瀏覽器。"""
    handler = partial(_Handler)
    handler.broker = broker  # type: ignore[attr-defined]
    # partial 無法設類別屬性 → 直接設在 _Handler 上（單一 broker 程序，足夠）
    _Handler.broker = broker
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
const I18N = {
 'en':{sub:'Desktop collaboration + chat handoff · Scan the QR with the Hermes mobile app to connect',
  scan:'app → "Computer Mesh" → Scan',loading:'Loading…',conn:'Connection',bind:'Bound address',
  paired:'Paired devices',window:'Pairing window',reopen:'Reopen',history:'Task history',
  hint_click:'· tap any row for the full answer',th_time:'Time',th_from:'From',th_content:'Content',
  th_status:'Status',no_tasks:'No tasks yet',st_pending:'Pending',st_running:'Running',st_done:'Done',
  no_answer:'(no answer)',running_dots:'Running…',not_paired:'Not paired',win_closed:'Closed',
  qr_fail:'QR failed (see terminal code)',paired_n:(n,l)=>n+' device(s) ('+l+')',win_open:(s)=>'Open · '+s+'s left'},
 'zh-TW':{sub:'桌面協作 + 對話接力 · 用手機 Hermes app 掃下方 QR 即可連結',
  scan:'app →「電腦協作」→ 掃描',loading:'載入中…',conn:'連線狀態',bind:'綁定位址',
  paired:'已配對裝置',window:'配對視窗',reopen:'重新開啟',history:'任務歷史',
  hint_click:'· 點任意一列看完整回答',th_time:'時間',th_from:'來源',th_content:'內容',
  th_status:'狀態',no_tasks:'尚無任務',st_pending:'待處理',st_running:'執行中',st_done:'完成',
  no_answer:'（無回答內容）',running_dots:'執行中…',not_paired:'尚未配對',win_closed:'已關閉',
  qr_fail:'QR 產生失敗（看終端文字配對碼）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'開放中 · 剩 '+s+' 秒'},
 'zh-CN':{sub:'桌面协作 + 对话接力 · 用手机 Hermes app 扫下方 QR 即可连接',
  scan:'app →「电脑协作」→ 扫描',loading:'加载中…',conn:'连接状态',bind:'绑定地址',
  paired:'已配对设备',window:'配对窗口',reopen:'重新开启',history:'任务历史',
  hint_click:'· 点任意一行看完整回答',th_time:'时间',th_from:'来源',th_content:'内容',
  th_status:'状态',no_tasks:'暂无任务',st_pending:'待处理',st_running:'执行中',st_done:'完成',
  no_answer:'（无回答内容）',running_dots:'执行中…',not_paired:'尚未配对',win_closed:'已关闭',
  qr_fail:'QR 生成失败（看终端文字配对码）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'开放中 · 剩 '+s+' 秒'},
 'ja':{sub:'デスクトップ連携 + 会話の引き継ぎ · Hermes アプリで QR をスキャンして接続',
  scan:'アプリ →「コンピュータ連携」→ スキャン',loading:'読み込み中…',conn:'接続状態',bind:'バインドアドレス',
  paired:'ペアリング済み端末',window:'ペアリング受付',reopen:'再開',history:'タスク履歴',
  hint_click:'· 行をタップで全文表示',th_time:'時刻',th_from:'送信元',th_content:'内容',
  th_status:'状態',no_tasks:'タスクなし',st_pending:'待機中',st_running:'実行中',st_done:'完了',
  no_answer:'（回答なし）',running_dots:'実行中…',not_paired:'未ペアリング',win_closed:'終了',
  qr_fail:'QR 生成失敗（端末のコード参照）',paired_n:(n,l)=>n+' 台 ('+l+')',win_open:(s)=>'受付中 · 残り '+s+' 秒'},
 'ko':{sub:'데스크톱 협업 + 대화 이어받기 · Hermes 앱으로 QR 스캔하여 연결',
  scan:'앱 →「컴퓨터 협업」→ 스캔',loading:'불러오는 중…',conn:'연결 상태',bind:'바인딩 주소',
  paired:'페어링된 기기',window:'페어링 창',reopen:'다시 열기',history:'작업 기록',
  hint_click:'· 행을 누르면 전체 답변',th_time:'시간',th_from:'출처',th_content:'내용',
  th_status:'상태',no_tasks:'작업 없음',st_pending:'대기 중',st_running:'실행 중',st_done:'완료',
  no_answer:'(답변 없음)',running_dots:'실행 중…',not_paired:'페어링 안 됨',win_closed:'닫힘',
  qr_fail:'QR 생성 실패 (터미널 코드 참조)',paired_n:(n,l)=>n+'대 ('+l+')',win_open:(s)=>'열림 · '+s+'초 남음'},
 'es':{sub:'Colaboración de escritorio + transferencia de chat · Escanea el QR con la app Hermes para conectar',
  scan:'app → "Malla de PC" → Escanear',loading:'Cargando…',conn:'Conexión',bind:'Dirección',
  paired:'Dispositivos vinculados',window:'Ventana de vinculación',reopen:'Reabrir',history:'Historial de tareas',
  hint_click:'· toca una fila para ver la respuesta',th_time:'Hora',th_from:'Origen',th_content:'Contenido',
  th_status:'Estado',no_tasks:'Sin tareas',st_pending:'Pendiente',st_running:'Ejecutando',st_done:'Hecho',
  no_answer:'(sin respuesta)',running_dots:'Ejecutando…',not_paired:'Sin vincular',win_closed:'Cerrada',
  qr_fail:'QR falló (ver código en terminal)',paired_n:(n,l)=>n+' disp. ('+l+')',win_open:(s)=>'Abierta · '+s+'s rest.'}
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
    document.getElementById('paired').textContent = s.paired_count ? T.paired_n(s.paired_count, s.paired.join(', ')) : T.not_paired;
    document.getElementById('window').textContent = s.pairing_left ? T.win_open(s.pairing_left) : T.win_closed;
    document.getElementById('qrbox').innerHTML = s.qr ? '<img src="'+s.qr+'" alt="QR">' : '<div class="empty">'+T.qr_fail+'</div>';
    const tj = JSON.stringify(s.tasks);
    if(tj !== lastTasksJson){ lastTasksJson = tj; renderTasks(s.tasks); }  // 只在變化時重繪，保住展開狀態
  }catch(e){ document.getElementById('dot').className='dot off'; }
}
tick(); setInterval(tick, 3000);
</script>
</body>
</html>"""
