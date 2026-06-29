"""
handoff_core — hermes-agent 對話「接力」的合併核心（M1）。

零外部相依（只用標準庫 sqlite3 / json / hashlib），讓桌面端與測試共用同一份
經過驗證的邏輯；手機端（Kotlin）依此規格實作對等版本。

設計依據（見 android/docs/plan-desktop-mobile-sync.md）：
- sessions.id 是 TEXT(UUID) → 全域唯一 → 可跨裝置 upsert
- messages.id 是 INTEGER AUTOINCREMENT → 本機、非全域 → 匯入時丟棄來源 id、
  靠自然鍵 (session_id, timestamp, role, content) 去重做 append-union
- 長對話會被壓縮切成 parent_session_id 鏈 → 搬一則要帶整條連通鏈
- FTS5 由 DB trigger 自動維護 → 正常 INSERT 即可，匯入端不需手動碰索引
- 機密（auth.json/.env）永不在 payload 內（本模組只碰 state.db 與 memories/*.md）

bundle 格式（單一 session 連通鏈）：
{
  "schema": 1,
  "source_device": "<id>",
  "root_session_id": "<uuid>",
  "session_ids": ["<uuid>", ...],          # 連通鏈全部
  "sessions": [ {col: val, ...}, ... ],     # 動態欄位（依來源 schema）
  "messages": [ {col: val, ...}, ... ],     # 已去除來源 id
  "exported_at": <float>,
}
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Set

SCHEMA = 1
# messages 自然鍵欄位（跨裝置去重用；不含本機自增 id）
_MSG_NATURAL_KEY = ("session_id", "timestamp", "role", "content")


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """動態讀取表的欄位名（適配不同 schema 版本，不硬編 30 個欄位）。"""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── 連通鏈解析 ────────────────────────────────────────────────────────────

def resolve_chain(conn: sqlite3.Connection, session_id: str) -> List[str]:
    """回傳與 session_id 連通的整條 parent/child 鏈 session id（去重、含自己）。

    壓縮把長對話切成 parent_session_id 串接的鏈；使用者眼中的「一則對話」可能是多
    個 session row。先沿 parent 往上找到 root，再從 root 收集所有後代。
    """
    # 1. 往上找 root
    root = session_id
    seen_up: Set[str] = set()
    while True:
        if root in seen_up:
            break  # 防環
        seen_up.add(root)
        row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE id = ?", (root,)
        ).fetchone()
        if row is None:
            # session 不存在；若是起點就回空，否則停在已知最上層
            if root == session_id:
                return []
            break
        parent = row["parent_session_id"]
        if not parent:
            break
        root = parent

    # 2. 從 root 廣度收集所有後代
    chain: List[str] = []
    seen: Set[str] = set()
    stack = [root]
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        chain.append(sid)
        children = conn.execute(
            "SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)
        ).fetchall()
        stack.extend(c["id"] for c in children)
    return chain


# ── 匯出 ──────────────────────────────────────────────────────────────────

def export_session(db_path: str, session_id: str, source_device: str = "",
                   exported_at: float = 0.0) -> Optional[Dict[str, Any]]:
    """從 state.db 抽出一則對話（連通鏈）的 sessions + messages，組成 bundle。

    回傳 None 表示找不到該 session。messages 已去除來源自增 id。
    """
    conn = _connect(db_path)
    try:
        chain = resolve_chain(conn, session_id)
        if not chain:
            return None
        scols = _columns(conn, "sessions")
        mcols = [c for c in _columns(conn, "messages") if c != "id"]  # 丟棄來源 id

        placeholders = ",".join("?" * len(chain))
        sessions = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(scols)} FROM sessions WHERE id IN ({placeholders})",
                chain,
            ).fetchall()
        ]
        messages = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(mcols)} FROM messages "
                f"WHERE session_id IN ({placeholders}) ORDER BY session_id, timestamp",
                chain,
            ).fetchall()
        ]
        return {
            "schema": SCHEMA,
            "source_device": source_device,
            "root_session_id": chain[0],
            "session_ids": chain,
            "sessions": sessions,
            "messages": messages,
            "exported_at": exported_at,
        }
    finally:
        conn.close()


def export_all(db_path: str, source_device: str = "",
               exported_at: float = 0.0) -> Dict[str, Any]:
    """匯出整個 state.db 的「所有」session + message 成單一 bundle（#22 反向同步）。

    用於手機→桌面的最終一致同步：一次把本機全部對話打包回傳，桌面以 import_bundle
    （by-id upsert + 自然鍵去重）冪等合併。與 export_session 同 bundle 格式，差別在不解
    連通鏈、而是全表 dump。空 db 回傳 sessions/messages 皆空的合法 bundle（非 None）。
    messages 已丟棄來源自增 id（讓對端 AUTOINCREMENT 配新 id）。
    """
    conn = _connect(db_path)
    try:
        scols = _columns(conn, "sessions")
        mcols = [c for c in _columns(conn, "messages") if c != "id"]
        sessions = [
            dict(r) for r in conn.execute(f"SELECT {','.join(scols)} FROM sessions").fetchall()
        ]
        messages = [
            dict(r) for r in conn.execute(
                f"SELECT {','.join(mcols)} FROM messages ORDER BY session_id, timestamp"
            ).fetchall()
        ]
        return {
            "schema": SCHEMA,
            "source_device": source_device,
            "session_ids": [s["id"] for s in sessions],
            "sessions": sessions,
            "messages": messages,
            "exported_at": exported_at,
        }
    finally:
        conn.close()


# ── 匯入合併 ──────────────────────────────────────────────────────────────

def _natural_key(m: Dict[str, Any]) -> str:
    raw = "\x1f".join(
        "" if m.get(k) is None else str(m.get(k)) for k in _MSG_NATURAL_KEY
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_bundle(db_path: str, bundle: Dict[str, Any]) -> Dict[str, int]:
    """把 bundle 合併進本機 state.db。回傳統計。

    - sessions：by id upsert（INSERT OR IGNORE，不覆蓋本機既有 → 保留本機端 metadata）
    - messages：自然鍵去重後 INSERT（丟棄來源 id、讓本機 AUTOINCREMENT 配新 id）
    - FTS5 由 trigger 自動維護
    冪等：同一 bundle 重複匯入不會產生重複 message。
    """
    if bundle.get("schema") != SCHEMA:
        raise ValueError(f"unsupported bundle schema: {bundle.get('schema')}")

    conn = _connect(db_path)
    stats = {"sessions_added": 0, "sessions_existing": 0,
             "messages_added": 0, "messages_skipped": 0}
    try:
        local_scols = set(_columns(conn, "sessions"))
        local_mcols = set(_columns(conn, "messages"))

        with conn:  # 單一交易（全有或全無）
            # 1. sessions：upsert（不覆蓋既有）
            for s in bundle.get("sessions", []):
                cols = [c for c in s.keys() if c in local_scols]
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (s["id"],)
                ).fetchone()
                if exists:
                    stats["sessions_existing"] += 1
                    continue
                conn.execute(
                    f"INSERT OR IGNORE INTO sessions ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [s[c] for c in cols],
                )
                stats["sessions_added"] += 1

            # 2. messages：自然鍵去重 → 只插新訊息
            #    先載入每個目標 session 既有訊息的自然鍵集合
            target_sids = {m["session_id"] for m in bundle.get("messages", [])}
            existing_keys: Set[str] = set()
            for sid in target_sids:
                for r in conn.execute(
                    "SELECT session_id, timestamp, role, content "
                    "FROM messages WHERE session_id = ?", (sid,)
                ).fetchall():
                    existing_keys.add(_natural_key(dict(r)))

            for m in bundle.get("messages", []):
                key = _natural_key(m)
                if key in existing_keys:
                    stats["messages_skipped"] += 1
                    continue
                cols = [c for c in m.keys() if c in local_mcols and c != "id"]
                conn.execute(
                    f"INSERT INTO messages ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [m[c] for c in cols],
                )
                existing_keys.add(key)  # 防 bundle 內自身重複
                stats["messages_added"] += 1
        return stats
    finally:
        conn.close()


# ── memory append-union（M1：安全、不覆蓋、衝突保留）────────────────────────

def merge_memory_text(local: str, incoming: str) -> str:
    """把 incoming 的記憶條目併入 local（行級 union），保留 local 既有、只加新行。

    M1 安全策略：不刪、不覆蓋；incoming 有而 local 沒有的「非空行」附加到末尾，
    標註來源區塊。真正的 3-way（含刪除傳播）留待 M3（屆時用 .sync-base 當共同祖先）。
    回傳合併後文字；若無新增則原樣回傳。
    """
    local_lines = local.splitlines()
    local_set = {ln.strip() for ln in local_lines if ln.strip()}
    new_lines = [
        ln for ln in incoming.splitlines()
        if ln.strip() and ln.strip() not in local_set
    ]
    if not new_lines:
        return local
    sep = "" if local.endswith("\n") or local == "" else "\n"
    block = "\n<!-- merged from handoff -->\n" + "\n".join(new_lines) + "\n"
    return local + sep + block


def _atomic_write_text(path: str, text: str) -> None:
    """原子寫文字檔（tmp + os.replace），避免半寫狀態。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mem-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def import_memory(hermes_home: str, memory: Dict[str, str]) -> Dict[str, int]:
    """把 bundle 的 memory（{檔名: 內容}）append-union 進本機 memories/*.md。

    安全（檔名來自 bundle，不可信）：只接受「純檔名」的 .md（拒路徑穿越 `../`、子目錄）；
    既有檔若是 symlink 則跳過（防寫穿到 .env 等）；寫入前確認目錄 realpath 仍在 memories/ 內。
    不刪不覆蓋（沿用 merge_memory_text 的 M1 策略）。回傳統計。
    """
    stats = {"mem_added": 0, "mem_merged": 0, "mem_unchanged": 0}
    if not memory:
        return stats
    mem_dir = os.path.join(hermes_home, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    real_mem = os.path.realpath(mem_dir)
    for name, incoming in memory.items():
        if not isinstance(name, str) or not name.endswith(".md"):
            continue
        if name in (".", "..") or os.path.basename(name) != name:
            continue  # 拒路徑穿越/子目錄
        path = os.path.join(mem_dir, name)
        if os.path.islink(path):
            continue  # 既有 symlink → 不安全，跳過
        if os.path.commonpath([os.path.realpath(os.path.dirname(path)), real_mem]) != real_mem:
            continue
        existed = os.path.isfile(path)
        local = ""
        if existed:
            try:
                with open(path, encoding="utf-8") as f:
                    local = f.read()
            except OSError:
                continue
        merged = merge_memory_text(local, incoming or "")
        if existed and merged == local:
            stats["mem_unchanged"] += 1
            continue
        _atomic_write_text(path, merged)
        stats["mem_merged" if existed else "mem_added"] += 1
    return stats


def import_all(hermes_home: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    """手機端單一匯入入口：state.db（sessions/messages）+ memories/ 一起合併。回傳合併統計。"""
    db = os.path.join(hermes_home, "state.db")
    stats: Dict[str, Any] = dict(import_bundle(db, bundle))
    stats.update(import_memory(hermes_home, bundle.get("memory") or {}))
    return stats
