"""
handoff_server — desktop-side handoff transport (#3 discovery + #4 encrypted transfer).

Desktop = sender/server: advertises `_hermes-handoff._tcp` over mDNS + TCP listener, and
sends the bundle for a given session, NaCl Box-encrypted, to a "paired" phone (client).
The mobile-side Kotlin implements this protocol.

Mutual authentication (bidirectional, via Box):
- the server only accepts clients in the peers list whose pubkey matches (blocks unknown devices).
- the client's request is encrypted with Box(client_sk→server_pk) → the server being able
  to decrypt = proof the client truly holds the private key for that pubkey (authenticates the client).
- the bundle is encrypted with Box(server_sk→client_pk) → the client being able to decrypt =
  proof it comes from the registered server (authenticates the server). Tampering / wrong keys
  necessarily fail.

Wire framing: 4-byte big-endian length prefix + bytes. Both handshake and payload use this framing.
🔴 Secrets are never transmitted; private keys never leave the local machine.
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


# ── peer store (device_id → pubkey b64) ──────────────────────────────────────

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


# ── wire framing ──────────────────────────────────────────────────────────────

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


# ── Server (desktop) ──────────────────────────────────────────────────────────

@dataclass
class HandoffServer:
    identity: pr.DeviceIdentity
    peers: PeerStore
    # export_fn(session_id) -> bundle dict | None; usually bound to desktop_export.export_for_handoff
    export_fn: Callable[[str], Optional[dict]]
    host: str = ""  # empty = bind the local LAN IP (_local_ip) at start; never bind 0.0.0.0 (security rule)
    port: int = 0  # 0 = auto-select

    _sock: Optional[socket.socket] = None
    _thread: Optional[threading.Thread] = None
    _running: bool = False
    _zc = None  # zeroconf instance

    def _handle(self, conn: socket.socket):
        try:
            # handshake: client sends {did, pk} in cleartext
            hello = json.loads(_recv_frame(conn).decode("utf-8"))
            cdid = hello["did"]
            cpk = pr._b64d(hello["pk"])
            if not self.peers.is_paired(cdid, cpk):
                _send_frame(conn, json.dumps({"ok": False, "err": "not paired"}).encode())
                return
            _send_frame(conn, json.dumps({"ok": True, "proto": PROTO}).encode())

            # client sends a request encrypted with Box(client_sk→server_pk) → server decrypts = authenticates the client
            req_blob = _recv_frame(conn)
            req = json.loads(pr.box_decrypt(self.identity.private_key, cpk, req_blob))
            session_id = req["session_id"]

            bundle = self.export_fn(session_id)
            if bundle is None:
                _send_frame(conn, json.dumps({"ok": False, "err": "session not found"}).encode())
                return
            # bundle encrypted with Box(server_sk→client_pk) → only this client can decrypt + verify source
            payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
            _send_frame(conn, json.dumps({"ok": True}).encode())
            _send_frame(conn, pr.box_encrypt(self.identity.private_key, cpk, payload))
        except Exception as e:  # noqa: BLE001 — a single connection error should not take down the server
            try:
                _send_frame(conn, json.dumps({"ok": False, "err": str(e)}).encode())
            except OSError:
                pass
        finally:
            conn.close()

    def start(self, advertise: bool = True) -> int:
        bind_host = self.host or _local_ip()  # bind the local LAN IP; never 0.0.0.0 (security rule)
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


# Tailscale / overlay shared address space (RFC 6598 CGNAT). A QR-scanning phone that is
# only on the same Wi-Fi without Tailscale can't reach the desktop's 100.x address → the
# QR host must avoid this range and prefer a real LAN private network.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_lan_ipv4(ip: str) -> bool:
    """Whether this is an IPv4 private address a phone on the same subnet can reach directly (excludes CGNAT/Tailscale and loopback)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (a.version == 4 and a.is_private
            and a not in _CGNAT_NET and not a.is_loopback)


def _scan_interfaces_for_lan() -> Optional[str]:
    """Scan all interfaces via ip/ifconfig and return the first real LAN private IPv4 (dependency-free fallback).
    Needed because: on machines where Tailscale is the default route, both the 8.8.8.8 trick and
    gethostname() only yield 100.x; only enumerating interfaces finds en0/eth0's 192.168/10/172.16 address."""
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
    """Pick a local address a phone on the same subnet can reach after scanning the QR: LAN private preferred, avoiding Tailscale 100.x.
    Note: the authoritative reachable address still comes from Android NSD resolution (#6c-4); this function is the QR host's best guess."""
    # 1. default-route source address (in most cases the real LAN)
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
    # 2. primary falls in Tailscale/overlay or is unavailable → scan gethostname resolution
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if _is_lan_ipv4(info[4][0]):
                return info[4][0]
    except OSError:
        pass
    # 3. enumerate interfaces (under macOS Tailscale default route, the first two steps only yield 100.x)
    scanned = _scan_interfaces_for_lan()
    if scanned:
        return scanned
    # 4. best effort fallback
    return primary or "127.0.0.1"


# ── Client (mobile-side spec reference; Kotlin implements against this) ──────

def pull_session(host: str, port: int, my_identity: pr.DeviceIdentity,
                 server_pubkey: bytes, session_id: str, timeout: float = 15.0) -> dict:
    """Connect to the server to pull and decrypt one session's bundle. Raises on failure."""
    with socket.create_connection((host, port), timeout=timeout) as conn:
        # handshake
        _send_frame(conn, json.dumps({
            "did": my_identity.device_id, "pk": my_identity.public_b64}).encode())
        ack = json.loads(_recv_frame(conn).decode())
        if not ack.get("ok"):
            raise PermissionError(ack.get("err", "handshake rejected"))
        # encrypted request (proves possession of the private key)
        req = json.dumps({"session_id": session_id}).encode()
        _send_frame(conn, pr.box_encrypt(my_identity.private_key, server_pubkey, req))
        resp = json.loads(_recv_frame(conn).decode())
        if not resp.get("ok"):
            raise LookupError(resp.get("err", "request failed"))
        blob = _recv_frame(conn)
        plaintext = pr.box_decrypt(my_identity.private_key, server_pubkey, blob)
        return json.loads(plaintext.decode("utf-8"))
