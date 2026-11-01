# Adapted RSA-only portion of p115cipher 0.0.6 by ChenyangGao.
# Source declares GPLv3; distributed under GPL-3.0-or-later with this plugin.
# Changes: extracted only RSA, replaced filewrap with memoryview, no binary dependencies.
from __future__ import annotations
from base64 import b64encode, b64decode
from typing import Final, Literal, Iterator
Buffer = object
from_bytes = int.from_bytes
to_bytes = int.to_bytes
def to_bytes_view(value): return memoryview(value).cast("B")


G_kts: Final = bytes((
    0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a, 0x1a, 0x45, 0xe8, 0xbe, 0x7d, 0xa6, 0x73, 0xb8, 
    0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4, 0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa, 
    0x38, 0x01, 0x35, 0x9e, 0x26, 0x69, 0x2c, 0x86, 0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6, 
    0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd, 0x6b, 0x97, 0x8f, 0x4d, 0x8f, 0x89, 0x13, 0xb7, 
    0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e, 0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86, 
    0x50, 0x95, 0x4f, 0xd1, 0xeb, 0x83, 0x26, 0x34, 0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81, 
    0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59, 0x34, 0x66, 0x3b, 0xaa, 0xba, 0x81, 0x60, 0x48, 
    0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77, 0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e, 
    0x36, 0x9f, 0x34, 0x80, 0x5c, 0x45, 0x2c, 0x9b, 0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5, 
))

RSA_PUBKEY_PAIR: Final = (
    0x8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b8602136477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683, 
    0x10001, 
)

def acc_step(
    start: int, 
    stop: None | int = None, 
    step: int = 1, 
) -> Iterator[tuple[int, int, int]]:
    if stop is None:
        start, stop = 0, start
    for i in range(start + step, stop, step):
        yield start, (start := i), step
    if start != stop:
        yield start, stop, stop - start

def bytes_xor(
    v1: Buffer, 
    v2: Buffer, 
    /, 
    size: int = 0, 
    byteorder: Literal["little", "big"] = "big", 
) -> bytes:
    if size:
        v1 = to_bytes_view(v1)[:size]
        v2 = to_bytes_view(v2)[:size]
    else:
        size = memoryview(v1).nbytes
    return to_bytes(from_bytes(v1) ^ from_bytes(v2), size, byteorder=byteorder)

def xor(
    src: Buffer, 
    key: Buffer, 
    /, 
    byteorder: Literal["little", "big"] = "big", 
) -> bytearray:
    src = to_bytes_view(src)
    key = to_bytes_view(key)
    secret = bytearray()
    if i := len(src) & 0b11:
        secret += bytes_xor(src, key, i, byteorder=byteorder)
    for i, j, s in acc_step(i, len(src), len(key)):
        secret += bytes_xor(src[i:j], key[:s], byteorder=byteorder)
    return secret

def rsa_gen_key(
    rand_key: Buffer, 
    sk_len: int = 4, 
    /, 
) -> bytearray:
    rand_key = to_bytes_view(rand_key)
    xor_key = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    for i in range(sk_len):
        x = (rand_key[i] + G_kts[index]) & 0xff
        xor_key[i] = G_kts[length] ^ x
        length -= sk_len
        index += sk_len
    return xor_key

def pad_pkcs1_v1_5(message: Buffer, /, size: int = 128) -> int:
    data = bytearray(b"\x00")
    data += b"\x02" * (size - 2 - memoryview(message).nbytes)
    data += b"\x00"
    data += message
    return from_bytes(data)

def rsa_encrypt_with_pubkey(
    data: Buffer, 
    /, 
    block_size: int = 128, 
    n: int = RSA_PUBKEY_PAIR[0], 
    e: int = RSA_PUBKEY_PAIR[1], 
) -> bytearray:
    "把数据用 RSA 公钥加密"
    cipher_data = bytearray()
    view = to_bytes_view(data)
    for l, r, _ in acc_step(0, len(view), block_size - 11):
        cipher_data += to_bytes(
            pow(pad_pkcs1_v1_5(view[l:r], block_size), e, n), 
            block_size, 
        )
    return cipher_data

def rsa_decrypt_with_pubkey(
    cipher_data: Buffer, 
    /, 
    block_size: int = 128, 
    n: int = RSA_PUBKEY_PAIR[0], 
    e: int = RSA_PUBKEY_PAIR[1], 
) -> bytearray:
    "把数据用 RSA 公钥解密"
    data = bytearray()
    view = to_bytes_view(cipher_data)
    for l, r, _ in acc_step(0, len(view), block_size):
        p = pow(from_bytes(view[l:r]), e, n)
        b = to_bytes(p, (p.bit_length() + 0b111) >> 3)
        data += memoryview(b)[b.index(0)+1:]
    return data

G_key_l: Final  = b"x\x06\xadL3\x86]\x18L\x01?F"

G_key_s: Final  = b")#!^"

RSA_RAND_KEY: Final = b"\x00" * 16

RSA_KEY: Final      = b"\x8d\xa5\xa5\x8d"

def rsa_encrypt(
    data: Buffer, 
    /, 
    rand_key: bytes = RSA_RAND_KEY, 
) -> bytes:
    """把数据用 RSA 公钥加密

    :param data: 数据原文
    :param rand_key: 16 字节 (128 位) 的随机字节串

    :return: 数据密文
    """
    if rand_key is RSA_RAND_KEY:
        key: Buffer = RSA_KEY
    else:
        key = rsa_gen_key(rand_key)
    tmp = xor(data, key)[::-1]
    xor_data = rand_key + xor(tmp, G_key_l)
    return b64encode(rsa_encrypt_with_pubkey(xor_data))

def rsa_decrypt(
    cipher_data: Buffer, 
    /, 
    rand_key: bytes = RSA_RAND_KEY, 
) -> bytes:
    """把数据用 RSA 公钥解密

    :param cipher_data: 数据密文
    :param rand_key: 16 字节 (128 位) 的随机字节串

    :return: 数据原文
    """
    if rand_key is RSA_RAND_KEY:
        key: Buffer = RSA_KEY
    else:
        key = rsa_gen_key(rand_key)
    data = rsa_decrypt_with_pubkey(b64decode(cipher_data))
    view = memoryview(data)
    randkey = view[:16]
    key_l = rsa_gen_key(randkey, 12)
    tmp = memoryview(xor(view[16:], key_l))[::-1]
    return bytes(xor(tmp, key))
