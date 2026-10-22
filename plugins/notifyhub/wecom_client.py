"""WeCom internal application: token cache and native news/textcard messages."""
import asyncio
import html
import time
from urllib.parse import urlsplit

import httpx
from movieclaw_sdk import net


class WecomError(RuntimeError):
    """Only sanitized details; never include request URLs or secrets."""


def valid_link(value):
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and len(value.encode()) <= 2048 else ""
    except ValueError:
        return ""


def clip_bytes(text, limit):
    text = str(text)
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:limit - 3].decode("utf-8", "ignore") + "…"


def card_description(text):
    """Escape template content and keep supported HTML inside the 512-char budget."""
    parts = []
    for line in str(text).splitlines():
        if not line.strip():
            continue
        opening, closing = '<div class="normal">', '</div>'
        available = 512 - len("".join(parts)) - len(opening) - len(closing)
        if available <= 3:
            break
        escaped = html.escape(line, quote=False)
        if len(escaped) > available:
            # Truncate plain text first; never split an HTML entity.
            shortened = line
            while len(html.escape(shortened, quote=False)) + 1 > available:
                shortened = shortened[:-1]
            escaped = html.escape(shortened, quote=False) + "…"
        parts.append(opening + escaped + closing)
    return "".join(parts) or '<div class="normal">MovieClaw 通知</div>'


class WecomClient:
    def __init__(self, cfg, logger, service="notifyhub"):
        self.cfg, self.logger, self.service = cfg, logger, service
        self.token = ""
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def request(self, method, path, *, params=None, body=None):
        try:
            async with httpx.AsyncClient(transport=net.http_transport(self.service), timeout=self.cfg.timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.request(method, self.cfg.api_base.rstrip("/") + path, params=params, json=body)
                if not 200 <= response.status_code < 300:
                    raise WecomError(f"企业微信 HTTP 状态 {response.status_code}")
                value = response.json()
                if not isinstance(value, dict):
                    raise WecomError("企业微信响应不是 JSON 对象")
                return value
        except WecomError:
            raise
        except Exception as exc:
            raise WecomError(f"企业微信请求失败（{type(exc).__name__}）") from None

    async def access_token(self):
        async with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            value = await self.request("GET", "/cgi-bin/gettoken", params={"corpid": self.cfg.corp_id, "corpsecret": self.cfg.corp_secret.get_secret_value()})
            if value.get("errcode", 0) != 0 or not isinstance(value.get("access_token"), str) or not value["access_token"]:
                raise WecomError(f"获取企业微信令牌失败，错误码 {value.get('errcode', 'unknown')}")
            try:
                lifetime = int(value.get("expires_in", 7200))
            except (TypeError, ValueError):
                raise WecomError("企业微信令牌有效期不合规") from None
            self.token = value["access_token"]
            self.expires = time.monotonic() + max(0, lifetime - min(120, lifetime // 10))
            return self.token

    def message(self, title, text, image="", link=""):
        link, image = valid_link(link), valid_link(image)
        msg = {"touser": self.cfg.to_user, "agentid": self.cfg.agent_id, "enable_duplicate_check": 1, "duplicate_check_interval": 60}
        mode = self.cfg.message_type
        if mode == "auto":
            mode = "news" if image and link else "textcard" if link else "text"
        if mode == "news" and not (image and link):
            mode = "textcard" if link else "text"
        if mode == "textcard" and not link:
            mode = "text"
        msg["msgtype"] = mode
        if mode == "news":
            msg["news"] = {"articles": [{"title": title[:128], "description": text[:512], "picurl": image, "url": link}]}
        elif mode == "textcard":
            msg["textcard"] = {"title": title[:128], "description": card_description(text), "url": link, "btntxt": "查看详情"}
        else:
            msg["text"] = {"content": clip_bytes(title + "\n" + text, 2048)}
        return msg

    async def send(self, notice):
        message = self.message(notice["title"], notice["content"], notice.get("push_img_url", ""), notice.get("push_link_url", ""))
        for attempt in range(2):
            token = await self.access_token()
            value = await self.request("POST", "/cgi-bin/message/send", params={"access_token": token}, body=message)
            code = value.get("errcode")
            if code in {40014, 42001} and attempt == 0:
                async with self.lock:
                    if self.token == token:
                        self.token, self.expires = "", 0.0
                continue
            if code != 0:
                raise WecomError(f"企业微信发送失败，错误码 {code}")
            if any(value.get(k) for k in ("invaliduser", "invalidparty", "invalidtag", "unlicenseduser")):
                self.logger.warning("企业微信已接受消息，但部分接收人无效或无接口许可；请检查应用可见范围")
            return value
        raise WecomError("企业微信令牌刷新后发送仍失败")
