"""
pairing tests — requires PyNaCl, run with a venv:
    /tmp/handoff-venv/bin/python handoff/tests/test_pairing.py

Verifies: identity keygen+persistence(0600), device_id derived from the public key and stable,
QR round-trip+tamper protection, NaCl Box encrypt/decrypt round-trip + tamper detection + wrong-key rejection.
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

    # 1. keygen + persistence: the second load should be the same key
    a1 = p.load_or_create_identity(keyfile)
    a2 = p.load_or_create_identity(keyfile)
    assert bytes(a1.public_key) == bytes(a2.public_key), "reload should be the same identity"
    assert stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600, "private key file must be 0600"

    # 2. device_id derived from the public key, stable
    assert a1.device_id == p.device_id_for(bytes(a1.public_key))
    assert len(a1.device_id) == 16  # 8 bytes hex

    # 3. QR round-trip
    qr = p.build_pair_qr(a1, host="192.168.1.5", port=9120)
    assert "private" not in qr.lower() and bytes(a1.private_key).hex() not in qr, \
        "QR must not contain the private key"
    peer = p.parse_pair_qr(qr)
    assert peer.device_id == a1.device_id
    assert peer.public_key == bytes(a1.public_key)
    assert peer.host == "192.168.1.5" and peer.port == 9120

    # 3b. tampered QR (did does not match pk) should be rejected
    bad = qr.replace(a1.device_id, "deadbeefdeadbeef")
    try:
        p.parse_pair_qr(bad)
        assert False, "tampered QR should be rejected"
    except ValueError:
        pass

    # 3c. a pure pairing QR(v1) parses with session_id as None
    assert peer.session_id is None, "v1 pairing QR should not carry a session_id"

    # 3d. handoff QR(v2): carries session_id, round-trip + private key not leaked
    hqr = p.build_handoff_qr(a1, host="10.0.0.9", port=9120, session_id="sess-abc-123")
    assert bytes(a1.private_key).hex() not in hqr, "handoff QR must not contain the private key"
    hpeer = p.parse_qr(hqr)
    assert hpeer.device_id == a1.device_id
    assert hpeer.public_key == bytes(a1.public_key)
    assert hpeer.host == "10.0.0.9" and hpeer.port == 9120
    assert hpeer.session_id == "sess-abc-123", "handoff QR should carry back the session_id"

    # 3e. handoff QR missing sid should be rejected (builder-side guard)
    try:
        p.build_handoff_qr(a1, host="10.0.0.9", port=9120, session_id="")
        assert False, "empty session_id should be rejected"
    except ValueError:
        pass

    # 3f. a forged v2 (missing sid) should be rejected on parse
    import json as _json
    nosid = _json.dumps({"v": 2, "did": a1.device_id, "pk": a1.public_b64,
                         "host": "h", "port": 1})
    try:
        p.parse_qr(nosid)
        assert False, "v2 missing sid should be rejected"
    except ValueError:
        pass

    # 3g. tampering the did of a handoff QR is still rejected (forgery protection applies to v2 too)
    badh = hqr.replace(a1.device_id, "deadbeefdeadbeef")
    try:
        p.parse_qr(badh)
        assert False, "tampered handoff QR should be rejected"
    except ValueError:
        pass

    # 3h. a short public key (not 32 bytes) should be rejected at the parse stage even if the did is self-consistent (review P2)
    short_pk = b"\x01"
    short = _json.dumps({"v": 1, "did": p.device_id_for(short_pk),
                         "pk": p._b64e(short_pk), "host": "h", "port": 1})
    try:
        p.parse_qr(short)
        assert False, "short public key should be rejected at the parse stage"
    except ValueError:
        pass

    # 4. Box encrypt/decrypt round-trip (A→B)
    b = p.load_or_create_identity(os.path.join(tmp, "b.key"))
    msg = b"handoff bundle bytes \x00\x01\x02 \xe4\xb8\xad\xe6\x96\x87".decode("latin-1").encode("utf-8")
    ct = p.box_encrypt(a1.private_key, bytes(b.public_key), msg)
    pt = p.box_decrypt(b.private_key, bytes(a1.public_key), ct)
    assert pt == msg, "Box round-trip should recover the original plaintext"
    assert msg not in ct, "ciphertext should not contain the plaintext"

    # 5. tampered ciphertext → decryption fails
    tampered = bytearray(ct); tampered[-1] ^= 0x01
    try:
        p.box_decrypt(b.private_key, bytes(a1.public_key), bytes(tampered))
        assert False, "tampered ciphertext should fail to decrypt"
    except nacl.exceptions.CryptoError:
        pass

    # 6. a third party (wrong key) cannot decrypt
    c = p.load_or_create_identity(os.path.join(tmp, "c.key"))
    try:
        p.box_decrypt(c.private_key, bytes(a1.public_key), ct)
        assert False, "a non-paired party should not be able to decrypt"
    except nacl.exceptions.CryptoError:
        pass

    print("✅ all passed (7 groups): keygen/0600 · device_id · pairing/handoff QR round-trip+tamper protection"
          "(v1/v2/missing-sid/tampered/short-pubkey) · Box round-trip · tamper detection · wrong-key rejection")
    return 0


if __name__ == "__main__":
    sys.exit(run())
