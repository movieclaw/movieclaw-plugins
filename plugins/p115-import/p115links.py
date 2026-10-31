"""入口只做链接拆分；保留消息上下文与提取码，不用 AI 重新拼链接。"""
import re
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode

_LINK=re.compile(r"https?://(?:[a-z0-9-]+\.)?115(?:cdn)?\.com/s/[^\s，。；、<>）)\]】]+|ed2k://\|file\|[^|]+\|\d+\|[a-f0-9]{32}\|/?|magnet:\?[^\s，。；、<>）)\]】]+",re.I)
_PASS=re.compile(r"(?:提取码|提取碼|访问码|访问碼|密码|密碼)\s*[:：]?\s*([A-Za-z0-9]{4,8})")


def split_inputs(text):
    matches=list(_LINK.finditer(text or ""))
    if not matches:
        return [(text or "").strip()]
    out=[];offline=[]
    for i,match in enumerate(matches):
        link=match.group().rstrip(".,;，。；")
        if link.lower().startswith(("ed2k:","magnet:")):
            offline.append(link);continue
        parts=urlsplit(link)
        query=dict(parse_qsl(parts.query))
        if not query.get("password") and not query.get("pwd"):
            tail=text[match.end():matches[i+1].start() if i+1<len(matches) else len(text)]
            password=_PASS.search(tail)
            if password:
                query["password"]=password.group(1)
                link=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment))
        if link not in out:out.append(link)
    if offline:out.append("\n".join(dict.fromkeys(offline)))
    if len(out)>20:raise ValueError("一次最多20批链接，请拆开发送")
    return out
