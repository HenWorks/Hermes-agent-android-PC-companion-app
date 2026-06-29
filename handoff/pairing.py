"""
pairing — device identity, pairing QR, and NaCl Box end-to-end encryption for hermes
handoff (#2).

The desktop side uses PyNaCl; the phone side (Kotlin) uses lazysodium-android — both back
onto the same libsodium -> Box (Curve25519 + XSalsa20-Poly1305) is binary-interoperable.
This module is the desktop side + the spec reference.

Model:
- One long-term Curve25519 key pair per device (private key stored in a 0600 file; public
  key used for pairing + encryption).
- device_id = hex of the public key's first 8 bytes (stable, verifiable from the public
  key, unforgeable).
- Pairing: the desktop shows a QR (JSON: v/did/pk/host/port); the phone scans it ->
  stores the desktop pubkey+did. The phone's pubkey is sent back to the desktop during
  the transfer handshake (see #4); both sides store each other's -> pairing complete.
- Subsequent communication: Box(my_sk, peer_pk) authenticated encryption (any tampering /
  wrong key causes decryption to fail).

🔴 The private key never leaves the machine and never enters the bundle/QR; the QR
contains only the "public key".
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import nacl.public
import nacl.utils

QR_SCHEMA = 1          # pure pairing QR (trust establishment, no session)
HANDOFF_QR_SCHEMA = 2  # handoff QR: pairing info + a specified session_id (first scan does pairing + selection at once)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def device_id_for(pubkey: bytes) -> str:
    """Derive a stable device_id from the public key (first 8 bytes, hex)."""
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
    """Load the long-term private key from a 0600 file; if absent, generate one and write
    it out atomically as 0600."""
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            sk = nacl.public.PrivateKey(f.read())
        return DeviceIdentity(sk, sk.public_key)
    sk = nacl.public.PrivateKey.generate()
    d = os.path.dirname(os.path.abspath(key_path)) or "."
    os.makedirs(d, exist_ok=True)
    # 0600 atomic write
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


# ── Pairing QR ────────────────────────────────────────────────────────────────

@dataclass
class PeerInfo:
    device_id: str
    public_key: bytes
    host: str
    port: int
    session_id: str | None = None  # only carried by handoff QR (v2); None for pure pairing QR (v1)


def build_pair_qr(identity: DeviceIdentity, host: str, port: int) -> str:
    """Produce pure pairing QR content (v1, for trust establishment). Contains only the
    public key, never the private key."""
    return json.dumps({
        "v": QR_SCHEMA,
        "did": identity.device_id,
        "pk": identity.public_b64,
        "host": host,
        "port": port,
    }, separators=(",", ":"))


def build_handoff_qr(identity: DeviceIdentity, host: str, port: int,
                     session_id: str) -> str:
    """Produce handoff QR content (v2): pairing info + a specified session_id. The phone's
    first scan completes pairing + selects the session to receive at once. Contains only
    the public key, never the private key."""
    if not session_id:
        raise ValueError("handoff QR requires a session_id")
    return json.dumps({
        "v": HANDOFF_QR_SCHEMA,
        "did": identity.device_id,
        "pk": identity.public_b64,
        "host": host,
        "port": port,
        "sid": session_id,
    }, separators=(",", ":"))


def parse_qr(s: str) -> PeerInfo:
    """Parse a pairing (v1) or handoff (v2) QR; verify the device_id matches the public
    key (anti-forgery). v2 must carry sid, and the returned PeerInfo.session_id is the
    session to receive."""
    d = json.loads(s)
    v = d.get("v")
    if v not in (QR_SCHEMA, HANDOFF_QR_SCHEMA):
        raise ValueError(f"unsupported QR schema: {v}")
    pk = _b64d(d["pk"])
    if len(pk) != 32:
        raise ValueError(f"wrong public key length: {len(pk)} (Curve25519 must be 32 bytes)")
    if device_id_for(pk) != d["did"]:
        raise ValueError("device_id does not match public key (QR may have been tampered with)")
    sid = None
    if v == HANDOFF_QR_SCHEMA:
        sid = d.get("sid")
        if not sid:
            raise ValueError("handoff QR (v2) is missing sid")
    return PeerInfo(device_id=d["did"], public_key=pk,
                    host=d["host"], port=int(d["port"]), session_id=sid)


# Backward-compatibility alias: older callers/tests use parse_pair_qr; now unified to
# parse_qr (still accepts v1).
parse_pair_qr = parse_qr


# ── NaCl Box end-to-end encryption ─────────────────────────────────────────────────────

def box_encrypt(my_sk: nacl.public.PrivateKey, peer_pk: bytes, plaintext: bytes) -> bytes:
    """Box authenticated encryption; returns nonce(24) + ciphertext. Only the peer with the
    matching key can decrypt + verify authenticity."""
    box = nacl.public.Box(my_sk, nacl.public.PublicKey(peer_pk))
    nonce = nacl.utils.random(nacl.public.Box.NONCE_SIZE)
    return bytes(box.encrypt(plaintext, nonce))  # EncryptedMessage = nonce||ct


def box_decrypt(my_sk: nacl.public.PrivateKey, peer_pk: bytes, blob: bytes) -> bytes:
    """Box decryption + authentication; tampering or a wrong key raises
    nacl.exceptions.CryptoError."""
    box = nacl.public.Box(my_sk, nacl.public.PublicKey(peer_pk))
    return box.decrypt(blob)
