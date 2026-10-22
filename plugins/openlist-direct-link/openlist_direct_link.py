"""OpenList 直链：strm 指向网盘挂载里的文件时，让播放器直接从网盘取。

Symedia、CloudDrive2 一类工具生成的 strm 里写的是挂载路径（``/cd2/CloudDrive/115/电影/a.mkv``），
默认由 MovieClaw 读挂载再推给播放器，视频流量经过服务器两遍。同一个网盘也挂在 OpenList（alist）
里时，本插件按路径规则把挂载路径换成 OpenList 里的路径，向 OpenList 要这个文件的签名，交出
``/d/<路径>?sign=…``——OpenList 自己的 302 地址，115 一类存储会跳到网盘 CDN。

OpenList 在内网也没关系：MovieClaw 会带着播放器的 User-Agent 替它跟跳转，落到网盘 CDN 再交给
播放器（115 直链与 UA 绑定，这样拿到的才是播放器能用的）。跟跳转落不到外网（OpenList 开了本地
代理、自己出字节）、OpenList 连不上、路径不在规则里，MovieClaw 都照旧由服务器出字节，播放不受影响。

用到的扩展点：决策钩子 ``playback.strm.direct-link``（MovieClaw 0.39 起；只读查询，不改任何东西）。
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, SecretStr

from movieclaw_api.hooks import STRM_DIRECT_LINK, DirectLink, MountedFile
from movieclaw_kernel import Context, plugin
from movieclaw_sdk import net

SERVICE = "openlist-direct-link"


class Config(BaseModel):
    base_url: str = Field(
        default="",
        title="OpenList 地址",
        description="例如 http://192.168.1.10:5244；内网地址也行",
    )
    token: SecretStr = Field(
        default=SecretStr(""),
        title="OpenList 令牌",
        description="OpenList 设置 → 其他 → 令牌；访客能读的存储可以留空",
    )
    rules: list[str] = Field(
        default_factory=list,
        title="路径规则",
        description="每条一行：挂载路径 => OpenList 里的同一个目录，例如 /cd2/CloudDrive/115 => /115",
    )
    ttl_s: int = Field(
        default=300,
        ge=0,
        le=3600,
        title="直链复用时长（秒）",
        description="同一文件、同一播放器多久内复用同一条直链；应短于网盘直链本身的有效期",
    )


def parse_rules(lines: list[str]) -> list[tuple[str, str]]:
    """``挂载路径 => OpenList 路径`` → (挂载路径, OpenList 路径)；写错的行跳过。"""
    rules = []
    for line in lines:
        mount, sep, remote = (part.strip() for part in line.partition("=>"))
        if sep and mount.startswith("/") and remote.startswith("/"):
            # 尾部斜杠去掉；映射到 OpenList 根目录（/）时 remote 成了空串，拼接照样对
            rules.append((mount.rstrip("/"), remote.rstrip("/")))
    return rules


def openlist_path(path: str, rules: list[tuple[str, str]]) -> str | None:
    """挂载路径 → OpenList 里的路径（最长前缀优先，按整段目录匹配）；不在任何规则里返回 None。

    MovieClaw 给的是展开软链后的真实路径，规则按用户写的挂载路径配：两种写法都认。
    """
    best: tuple[int, str] | None = None
    for mount, remote in rules:
        for prefix in {mount, os.path.realpath(mount)}:
            if (path == prefix or path.startswith(prefix + "/")) and (
                best is None or len(prefix) > best[0]
            ):
                best = (len(prefix), remote + path[len(prefix) :])
    return best[1] if best else None


async def direct_link(
    client: httpx.AsyncClient, config: Config, rules: list[tuple[str, str]], file: MountedFile
) -> DirectLink | None:
    """查 OpenList，给出这个挂载文件的签名下载地址；不认领、查不到返回 None。"""
    remote = openlist_path(file.path, rules)
    if remote is None:
        return None
    base = config.base_url.rstrip("/")
    response = await client.post(
        f"{base}/api/fs/get",
        json={"path": remote, "password": ""},
        headers={"Authorization": config.token.get_secret_value()},
    )
    body = response.json()
    if body.get("code") != 200:
        raise LookupError(f"OpenList 找不到 {remote}：{body.get('message')}")
    sign = (body.get("data") or {}).get("sign") or ""
    url = f"{base}/d{quote(remote)}" + (f"?sign={sign}" if sign else "")
    return DirectLink(url=url, ttl_s=config.ttl_s)


@plugin("openlist-direct-link", title="OpenList 直链", config=Config)
async def openlist_direct_link(ctx: Context[Config]) -> None:
    config = ctx.config
    rules = parse_rules(config.rules)
    if not config.base_url or not rules:
        ctx.logger.info("还没填 OpenList 地址或路径规则：到插件详情页填好后生效")
        return
    client = httpx.AsyncClient(transport=net.http_transport(SERVICE), timeout=1.5)
    ctx.effect(client.aclose, label="openlist-client")

    async def on_mounted(file: MountedFile) -> DirectLink | None:
        try:
            return await direct_link(client, config, rules, file)
        except (httpx.HTTPError, ValueError, LookupError) as exc:
            # 不认领：MovieClaw 照旧由服务器出字节
            ctx.logger.warning("OpenList 直链没取到，照旧由服务器出字节：%s", exc)
            return None

    ctx.on(STRM_DIRECT_LINK, on_mounted)
