"""
desktop_export — 桌面側接力匯出（M1，桌面 → 手機）。

純標準庫。職責：
1. 用 sqlite3.backup() 對 live state.db 取「一致快照」（不碰 WAL、不被寫入干擾），
   再從快照抽單一 session 連通鏈 → bundle（複用 handoff_core.export_session）。
2. 收 memories/*.md 快照併入 bundle（手機端做 append-union）。
3. 單一 owner 鎖：匯出本身**唯讀**；待傳輸確認後才呼叫 mark_handed_off() 把來源 session
   標 handoff_state='completed' + archived=1（之後桌面不再續寫該 session）。失敗用
   release_handoff() 解鎖可重試。

🔴 機密永不入 bundle：本模組只讀 state.db + memories/，絕不碰 auth.json/.env。

之後 #5b 會把這些函式包成 hermes plugin（register(ctx) 背景服務）；M1 先用 CLI 驗：
    python3 desktop_export.py --home ~/.hermes --session <uuid> --out bundle.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handoff_core as hc  # noqa: E402


def _snapshot(state_db: str) -> str:
    """用 sqlite3.backup() 取 state.db 的一致快照到暫存檔，回傳快照路徑。

    比照上游 backup.py：唯讀開啟來源 + backup() API → 即使 hermes 正在寫也安全，
    且不複製 -wal/-shm。呼叫端負責刪除回傳的暫存檔。
    """
    fd, snap = tempfile.mkstemp(prefix="hermes-handoff-snap-", suffix=".db")
    os.close(fd)
    src = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return snap


def _read_memory(hermes_home: str) -> dict:
    """讀 memories/*.md 成 {filename: content}。無目錄則回空。

    安全（review P2）：**跳過 symlink / 非 regular file**，且每個檔的 realpath 必須落在
    memories/ 內 —— 防 `memories/SECRET.md -> ../.env` 這類 symlink 把機密讀進 bundle。
    """
    mem_dir = os.path.join(hermes_home, "memories")
    out = {}
    if not os.path.isdir(mem_dir):
        return out
    real_mem = os.path.realpath(mem_dir)
    for path in sorted(glob.glob(os.path.join(mem_dir, "*.md"))):
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue  # 跳過 symlink 與非一般檔
            rp = os.path.realpath(path)
            if os.path.commonpath([rp, real_mem]) != real_mem:
                continue  # realpath 逃出 memories/ → 拒讀
            with open(path, encoding="utf-8") as f:
                out[os.path.basename(path)] = f.read()
        except (OSError, ValueError):
            pass
    return out


def export_for_handoff(hermes_home: str, session_id: str,
                       source_device: str = "", include_memory: bool = True) -> dict | None:
    """產生接力 bundle（唯讀，不動 live db）。回傳 None 表示找不到 session。"""
    state_db = os.path.join(hermes_home, "state.db")
    if not os.path.isfile(state_db):
        raise FileNotFoundError(f"state.db not found: {state_db}")
    snap = _snapshot(state_db)
    try:
        # 只支援 0.16.0+ 桌面：schema 不符就 fail-fast（不產 bundle）
        guard = sqlite3.connect(snap)
        try:
            _require_handoff_schema(guard)
        finally:
            guard.close()
        bundle = hc.export_session(
            snap, session_id, source_device=source_device, exported_at=time.time())
        if bundle is None:
            return None
        if include_memory:
            bundle["memory"] = _read_memory(hermes_home)
        return bundle
    finally:
        try:
            os.remove(snap)
        except OSError:
            pass


# ── 支援版本守衛（只支援 0.16.0+ 桌面，schema_version >= 15）──────────────
# 政策：只支援 0.16.0+ 桌面 → 缺 handoff 欄位就 fail-fast 報清楚的錯（不再 sidecar 軟鎖）。
_REQUIRED_LOCK_COLS = ("handoff_state", "handoff_platform", "archived")
_MIN_SCHEMA_VERSION = 15  # hermes 0.16.0


class UnsupportedDesktopError(RuntimeError):
    """桌面 hermes 版本過舊（< 0.16.0），不支援接力。"""


def _require_handoff_schema(conn: sqlite3.Connection) -> None:
    """handoff「欄位能力」守衛：sessions 須具 handoff_state/handoff_platform/archived。

    這三欄是 0.16.0(schema v15) 引入的，故缺欄位 ≈ 桌面 < 0.16.0。實際判定看「欄位是否
    存在」而非嚴格 schema_version（即使無 schema_version table，只要三欄齊備即可接力）；
    schema_version 僅用於錯誤訊息輔助說明。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    missing = [c for c in _REQUIRED_LOCK_COLS if c not in have]
    if missing:
        ver = None
        try:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            ver = row[0] if row else None
        except sqlite3.Error:
            pass
        raise UnsupportedDesktopError(
            f"桌面 hermes-agent 缺接力所需欄位 {missing}（schema_version={ver}，"
            f"需 0.16.0+ / schema v{_MIN_SCHEMA_VERSION}）。請更新桌面版。")


# ── 單一 owner 鎖（寫 live db；傳輸確認後才呼叫）──────────────────────────
# 對 bundle 整條鏈的「所有」session 一起標記/解鎖（export 帶整條鏈，只標一個會漏）。
# 假設 0.16.0+ schema（欄位必存在，由守衛保證）。

def _chain_for(conn: sqlite3.Connection, session_id: str) -> list:
    return hc.resolve_chain(conn, session_id) or [session_id]


def _set_lock(state_db: str, session_id: str, *, state: str, archived: int,
              platform: str | None) -> None:
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row  # resolve_chain 需要
    try:
        _require_handoff_schema(conn)
        with conn:
            for sid in _chain_for(conn, session_id):
                if platform is not None:
                    # 鎖定：記下目前 handoff 目標平台
                    conn.execute(
                        "UPDATE sessions SET handoff_state=?, handoff_platform=?, "
                        "archived=? WHERE id=?", (state, platform, archived, sid))
                else:
                    # 解鎖（release）：清掉 handoff_platform（語義＝目前無鎖定平台）
                    conn.execute(
                        "UPDATE sessions SET handoff_state=?, handoff_platform=NULL, "
                        "archived=? WHERE id=?", (state, archived, sid))
    finally:
        conn.close()


def mark_handed_off(state_db: str, session_id: str, platform: str = "android") -> None:
    """傳輸成功後：整條鏈標已交棒 + archived（單一 owner，桌面不再續寫）。"""
    _set_lock(state_db, session_id, state="completed", archived=1, platform=platform)


def release_handoff(state_db: str, session_id: str) -> None:
    """傳輸失敗：整條鏈解鎖（可重試）。"""
    _set_lock(state_db, session_id, state="failed", archived=0, platform=None)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hermes 接力：桌面側匯出單一對話 bundle")
    ap.add_argument("--home", default=os.path.expanduser("~/.hermes"),
                    help="HERMES_HOME（預設 ~/.hermes）")
    ap.add_argument("--session", required=True, help="要交棒的 session id (UUID)")
    ap.add_argument("--out", required=True, help="輸出 bundle.json 路徑")
    ap.add_argument("--device", default="desktop", help="來源 device id")
    ap.add_argument("--no-memory", action="store_true", help="不含 memory")
    ap.add_argument("--mark", action="store_true",
                    help="匯出後立即標記已交棒（測試用；正式應在傳輸確認後）")
    a = ap.parse_args(argv)

    bundle = export_for_handoff(a.home, a.session, source_device=a.device,
                                include_memory=not a.no_memory)
    if bundle is None:
        print(f"找不到 session: {a.session}", file=sys.stderr)
        return 1
    # 安全（review P2）：bundle 含對話 + memory → 不可在 umask 022 下變 0644 同機可讀。
    # 寫到同目錄的 0600 暫存檔（mkstemp 預設 0600）再原子 os.replace。
    out_dir = os.path.dirname(os.path.abspath(a.out)) or "."
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False)
        os.replace(tmp, a.out)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    mem = len(bundle.get("memory", {}))
    print(f"匯出 OK → {a.out}：鏈 {len(bundle['session_ids'])} session、"
          f"{len(bundle['messages'])} 訊息、{mem} memory 檔")
    if a.mark:
        mark_handed_off(os.path.join(a.home, "state.db"), a.session)
        print(f"已標記 {a.session} 為已交棒（archived）")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
