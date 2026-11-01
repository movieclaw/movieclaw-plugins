"""m115 download cipher tests (1.7.3).

The cipher is a line-by-line port of SheltonZhu/115driver pkg/crypto/m115 --
the wire protocol of alist/OpenList's DownloadWithUA. Its RSA envelope is NOT
locally reciprocal by design: requests are encrypted with the 115 public key
(only 115's private key can read them), and responses are encrypted by 115
with its private key so the public key decrypts them. So we verify in layers:
1. xor transforms are self-inverse and the derive keys match the Go code;
2. the RSA block/pad/separator/extract logic round-trips through an e=1
   channel (where pow(x,1,N)=x, so encrypt becomes readable to decrypt);
3. with the RSA seal replaced by an identity stand-in, a simulated 115
   (which knows our request key, as the real one recovers it from the sealed
   prefix) can read our request and seal a response we decode back.
The real-key exchange still needs an on-device check -- that is what the
panel's 播放诊断 report is for.
"""
import json
import pytest
import p115_m115 as m
from base64 import b64decode, b64encode


def test_xor_transform_is_self_inverse():
    for data in (b'', b'a', b'abcd', b'hello world!!', b'\x00\x01\x02\x03\x04'):
        for key in (b'abcd', b'x\x06\xadL3\x86]\x18L\x01?F'):
            assert m._xor_transform(m._xor_transform(data, key), key) == data


def test_xor_transform_head_boundary():
    # Go m115/xor.go cycles the key over the len%4 head first, then RESTARTS
    # the cycle for the rest -- the rest of the key stream is not simply
    # key[i % len] continued, it re-begins at the head boundary.
    data = bytes(range(5)); key = b'abcd'
    out = m._xor_transform(data, key)
    mod = len(data) % 4
    expected = (bytes(data[i] ^ key[i % 4] for i in range(mod)) +
                bytes(data[i] ^ key[(i - mod) % 4] for i in range(mod, len(data))))
    assert out == expected


def test_xor_derive_key_shape():
    assert len(m._xor_derive_key(b'\x00' * 16, 4)) == 4
    assert len(m._xor_derive_key(b'\x00' * 16, 12)) == 12


def test_rsa_block_structure_roundtrips_on_e1_channel(monkeypatch):
    monkeypatch.setattr(m, '_E', 1)  # pow(x,1,N)=x: the seal becomes readable
    blob = bytes(range(256)) * 2
    for n in (1, 50, 117, 118, 234, 300):
        data = blob[:n]
        cipher = m._rsa_encrypt(data)
        assert len(cipher) % 128 == 0, 'every RSA block must be 128 bytes'
        assert m._rsa_decrypt(cipher) == data, 'pad/separator/extract broken at %d bytes' % n


def test_simulated_server_roundtrip(monkeypatch):
    # RSA seal -> identity: the test simulates 115's bookkeeping around it.
    monkeypatch.setattr(m, '_rsa_encrypt', lambda d: d)
    monkeypatch.setattr(m, '_rsa_decrypt', lambda d: d)
    key = m.generate_key()
    # 115 reads our request: strip our key prefix, undo the xor chain.
    payload = json.dumps({'pickcode': 'abcdef123456'}, separators=(',', ':')).encode()
    raw = b64decode(m.encode(payload, key))
    assert raw[:16] == key, 'sealed prefix must carry the request key'
    read_back = m._xor_transform(m._xor_transform(raw[16:], m.XOR_CLIENT_KEY)[::-1],
                                 m._xor_derive_key(key, 4))
    assert read_back == payload
    # 115 seals a response: its own key first, then the inverse of decode.
    resp = {'abcdef123456': {'url': {'url': 'https://cdn.example.com/movie.mkv?f=1'},
                             'file_name': 'x', 'file_size': 1}}
    result = json.dumps(resp).encode()
    server_key = m.generate_key()
    body = m._xor_transform(result, m._xor_derive_key(key, 4))[::-1]
    body = m._xor_transform(body, m._xor_derive_key(server_key, 12))
    assert m.decode(b64encode(server_key + body).decode(), key) == result


def test_decode_rejects_short_payload(monkeypatch):
    monkeypatch.setattr(m, '_rsa_decrypt', lambda d: d)
    with pytest.raises(ValueError):
        m.decode(b64encode(b'123').decode(), m.generate_key())
