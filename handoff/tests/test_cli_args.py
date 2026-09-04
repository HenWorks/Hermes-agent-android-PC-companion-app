"""
CLI 參數必須在**兩條啟動路徑**上都生效。

## 這一檔為什麼存在（issue #1）

使用者回報：「在 Windows 跑 `hermes-companion.exe --session <session_id>`，
只顯示配對 QR，不是 handoff QR。」

根因不是他用錯，是**打包版根本不吃參數**：

    companion.py:
        try:    return companion_app.main()   # 托盤路徑 —— 完全不看 argv
        except: return mesh_broker.main()     # --session 的 argparse 只在這裡

`companion_app.main()` 直接 `mb.serve()` 無參數。托盤只要起得來——也就是**所有桌面
GUI 環境**——`--session` / `--host` / `--port` / `--home` 就全部被靜默吞掉。
只有 headless（無 display）才會退到 CLI 那條，參數才生效。

`--host` 一起被吞這件事同樣有代價：想用 VPN / Tailscale 位址配對的人
（issue #1 的第二個問題）也走不通。

## 這些測試釘的不變量

`test_parser_is_defined_once`      參數只有一份定義（兩份 argparse 必然只改到一邊）
`test_tray_path_accepts_argv`      托盤入口吃 argv，且真的轉發給 serve() / serve_web()
`test_entry_point_forwards_argv`   companion.py 兩條路都傳 argv
`test_session_switches_the_qr`     指定 --session 時主控台給的是 handoff QR

最後一條是**行為測試**：真的起一個 web console，比對它送出的 pair_code
是不是 `handoff_qr(session)`。前三條是結構契約，擋的是「接線被拆掉」。
"""
import inspect
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import companion_app  # noqa: E402
import companion_web as cw  # noqa: E402
import handoff_server as hs  # noqa: E402
import mesh_broker as mb  # noqa: E402
import pairing as pr  # noqa: E402

# companion.py 在 repo **根**，tests/ 往上兩層才到
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_parser_is_defined_once():
    """參數只有一份定義。兩邊各寫一份 argparse 的話，加旗標時必然只改到一邊。"""
    assert hasattr(mb, "build_arg_parser"), "mesh_broker 沒有共用的 parser 建構函式"
    opts = {a.option_strings[0] for a in mb.build_arg_parser()._actions if a.option_strings}
    for flag in ("--home", "--host", "--port", "--session"):
        assert flag in opts, f"{flag} 不在共用 parser 裡"
    app_src = inspect.getsource(companion_app)
    assert "add_argument" not in app_src, (
        "companion_app 自己又定義了一份參數 —— 那就是第二份真相，"
        "下次加旗標只會改到其中一邊"
    )
    print("✓ test_parser_is_defined_once")


def test_tray_path_accepts_argv():
    """托盤入口要吃 argv，而且要把值**真的**用在 serve() 與 serve_web() 上。"""
    sig = inspect.signature(companion_app.main)
    assert "argv" in sig.parameters, "companion_app.main 不吃 argv ⇒ 打包版會吞掉所有參數"
    src = inspect.getsource(companion_app.main)
    assert "mb.parse_args(argv)" in src, "沒有解析 argv"
    assert "mb.serve(a.home, host=a.host, port=a.port)" in src, (
        "解析了卻沒轉發給 serve() ⇒ --host/--port/--home 仍然無效"
    )
    assert "session=a.session" in src, "--session 沒有轉發給 serve_web() ⇒ QR 不會切換"
    print("✓ test_tray_path_accepts_argv")


def test_entry_point_forwards_argv():
    """
    companion.py 的**兩條**路都要傳 argv —— 只修一條等於只修一半的使用者。

    ⚠️ `companion.py` 是 PyInstaller 的打包進入點，**只存在公開 companion repo**，
    monorepo 的 `handoff/` 沒有它（那裡是這個模組的上游來源，不負責打包）。
    所以檔案不在時 skip 而不是紅——但**檔案在的話一律嚴格檢查**，不能因為
    「可能不存在」就整條放行。
    """
    entry = os.path.join(_REPO, "companion.py")
    if not os.path.exists(entry):
        import pytest
        pytest.skip("companion.py 只存在打包用的公開 repo；此處為上游來源樹")
    with open(entry, encoding="utf-8") as f:
        src = f.read()
    assert "sys.argv[1:]" in src, "進入點沒有取 argv"
    assert "companion_app.main(argv)" in src, "托盤路徑沒收到 argv（GUI 桌面走這條）"
    assert "mesh_broker.main(argv)" in src, "CLI 退場路徑沒收到 argv"
    print("✓ test_entry_point_forwards_argv")


def test_session_switches_the_qr():
    """
    行為測試：指定 session 時，主控台給的必須是 **handoff QR**（掃了會配對**並**收下那段對話），
    而不是純配對 QR。這是使用者實際回報的症狀。
    """
    cfg = tempfile.mkdtemp(prefix="cli-args-")
    identity = pr.load_or_create_identity(os.path.join(cfg, "identity.json"))
    peers = hs.PeerStore(os.path.join(cfg, "peers.json"))
    store = mb.MeshStore(os.path.join(cfg, "queue.db"))
    broker = mb.MeshBroker(identity=identity, peers=peers, store=store,
                           home=cfg, host="127.0.0.1", port=8765)

    host, port = cw.serve_web(broker, "127.0.0.1", 0, session="sess-abc123")
    time.sleep(0.3)
    got = json.loads(urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=5).read())
    assert got["pair_code"] == broker.handoff_qr("sess-abc123"), (
        "指定了 session，主控台卻仍給純配對 QR —— 這正是 issue #1 的症狀"
    )
    assert got["pair_code"] != broker.pair_qr(), "handoff QR 不該等於純配對 QR"
    print("✓ test_session_switches_the_qr")


if __name__ == "__main__":
    test_parser_is_defined_once()
    test_tray_path_accepts_argv()
    test_entry_point_forwards_argv()
    test_session_switches_the_qr()
    print("all CLI arg tests passed")
