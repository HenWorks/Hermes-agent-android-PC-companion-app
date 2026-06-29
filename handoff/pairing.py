"""
pairing — hermes 接力的裝置身分、配對 QR、NaCl Box 端到端加密（#2）。

桌面端用 PyNaCl；手機端（Kotlin）用 lazysodium-android，底層同一個 libsodium →
Box（Curve25519 + XSalsa20-Poly1305）二進位互通。本模組是桌面側 + 規格參照。

模型：
- 每台一組長期 Curve25519 金鑰對（私鑰存 0600 檔；公鑰用於配對 + 加密）。
- device_id = 公鑰前 8 bytes 的 hex（穩定、可由公鑰驗證，不可偽造）。
- 配對：桌面顯示 QR(JSON: v/did/pk/host/port)；手機掃 → 存桌面 pubkey+did。
  反向把手機 pubkey 給桌面在傳輸握手時帶上（見 #4），雙方互存 → 配對完成。
- 之後通訊：Box(my_sk, peer_pk) 認證加密（任何竄改/錯金鑰都會解密失敗）。

🔴 私鑰永不離開本機、永不入 bundle/QR；QR 只含「公鑰」。
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import nacl.public
import nacl.utils

QR_SCHEMA = 1          # 純配對 QR（信任建立，無 session）
HANDOFF_QR_SCHEMA = 2  # 接力 QR：配對資訊 + 指定 session_id（首掃同時完成配對 + 選取）


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def device_id_for(pubkey: bytes) -> str:
    """由公鑰推出穩定 device_id（前 8 bytes hex）。"""
    return pubkey[:8].hex()


@dataclass
class DeviceIdentity:
    private_key: nacl.public.PrivateKey
    public_key: nacl.public.PublicKey

    @property
    def device_id(self) -> str:
        return device_id_for(bytes(self.public_key))

    @property
    def public_b64(self) -> str:
        return _b64e(bytes(self.public_key))


def load_or_create_identity(key_path: str) -> DeviceIdentity:
    """從 0600 檔載入長期私鑰；不存在則產生並以 0600 原子寫出。"""
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            sk = nacl.public.PrivateKey(f.read())
        return DeviceIdentity(sk, sk.public_key)
    sk = nacl.public.PrivateKey.generate()
    d = os.path.dirname(os.path.abspath(key_path)) or "."
    os.makedirs(d, exist_ok=True)
    # 0600 原子寫
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".idkey-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(bytes(sk))
        os.replace(tmp, key_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return DeviceIdentity(sk, sk.public_key)


# ── 配對 QR ────────────────────────────────────────────────────────────────

@dataclass
class PeerInfo:
    device_id: str
    public_key: bytes
    host: str
    port: int
    session_id: str | None = None  # 僅接力 QR(v2) 帶；純配對 QR(v1) 為 None


def build_pair_qr(identity: DeviceIdentity, host: str, port: int) -> str:
    """產生純配對 QR 內容（v1，信任建立用）。只含公鑰，不含私鑰。"""
    return json.dumps({
        "v": QR_SCHEMA,
        "did": identity.device_id,
        "pk": identity.public_b64,
        "host": host,
        "port": port,
    }, separators=(",", ":"))


def build_handoff_qr(identity: DeviceIdentity, host: str, port: int,
                     session_id: str) -> str:
    """產生接力 QR 內容（v2）：配對資訊 + 指定 session_id。手機首掃同時完成配對 +
    選定要接收的 session。只含公鑰，不含私鑰。"""
    if not session_id:
        raise ValueError("handoff QR 需指定 session_id")
    return json.dumps({
        "v": HANDOFF_QR_SCHEMA,
        "did": identity.device_id,
        "pk": identity.public_b64,
        "host": host,
        "port": port,
        "sid": session_id,
    }, separators=(",", ":"))


def parse_qr(s: str) -> PeerInfo:
    """解析配對(v1)或接力(v2) QR；驗證 device_id 與公鑰一致（防偽造）。
    v2 必須帶 sid，回傳的 PeerInfo.session_id 即為要接收的 session。"""
    d = json.loads(s)
    v = d.get("v")
    if v not in (QR_SCHEMA, HANDOFF_QR_SCHEMA):
        raise ValueError(f"unsupported QR schema: {v}")
    pk = _b64d(d["pk"])
    if len(pk) != 32:
        raise ValueError(f"公鑰長度錯誤：{len(pk)}（Curve25519 須 32 bytes）")
    if device_id_for(pk) != d["did"]:
        raise ValueError("device_id 與公鑰不一致（QR 可能被竄改）")
    sid = None
    if v == HANDOFF_QR_SCHEMA:
        sid = d.get("sid")
        if not sid:
            raise ValueError("接力 QR(v2) 缺少 sid")
    return PeerInfo(device_id=d["did"], public_key=pk,
                    host=d["host"], port=int(d["port"]), session_id=sid)


# 向後相容別名：舊呼叫端/測試用 parse_pair_qr，現統一走 parse_qr（仍接受 v1）。
parse_pair_qr = parse_qr


# ── NaCl Box 端到端加密 ─────────────────────────────────────────────────────

def box_encrypt(my_sk: nacl.public.PrivateKey, peer_pk: bytes, plaintext: bytes) -> bytes:
    """Box 認證加密；回傳 nonce(24) + ciphertext。peer 用對應金鑰才能解 + 驗真。"""
    box = nacl.public.Box(my_sk, nacl.public.PublicKey(peer_pk))
    nonce = nacl.utils.random(nacl.public.Box.NONCE_SIZE)
    return bytes(box.encrypt(plaintext, nonce))  # EncryptedMessage = nonce||ct


def box_decrypt(my_sk: nacl.public.PrivateKey, peer_pk: bytes, blob: bytes) -> bytes:
    """Box 解密 + 驗真；竄改或錯金鑰會丟 nacl.exceptions.CryptoError。"""
    box = nacl.public.Box(my_sk, nacl.public.PublicKey(peer_pk))
    return box.decrypt(blob)
