"""bencode 编解码：只做转种需要的最小实现（解码为原始字节保留、编码重排）。

参考各 BT 工具通用实现；本插件用它做两件事：
- 解出 .torrent 的 announce / info，判断要不要补 tracker；
- 补完 tracker 后重编码（qB/TR 都接受未经签名的标准 bencode）。
"""

from __future__ import annotations


class BencodeError(ValueError):
    """不是合法的 bencode 字节流。"""


def bdecode(data: bytes):
    """解码 bencode → Python 对象（bytes / int / list / dict）。"""
    value, end = _decode(data, 0)
    if end != len(data):
        raise BencodeError("bencode 流有多余的尾部字节")
    return value


def bencode(obj) -> bytes:
    """编码 Python 对象 → bencode 字节流（dict 的 key 必须是 bytes，按字典序）。"""
    return _encode(obj)


# ---------------------------------------------------------------------------


def _decode(data: bytes, i: int):
    if i >= len(data):
        raise BencodeError("意外结束")
    c = data[i : i + 1]
    if c == b"i":
        j = data.find(b"e", i)
        if j < 0:
            raise BencodeError("整数没有结尾")
        return int(data[i + 1 : j]), j + 1
    if c == b"l":
        i += 1
        out: list = []
        while data[i : i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        d: dict = {}
        while data[i : i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            d[k] = v
        return d, i + 1
    if c.isdigit():
        j = data.find(b":", i)
        if j < 0:
            raise BencodeError("字符串没有长度分隔符")
        n = int(data[i:j])
        if n < 0:
            raise BencodeError("字符串长度为负")
        start = j + 1
        end = start + n
        if end > len(data):
            raise BencodeError("字符串长度越界")
        return data[start:end], end
    raise BencodeError(f"非法的 bencode 起始字节：{c!r}")


def _encode(obj) -> bytes:
    if isinstance(obj, bool):
        raise BencodeError("bencode 不支持布尔（用 0/1）")
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        return b"%d:%s" % (len(raw), raw)
    if isinstance(obj, list):
        return b"l" + b"".join(_encode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = []
        for k in sorted(obj):
            kb = k.encode("utf-8") if isinstance(k, str) else k
            if not isinstance(kb, bytes):
                raise BencodeError(f"字典键类型不支持：{type(k)}")
            items.append(_encode(kb) + _encode(obj[k]))
        return b"d" + b"".join(items) + b"e"
    raise BencodeError(f"类型不支持 bencode：{type(obj)}")
