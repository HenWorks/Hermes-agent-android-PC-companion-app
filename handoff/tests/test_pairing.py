"""
pairing 測試 — 需 PyNaCl，用 venv 跑：
    /tmp/handoff-venv/bin/python handoff/tests/test_pairing.py

驗證：身分 keygen+持久化(0600)、device_id 由公鑰推得且穩定、QR round-trip+防竄改、
NaCl Box 加解密 round-trip + 竄改偵測 + 錯金鑰拒絕。
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pairing as p  # noqa: E402
import nacl.exceptions  # noqa: E402


def run():
    tmp = tempfile.mkdtemp()
    keyfile = os.path.join(tmp, "id.key")

    # 1. keygen + 持久化：第二次載入應同一把
    a1 = p.load_or_create_identity(keyfile)
    a2 = p.load_or_create_identity(keyfile)
    assert bytes(a1.public_key) == bytes(a2.public_key), "重載應為同一身分"
    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600, "私鑰檔須 0600"

    # 2. device_id 由公鑰推得、穩定
    assert a1.device_id == p.device_id_for(bytes(a1.public_key))
    assert len(a1.device_id) == 16  # 8 bytes hex

    # 3. QR round-trip
    qr = p.build_pair_qr(a1, host="192.168.1.5", port=9120)
    assert "private" not in qr.lower() and bytes(a1.private_key).hex() not in qr, \
        "QR 不可含私鑰"
    peer = p.parse_pair_qr(qr)
    assert peer.device_id == a1.device_id
    assert peer.public_key == bytes(a1.public_key)
    assert peer.host == "192.168.1.5" and peer.port == 9120

    # 3b. 竄改 QR（did 與 pk 不符）應被拒
    bad = qr.replace(a1.device_id, "deadbeefdeadbeef")
    try:
        p.parse_pair_qr(bad)
        assert False, "竄改的 QR 應被拒"
    except ValueError:
        pass

    # 3c. 純配對 QR(v1) 解析後 session_id 為 None
    assert peer.session_id is None, "v1 配對 QR 不應帶 session_id"

    # 3d. 接力 QR(v2)：帶 session_id，round-trip + 私鑰不外洩
    hqr = p.build_handoff_qr(a1, host="10.0.0.9", port=9120, session_id="sess-abc-123")
    assert bytes(a1.private_key).hex() not in hqr, "接力 QR 不可含私鑰"
    hpeer = p.parse_qr(hqr)
    assert hpeer.device_id == a1.device_id
    assert hpeer.public_key == bytes(a1.public_key)
    assert hpeer.host == "10.0.0.9" and hpeer.port == 9120
    assert hpeer.session_id == "sess-abc-123", "接力 QR 應帶回 session_id"

    # 3e. 接力 QR 缺 sid 應被拒（建構端守衛）
    try:
        p.build_handoff_qr(a1, host="10.0.0.9", port=9120, session_id="")
        assert False, "空 session_id 應被拒"
    except ValueError:
        pass

    # 3f. 偽造的 v2（缺 sid）解析應被拒
    import json as _json
    nosid = _json.dumps({"v": 2, "did": a1.device_id, "pk": a1.public_b64,
                         "host": "h", "port": 1})
    try:
        p.parse_qr(nosid)
        assert False, "v2 缺 sid 應被拒"
    except ValueError:
        pass

    # 3g. 接力 QR 竄改 did 仍被拒（防偽造對 v2 同樣生效）
    badh = hqr.replace(a1.device_id, "deadbeefdeadbeef")
    try:
        p.parse_qr(badh)
        assert False, "竄改的接力 QR 應被拒"
    except ValueError:
        pass

    # 3h. 短公鑰（非 32 bytes）即使 did 自洽也應在 parse 階段被拒（review P2）
    short_pk = b"\x01"
    short = _json.dumps({"v": 1, "did": p.device_id_for(short_pk),
                         "pk": p._b64e(short_pk), "host": "h", "port": 1})
    try:
        p.parse_qr(short)
        assert False, "短公鑰應在 parse 階段被拒"
    except ValueError:
        pass

    # 4. Box 加解密 round-trip（A→B）
    b = p.load_or_create_identity(os.path.join(tmp, "b.key"))
    msg = b"handoff bundle bytes \x00\x01\x02 \xe4\xb8\xad\xe6\x96\x87".decode("latin-1").encode("utf-8")
    ct = p.box_encrypt(a1.private_key, bytes(b.public_key), msg)
    pt = p.box_decrypt(b.private_key, bytes(a1.public_key), ct)
    assert pt == msg, "Box round-trip 應還原原文"
    assert msg not in ct, "密文不應含明文"

    # 5. 竄改密文 → 解密失敗
    tampered = bytearray(ct); tampered[-1] ^= 0x01
    try:
        p.box_decrypt(b.private_key, bytes(a1.public_key), bytes(tampered))
        assert False, "竄改密文應解密失敗"
    except nacl.exceptions.CryptoError:
        pass

    # 6. 第三方（錯金鑰）無法解
    c = p.load_or_create_identity(os.path.join(tmp, "c.key"))
    try:
        p.box_decrypt(c.private_key, bytes(a1.public_key), ct)
        assert False, "非配對方應無法解密"
    except nacl.exceptions.CryptoError:
        pass

    print("✅ 全部通過（7 組）：keygen/0600 · device_id · 配對/接力 QR round-trip+防竄改"
          "（v1/v2/缺sid/竄改/短公鑰）· Box round-trip · 竄改偵測 · 錯金鑰拒絕")
    return 0


if __name__ == "__main__":
    sys.exit(run())
