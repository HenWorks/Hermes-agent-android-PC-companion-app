"""
handoff_server — 桌面側接力傳輸（#3 發現 + #4 加密傳輸）。

桌面 = sender/server：mDNS 廣告 `_hermes-handoff._tcp` + TCP 監聽，把指定 session
的 bundle 以 NaCl Box 加密後傳給「已配對」的手機（client）。手機端 Kotlin 依此協定實作。

互相認證（雙向，靠 Box）：
- server 只接受 peers 清單內、且 pubkey 相符的 client（防陌生裝置）。
- client 的請求用 Box(client_sk→server_pk) 加密 → server 能解 = 證明 client 真的持有
  該 pubkey 對應私鑰（認證 client）。
- bundle 用 Box(server_sk→client_pk) 加密 → client 能解 = 證明來自登記的 server（認證
  server）。竄改/錯金鑰必失敗。

線路框架：4-byte big-endian 長度前綴 + bytes。握手與 payload 皆此框架。
🔴 機密永不傳；私鑰永不離開本機。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import nacl.public

import pairing as pr

SERVICE_TYPE = "_hermes-handoff._tcp.local."
PROTO = 1


# ── 配對方儲存（device_id → pubkey b64）──────────────────────────────────────

class PeerStore:
    def __init__(self, path: str):
        self.path = path
        self._peers: dict[str, str] = {}
        self._load()

    def _load(self):
        import os
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._peers = json.load(f)
            except (OSError, ValueError):
                self._peers = {}

    def _save(self):
        import os
        import tempfile
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".peers-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._peers, f)
        os.replace(tmp, self.path)

    def add(self, device_id: str, pubkey: bytes):
        self._peers[device_id] = pr._b64e(pubkey)
        self._save()

    def pubkey(self, device_id: str) -> Optional[bytes]:
        v = self._peers.get(device_id)
        return pr._b64d(v) if v else None

    def is_paired(self, device_id: str, pubkey: bytes) -> bool:
        known = self.pubkey(device_id)
        return known is not None and known == pubkey


# ── 線路框架 ────────────────────────────────────────────────────────────────

def _send_frame(sock: socket.socket, data: bytes):
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _recv_frame(sock: socket.socket, max_len: int = 64 * 1024 * 1024) -> bytes:
    (n,) = struct.unpack(">I", _recv_exact(sock, 4))
    if n > max_len:
        raise ValueError(f"frame too large: {n}")
    return _recv_exact(sock, n)


# ── Server（桌面）────────────────────────────────────────────────────────────

@dataclass
class HandoffServer:
    identity: pr.DeviceIdentity
    peers: PeerStore
    # export_fn(session_id) -> bundle dict | None；通常綁 desktop_export.export_for_handoff
    export_fn: Callable[[str], Optional[dict]]
    host: str = ""  # 空 = start 時綁本機 LAN IP（_local_ip）；絕不綁 0.0.0.0（資安守則）
    port: int = 0  # 0 = 自動選

    _sock: Optional[socket.socket] = None
    _thread: Optional[threading.Thread] = None
    _running: bool = False
    _zc = None  # zeroconf instance

    def _handle(self, conn: socket.socket):
        try:
            # 握手：client 明文送 {did, pk}
            hello = json.loads(_recv_frame(conn).decode("utf-8"))
            cdid = hello["did"]
            cpk = pr._b64d(hello["pk"])
            if not self.peers.is_paired(cdid, cpk):
                _send_frame(conn, json.dumps({"ok": False, "err": "not paired"}).encode())
                return
            _send_frame(conn, json.dumps({"ok": True, "proto": PROTO}).encode())

            # client 送 Box(client_sk→server_pk) 加密的請求 → server 解 = 認證 client
            req_blob = _recv_frame(conn)
            req = json.loads(pr.box_decrypt(self.identity.private_key, cpk, req_blob))
            session_id = req["session_id"]

            bundle = self.export_fn(session_id)
            if bundle is None:
                _send_frame(conn, json.dumps({"ok": False, "err": "session not found"}).encode())
                return
            # bundle 用 Box(server_sk→client_pk) 加密 → 只有該 client 能解 + 驗來源
            payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
            _send_frame(conn, json.dumps({"ok": True}).encode())
            _send_frame(conn, pr.box_encrypt(self.identity.private_key, cpk, payload))
        except Exception as e:  # noqa: BLE001 — 單一連線錯誤不該拖垮 server
            try:
                _send_frame(conn, json.dumps({"ok": False, "err": str(e)}).encode())
            except OSError:
                pass
        finally:
            conn.close()

    def start(self, advertise: bool = True) -> int:
        bind_host = self.host or _local_ip()  # 綁本機 LAN IP；絕不 0.0.0.0（資安守則）
        self.host = bind_host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind_host, self.port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._running = True

        def loop():
            while self._running:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        if advertise:
            self._advertise()
        return self.port

    def _advertise(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return
        self._zc = Zeroconf()
        name = f"hermes-{self.identity.device_id}.{SERVICE_TYPE}"
        info = ServiceInfo(
            SERVICE_TYPE, name,
            addresses=[socket.inet_aton(_local_ip())],
            port=self.port,
            properties={"did": self.identity.device_id, "ver": str(PROTO)},
        )
        self._zc.register_service(info)
        self._zc_info = info

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


# Tailscale / overlay 共享位址空間（RFC 6598 CGNAT）。掃 QR 的手機若只在同一 Wi-Fi、
# 沒裝 Tailscale，連不到桌面的 100.x 位址 → QR host 必須避開這段，優先給真正 LAN 私網。
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_lan_ipv4(ip: str) -> bool:
    """是否為手機在同網段可直連的 IPv4 私網位址（排除 CGNAT/Tailscale 與 loopback）。"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (a.version == 4 and a.is_private
            and a not in _CGNAT_NET and not a.is_loopback)


def _scan_interfaces_for_lan() -> Optional[str]:
    """以 ip/ifconfig 掃所有介面，回第一個真正 LAN 私網 IPv4（dependency-free fallback）。
    需要：Tailscale 為預設路由的機器上，8.8.8.8 trick 與 gethostname() 都只給 100.x，
    唯有列舉介面才找得到 en0/eth0 的 192.168/10/172.16 位址。"""
    import re
    import subprocess
    for cmd in (["ip", "-4", "-o", "addr"], ["ifconfig"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out):
            if _is_lan_ipv4(m.group(1)):
                return m.group(1)
    return None


def _local_ip() -> str:
    """挑一個讓同網段手機掃 QR 後連得到的本機位址：LAN 私網優先，避開 Tailscale 100.x。
    註：權威的可達位址仍以 Android NSD 解析結果為準（#6c-4）；本函式是 QR host 的最佳猜測。"""
    # 1. 預設路由來源位址（多數情況即真正 LAN）
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        primary = s.getsockname()[0]
    except OSError:
        primary = ""
    finally:
        s.close()
    if _is_lan_ipv4(primary):
        return primary
    # 2. primary 落在 Tailscale/overlay 或取不到 → 掃 gethostname 解析
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if _is_lan_ipv4(info[4][0]):
                return info[4][0]
    except OSError:
        pass
    # 3. 列舉介面（macOS Tailscale 預設路由下，前兩步都只會給 100.x）
    scanned = _scan_interfaces_for_lan()
    if scanned:
        return scanned
    # 4. 退而求其次（best effort）
    return primary or "127.0.0.1"


# ── Client（手機側規格參照；Kotlin 依此實作）───────────────────────────────

def pull_session(host: str, port: int, my_identity: pr.DeviceIdentity,
                 server_pubkey: bytes, session_id: str, timeout: float = 15.0) -> dict:
    """連到 server 拉取並解密一則 session 的 bundle。失敗丟例外。"""
    with socket.create_connection((host, port), timeout=timeout) as conn:
        # 握手
        _send_frame(conn, json.dumps({
            "did": my_identity.device_id, "pk": my_identity.public_b64}).encode())
        ack = json.loads(_recv_frame(conn).decode())
        if not ack.get("ok"):
            raise PermissionError(ack.get("err", "handshake rejected"))
        # 加密請求（證明持有私鑰）
        req = json.dumps({"session_id": session_id}).encode()
        _send_frame(conn, pr.box_encrypt(my_identity.private_key, server_pubkey, req))
        resp = json.loads(_recv_frame(conn).decode())
        if not resp.get("ok"):
            raise LookupError(resp.get("err", "request failed"))
        blob = _recv_frame(conn)
        plaintext = pr.box_decrypt(my_identity.private_key, server_pubkey, blob)
        return json.loads(plaintext.decode("utf-8"))
