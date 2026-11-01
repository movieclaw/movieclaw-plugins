# Line-by-line port of SheltonZhu/115driver pkg/crypto/m115 (public.go + xor.go
# + rsa.go, GPL-3.0-or-later). This is the download-family cipher used by
# alist/OpenList's DownloadWithUA -- 115's chrome/downurl endpoint answers it
# with a m115-sealed `data` field, which the legacy RSA envelope (p115client
# download_url_app) no longer gets for real accounts (see 1.7.3 root cause).
# Reuses p115_rsa's constants: the xor seed table, client key and RSA pubkey
# are byte-identical to the Go originals.
from __future__ import annotations
from base64 import b64decode, b64encode
from os import urandom
from p115_rsa import G_kts as XOR_KEY_SEED, G_key_l as XOR_CLIENT_KEY, RSA_PUBKEY_PAIR

_N, _E = RSA_PUBKEY_PAIR
_KL = 128  # 1024-bit modulus -> 128-byte blocks


def _xor_derive_key(seed: bytes, size: int) -> bytes:
    return bytes(((seed[i] + XOR_KEY_SEED[size * i]) & 0xff) ^ XOR_KEY_SEED[size * (size - i - 1)]
                 for i in range(size))


def _xor_transform(data: bytes, key: bytes) -> bytes:
    # Go m115/xor.go: XOR the len%4 head with key cycling from index 0, then
    # continue the key cycle reset at the head boundary for the remainder.
    out = bytearray(data)
    ds, ks = len(out), len(key)
    mod = ds % 4
    for i in range(mod):
        out[i] ^= key[i % ks]
    for i in range(mod, ds):
        out[i] ^= key[(i - mod) % ks]
    return bytes(out)


def _rsa_encrypt(data: bytes) -> bytes:
    # PKCS#1 v1.5 type-2 blocks: 00 02 <random non-zero pad> 00 <payload>,
    # 117 payload bytes per 128-byte block, every block zero-padded to 128.
    out = bytearray()
    for start in range(0, len(data), _KL - 11):
        chunk = data[start:start + _KL - 11]
        pad_size = _KL - len(chunk) - 3
        buf = bytearray(_KL)
        buf[0], buf[1] = 0, 2
        for i, b in enumerate(urandom(pad_size)):
            buf[2 + i] = b % 0xff + 1
        buf[pad_size + 2] = 0
        buf[pad_size + 3:] = chunk
        out += pow(int.from_bytes(buf, 'big'), _E, _N).to_bytes(_KL, 'big')
    return bytes(out)


def _rsa_decrypt(data: bytes) -> bytes:
    # Public-key decrypt: drop everything up to and including the first zero
    # byte at index > 0 (PKCS#1 v1.5 separator).
    out = bytearray()
    for start in range(0, len(data), _KL):
        p = pow(int.from_bytes(data[start:start + _KL], 'big'), _E, _N)
        b = p.to_bytes((p.bit_length() + 0b111) >> 3, 'big') if p else b''
        for i, x in enumerate(b):
            if x == 0 and i != 0:
                out += b[i + 1:]
                break
    return bytes(out)


def generate_key() -> bytes:
    return urandom(16)


def encode(data: bytes, key: bytes) -> str:
    """m115 Encode (client -> 115): xor(key,4) -> reverse -> xor(clientKey),
    RSA-seal `key + body` and base64 it. The recipient recovers our key from
    the sealed prefix, which is what later lets it seal the response for us."""
    body = _xor_transform(data, _xor_derive_key(key, 4))[::-1]
    body = _xor_transform(body, XOR_CLIENT_KEY)
    return b64encode(_rsa_encrypt(key + body)).decode('ascii')


def decode(data: str, key: bytes) -> bytes:
    """m115 Decode (115 -> client): base64 -> RSA -> take the response key from
    the first 16 bytes -> xor(respKey,12) -> reverse -> xor(ourKey,4)."""
    raw = _rsa_decrypt(b64decode(data))
    if len(raw) < 17:
        raise ValueError('m115 payload too short')
    resp_key = raw[:16]
    body = _xor_transform(raw[16:], _xor_derive_key(resp_key, 12))[::-1]
    return _xor_transform(body, _xor_derive_key(key, 4))
