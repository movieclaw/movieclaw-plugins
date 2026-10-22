"""Mac 维护工具：验证 Apple Archive 的签名并读取快捷指令流程。"""
import plistlib
import struct
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def decode(path: Path, folder: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) < 256 or raw[:4] != b'AEA1':
        raise ValueError('不是已签名的快捷指令文件')
    length = struct.unpack('<I', raw[8:12])[0]
    if not 0 < length < len(raw) - 12:
        raise ValueError('快捷指令封装损坏')
    metadata = plistlib.loads(raw[12:12 + length])
    certificate = x509.load_der_x509_certificate(metadata['SigningCertificateChain'][0])
    public = folder / 'public.pem'
    public.write_bytes(certificate.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    archive = folder / 'signed.aa'
    destination = folder / 'decoded'
    destination.mkdir()
    subprocess.run(['aea', 'decrypt', '-i', str(path), '-o', str(archive),
                    '-sign-pub', str(public)], check=True, capture_output=True, timeout=30)
    subprocess.run(['aa', 'extract', '-i', str(archive), '-d', str(destination)],
                   check=True, capture_output=True, timeout=30)
    return plistlib.loads((destination / 'Shortcut.wflow').read_bytes())
