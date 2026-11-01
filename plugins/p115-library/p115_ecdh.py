# Adapted from p115cipher 0.0.6, ChenyangGao, GPL-3.0-or-later.
# ECDH response-envelope subset (fixed-key AES-CBC + bounded LZ4): exactly what
# 115 returns for downurl/signing requests. The upload-only token/payload
# builders were removed together with the upload feature in 2.0.0.
from __future__ import annotations
from p115_aes import AES as AES_
from typing import Final
Buffer = object


def to_bytes_view(v): return memoryview(v).cast('B')


def _pad(data, /):
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    if (pad_size := (-len(data) & 15)):
        return data + bytes((pad_size,) * pad_size)
    return data


def _unpad(data, /):
    view = to_bytes_view(data)
    if 0 < (pad_size := view[-1]) < 16 and all((c == pad_size for c in view[-pad_size:])):
        return view[:-pad_size].tobytes()
    return data


def aes_cbc_encrypt(data, aes_key, aes_iv):
    encrypt = AES_(aes_key).encrypt
    view = to_bytes_view(_pad(data))
    b = bytearray()
    puts = b.extend
    last_cipher = aes_iv
    for i in range(0, len(view), 16):
        last_cipher = encrypt([p ^ l for p, l in zip(view[i:i + 16], last_cipher)])
        puts(last_cipher)
    return b


def aes_cbc_decrypt(cipher_data, aes_key, aes_iv):
    decrypt = AES_(aes_key).decrypt
    view = to_bytes_view(cipher_data)
    b = bytearray()
    puts = b.extend
    last_cipher = aes_iv
    for i in range(0, len(view), 16):
        puts((p ^ l for p, l in zip(decrypt(view[i:i + 16]), last_cipher)))
        last_cipher = view[i:i + 16]
    return _unpad(b)


def lz4_decompress(source: Buffer, /) -> bytes:
    src = to_bytes_view(source)
    data = b''
    while src and src.nbytes > 2 and (src_len := (src[0] + (src[1] << 8))):
        data += lz4_block_decompress(src[2:src_len + 2], 8192)
        src = src[src_len + 2:]
    return data


def lz4_block_decompress(source, uncompressed_size=8192):
    s=bytes(source);i=0;out=bytearray()
    def length(n):
        nonlocal i
        if n==15:
            while True:
                v=s[i];i+=1;n+=v
                if v!=255:break
        return n
    while i<len(s):
        token=s[i];i+=1;n=length(token>>4)
        if i+n>len(s) or len(out)+n>uncompressed_size:raise ValueError('invalid LZ4 literals')
        out.extend(s[i:i+n]);i+=n
        if i==len(s):break
        offset=int.from_bytes(s[i:i+2],'little');i+=2;n=length(token&15)+4
        if not 0<offset<=len(out) or len(out)+n>uncompressed_size:raise ValueError('invalid LZ4 match')
        for _ in range(n):out.append(out[-offset])
    return bytes(out)


AES_KEY: Final      = b"\xfb\x1a\x19\xd6R\xf5\xaa\xf7\xbce\x1d\x0fi\xbfB/"

AES_IV: Final       = b"i\xbfB/I\x96\x05P\xa0\xadD\xec4F\xcbL"


def ecdh_aes_encrypt(
    data: Buffer,
    /,
    aes_key: bytes = AES_KEY,
    aes_iv: bytes = AES_IV,
) -> bytes:
    """用 AES 加密数据，密钥由 ECDH 生成"""
    return aes_cbc_encrypt(data, aes_key, aes_iv)


def ecdh_aes_decrypt(
    cipher_data: Buffer,
    /,
    aes_key: bytes = AES_KEY,
    aes_iv: bytes = AES_IV,
) -> bytes:
    """用 AES 解密数据，密钥由 ECDH 生成（downurl 响应信封）"""
    data = aes_cbc_decrypt(cipher_data, aes_key, aes_iv)
    return lz4_decompress(data)
