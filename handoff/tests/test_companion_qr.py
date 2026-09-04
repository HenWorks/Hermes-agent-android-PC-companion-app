"""
companion 主控台的 QR 回歸測試。

## 為什麼需要這一檔

配對 QR 曾經**在 Windows 上完全不顯示**，而且沒有任何測試發現得了。事故鏈：

  1. `companion_web._qr_data_uri()` 用 Python 的 `qrcode` + `pillow` 畫 PNG
  2. 那兩個套件只宣告在 `handoff/requirements.txt`
  3. 唯一會安裝它們的是 `mesh-start.sh`——**bash 腳本，而且用 `$VENV/bin/pip`**
     這種 POSIX venv 路徑（Windows 是 Scripts\\）
  4. Windows 上腳本跑不了 ⇒ `import qrcode` 失敗 ⇒ 舊版 `except Exception: return ""`
     **靜默**回空字串 ⇒ 頁面只剩一句「QR failed」
  5. Mac 一直正常（mesh-start.sh 在那裡跑得起來），所以我們自己從來沒踩到

手機端是用相機掃這張 QR 的（Android zxing）。沒有 QR，使用者唯一的路是把一串
140+ 字元的 JSON 從 PC 手抄到手機——對「不懂終端的使用者」等於沒有路。

修法是把 QR 搬到**瀏覽器端**產生（`companion_web` 頁面裡的 `qrMatrix`），Python 端零依賴。

## 這些測試釘的不變量

`test_status_has_no_png_qr_field`   後端不再回傳 PNG（回傳了就代表依賴又長回來）
`test_page_embeds_the_encoder`      頁面真的帶著編碼器與繪製函式
`test_console_works_without_qrcode` **把 qrcode/pillow 從 import 系統拿掉**，主控台照樣完整
`test_js_encoder_matches_python`    用 node 實跑頁面裡的 JS，與 python-qrcode 逐格比對

最後一條需要 node 與 qrcode 才跑得動，缺了就 skip——但**前三條不需要任何選用依賴**，
它們才是擋住這個 bug 復發的主力。
"""
import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _start_console():
    """起一個真的 broker + web console，回傳 (base_url, cfg_dir)。"""
    import companion_web as cw
    import handoff_server as hs
    import mesh_broker as mb
    import pairing as pr

    cfg = tempfile.mkdtemp(prefix="qr-test-")
    identity = pr.load_or_create_identity(os.path.join(cfg, "identity.json"))
    peers = hs.PeerStore(os.path.join(cfg, "peers.json"))
    store = mb.MeshStore(os.path.join(cfg, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           home=cfg, host="127.0.0.1", port=8765)
    host, port = cw.serve_web(broker, "127.0.0.1", 0)
    time.sleep(0.3)
    return f"http://{host}:{port}", cfg


def _get(url, path=""):
    return urllib.request.urlopen(url + path, timeout=5).read().decode()


def test_status_has_no_png_qr_field():
    """後端不得回傳 PNG data URI——回傳了就代表 Python 端的 QR 依賴又長回來了。"""
    url, cfg = _start_console()
    try:
        st = json.loads(_get(url, "/api/status"))
        assert "qr" not in st, (
            "/api/status 又出現 'qr' 欄位。那代表 QR 回到 Python 端產生，"
            "而那個依賴在 Windows 上裝不起來——正是原本的缺陷。"
        )
        assert st.get("pair_code"), "沒有 pair_code 的話前端無從產生 QR"
        assert len(st["pair_code"]) > 50, "pair_code 看起來不像完整的配對 JSON"
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_page_embeds_the_encoder():
    """頁面必須自帶編碼器與繪製函式，而且不能靠外部 CDN（區網/離線環境沒有網路）。"""
    url, cfg = _start_console()
    try:
        page = _get(url)
        assert "function qrMatrix(" in page, "頁面沒有帶 QR 編碼器"
        assert "function drawQr(" in page, "頁面沒有帶繪製函式"
        assert "s.qr" not in page, "前端還在讀後端的 PNG 欄位"
        # 離線可用是硬需求：companion 常跑在沒有對外網路的區網機器上
        for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.", "googleapis.com"):
            assert cdn not in page, f"頁面引用了外部資源 {cdn}——離線/區網環境會拿不到"
    finally:
        shutil.rmtree(cfg, ignore_errors=True)


def test_console_works_without_qrcode_or_pillow():
    """
    🚨 這是本檔的核心：把 `qrcode` / `pillow` 從 import 系統**拿掉**，
    主控台仍要給出完整的配對頁面。

    這正是 Windows 使用者的實際處境。上一版在這個情況下會靜默降級成「QR failed」，
    而**沒有任何測試會紅**。
    """
    blocked = {"qrcode", "PIL", "PIL.Image", "pillow"}
    real_import = builtins.__import__

    def guard(name, *a, **kw):
        if name.split(".")[0] in {"qrcode", "PIL", "pillow"}:
            raise ImportError(f"blocked by test: {name}")
        return real_import(name, *a, **kw)

    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.split(".")[0] in blocked}
    builtins.__import__ = guard
    try:
        url, cfg = _start_console()
        try:
            st = json.loads(_get(url, "/api/status"))
            assert st.get("pair_code"), "缺 qrcode 時連配對碼都沒有了"
            page = _get(url)
            assert "function qrMatrix(" in page, (
                "缺 qrcode/pillow 時頁面就沒有 QR 產生能力了——"
                "那就是 Windows 使用者看到的狀態，也是這個 bug 的本體"
            )
        finally:
            shutil.rmtree(cfg, ignore_errors=True)
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)


def test_js_encoder_matches_python():
    """
    用 node 實跑**頁面實際送出的那份 JS**，與 python-qrcode 在同一個遮罩下逐格比對。

    比對「同遮罩」而不是「預設遮罩」是刻意的：遮罩選擇是品質啟發式，8 個都合法，
    解碼器從 format info 讀得出用了哪個。逐格相同 ⇒ 我們產生的是合法可解碼的 QR。
    """
    if not shutil.which("node"):
        pytest.skip("需要 node 才能實跑頁面的 JS")
    try:
        import qrcode
    except ImportError:
        pytest.skip("需要 python-qrcode 當比對基準")

    url, cfg = _start_console()
    tmp = tempfile.mkdtemp(prefix="qr-js-")
    try:
        st = json.loads(_get(url, "/api/status"))
        code = st["pair_code"]
        page = _get(url)
        js = page[page.index("<script>") + len("<script>"): page.index("</script>")]

        runner = os.path.join(tmp, "run.js")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "const el=()=>({innerHTML:'',style:{},appendChild(){},dataset:{}});\n"
                "global.document={getElementById:el,querySelectorAll:()=>[],"
                "createElement:()=>({style:{},getContext:()=>({fillRect(){}})}),addEventListener(){}};\n"
                "global.window={location:{search:''},navigator:{language:'en'},addEventListener(){}};\n"
                "global.navigator=global.window.navigator;\n"
                "global.fetch=async()=>({json:async()=>({})});\n"
                "global.setInterval=()=>0;\n"
                + js +
                "\nconst r=qrMatrix(process.argv[2]);\n"
                "console.log(JSON.stringify({v:r.version,mask:r.mask,"
                "rows:r.modules.map(x=>x.map(v=>v?'1':'0').join(''))}));\n"
            )
        out = subprocess.run([shutil.which("node"), runner, code],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f"node 執行頁面 JS 失敗：{out.stderr[:400]}"
        mine = json.loads(out.stdout)

        q = qrcode.QRCode(version=mine["v"], error_correction=qrcode.constants.ERROR_CORRECT_M,
                          border=0, box_size=1, mask_pattern=mine["mask"])
        q.add_data(code)
        q.make(fit=False)
        ref = ["".join("1" if c else "0" for c in row) for row in q.get_matrix()]
        assert ref == mine["rows"], (
            f"頁面產生的 QR 與 python-qrcode 在 mask={mine['mask']} 下不同 ⇒ 掃不出來。"
            f"（v{mine['v']}，payload {len(code)} 字元）"
        )
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
