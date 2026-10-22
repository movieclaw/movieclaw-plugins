"""Trakt 的官方 HTTPS 接口；错误只带状态码，不回显账号凭据或响应正文。"""

import time
from urllib.parse import urlparse

import httpx

API_URL = "https://api.trakt.tv"


class TraktError(Exception):
    def __init__(self, status: int, *, retry_after: float = 60, uncertain: bool = False):
        self.status = status
        self.retry_after = max(1, min(retry_after, 86400))
        self.uncertain = uncertain
        super().__init__(f"Trakt 请求未完成（HTTP {status}）")


class TraktClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http

    async def request(self, method: str, path: str, body=None, token=None, params=None):
        headers = {
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "User-Agent": "MovieClaw-Trakt/0.2.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self.http.request(
                method, API_URL + path, json=body, headers=headers, params=params
            )
        except httpx.TransportError:
            raise TraktError(0, uncertain=method == "POST") from None
        if not 200 <= response.status_code < 300:
            try:
                retry = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                retry = 60
            raise TraktError(
                response.status_code,
                retry_after=retry,
                uncertain=method == "POST" and response.status_code >= 500,
            )
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise TraktError(502, uncertain=method == "POST") from None

    def application_fields(self):
        fields = {"client_id": self.client_id}
        if self.client_secret:
            fields["client_secret"] = self.client_secret
        return fields

    async def begin(self):
        result = await self.request("POST", "/oauth/device/code", {"client_id": self.client_id})
        url = urlparse(result.get("verification_url", ""))
        if url.scheme != "https" or url.hostname not in {
            "trakt.tv",
            "app.trakt.tv",
            "auth.trakt.tv",
        }:
            raise TraktError(502)
        if not result.get("device_code") or not result.get("user_code"):
            raise TraktError(502)
        return result

    async def poll(self, code):
        return await self.request(
            "POST", "/oauth/device/token", {**self.application_fields(), "code": code}
        )

    async def refresh(self, refresh_token):
        return await self.request(
            "POST",
            "/oauth/token",
            {
                **self.application_fields(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    @staticmethod
    def credentials(tokens: dict, *, now=None):
        if not all(
            isinstance(tokens.get(k), str) and tokens[k] for k in ("access_token", "refresh_token")
        ):
            raise TraktError(502)
        now = time.time() if now is None else now
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": float(tokens.get("created_at", now))
            + float(tokens.get("expires_in", 604800)),
        }
