"""豆瓣公开移动端演职员接口的最小异步客户端。

只访问 ``/movie/{id}/celebrities``；限速、缓存和错误分类由调用方配合。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

from movieclaw_sdk import net

from .matching import DoubanActor

_DEFAULT_BASE = "https://m.douban.com/rexxar/api/v2"
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_MAX_ACTORS = 40


class DoubanError(Exception):
    """豆瓣请求失败；消息可直接展示给用户。"""


class DoubanClient:
    def __init__(
        self,
        base_url: str = _DEFAULT_BASE,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        self._logger = logger or logging.getLogger(__name__)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={
                "User-Agent": _UA,
                "Referer": "https://m.douban.com/movie/",
                "Accept": "application/json",
            },
            timeout=20,
            follow_redirects=True,
            transport=net.http_transport("cast-cn"),
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _wait_turn(self) -> None:
        """全局限 1 请求/秒，再加一点随机抖动，避免固定节奏。"""
        async with self._lock:
            now = time.monotonic()
            delay = 1.0 + random.uniform(0.1, 0.5) - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = time.monotonic()

    async def celebrities(self, douban_id: str) -> list[DoubanActor]:
        """读取一部作品的演员表；失败抛 DoubanError。"""
        douban_id = (douban_id or "").strip()
        if not douban_id:
            raise DoubanError("豆瓣 id 为空")

        last_error: Exception | None = None
        for attempt in range(3):
            await self._wait_turn()
            try:
                response = await self._client.get(
                    f"/movie/{douban_id}/celebrities", params={"for_mobile": 1}
                )
                if response.status_code in (403, 429):
                    raise DoubanError(
                        f"豆瓣拒绝了请求（HTTP {response.status_code}），稍后再试或配置代理"
                    )
                response.raise_for_status()
                data = response.json()
            except DoubanError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                raise DoubanError(f"豆瓣演职员请求失败：{exc}") from exc
            return _parse_actors(data)

        raise DoubanError(f"豆瓣演职员请求失败：{last_error}")

    async def __aenter__(self) -> DoubanClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _parse_actors(data: Any) -> list[DoubanActor]:
    if not isinstance(data, dict):
        raise DoubanError("豆瓣返回的不是 JSON 对象")
    raw_actors = data.get("actors")
    if not isinstance(raw_actors, list):
        return []

    result: list[DoubanActor] = []
    for raw in raw_actors[:_MAX_ACTORS]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        avatar = raw.get("avatar")
        if isinstance(avatar, dict):
            avatar = avatar.get("large") or avatar.get("normal") or avatar.get("small")
        result.append(
            DoubanActor(
                name=name,
                latin_name=(str(raw.get("latin_name") or "").strip() or None),
                character=(str(raw.get("character") or "").strip() or None),
                avatar_url=(str(avatar).strip() if avatar else None),
                douban_id=(str(raw.get("id") or "").strip() or None),
            )
        )
    return result
