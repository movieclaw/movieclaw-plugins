"""跨用户、跨服务器共享的媒体白名单，不携带图片域名、宿主地址或播放凭据。"""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

_POSTER_PATH = re.compile(r"/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)
_TMDB_IMAGE_PATH = re.compile(r"/t/p/(?:w[0-9]{1,4}|original)(/.+)")


def safe_poster_path(value: object) -> str | None:
    """只接受 TMDB 单文件相对路径；不解码百分号、不接受子目录或 URL。

    前端将此路径交给本机宿主的 TMDB 图片入口，不能从对端指定图片服务地址。
    显式限制文件名与扩展名也拦住目录穿越、查询凭据、脚本及本地资产路径。
    """
    return value if isinstance(value, str) and _POSTER_PATH.fullmatch(value) else None


class ArtworkImage(BaseModel):
    """选择的图片只携带 TMDB 文件名和版式，不允许来源 URL 或服务器凭据。"""

    model_config = {"extra": "forbid"}
    path: str = Field(max_length=256)
    type: Literal["backdrop", "poster"]

    @field_validator("path")
    @classmethod
    def valid_path(cls, value):
        if not safe_poster_path(value):
            raise ValueError("请选择有效的 TMDB 图片")
        return value


def _official_poster_path(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 1024 or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "image.tmdb.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            return None
        match = _TMDB_IMAGE_PATH.fullmatch(parsed.path)
    except ValueError:
        return None
    return safe_poster_path(match[1]) if match else None


def sanitize_media(value: dict) -> dict:
    """保留最小公开影片信息，海报只输出相对路径，详情留给本人浏览器按需查询。

    宿主 history 当前没有 tmdb_id，不能拿它的 media_item_id 代替：后者只在
    原服务器有意义。完整简介、演职员等详情不进入共享快照，由本人浏览器按需读取。
    """
    fields = (
        "media_item_id",
        "tmdb_id",
        "imdb_id",
        "kind",
        "title",
        "year",
        "season_number",
        "episode_number",
        "episode_title",
    )
    result = {key: value[key] for key in fields if key in value}
    result["poster_path"] = safe_poster_path(value.get("poster_path")) or _official_poster_path(
        value.get("poster_url")
    )
    cover = safe_poster_path(value.get("cover_path"))
    if cover and value.get("cover_type") in ("backdrop", "poster"):
        result.update(cover_path=cover, cover_type=value["cover_type"])
    images = []
    for image in (
        value.get("cover_images", [])[:9] if isinstance(value.get("cover_images"), list) else []
    ):
        if (
            isinstance(image, dict)
            and safe_poster_path(image.get("path"))
            and image.get("type") in ("backdrop", "poster")
            and not any(row["path"] == image["path"] for row in images)
        ):
            images.append({"path": image["path"], "type": image["type"]})
    if images:
        result["cover_images"] = images
        result.update(cover_path=images[0]["path"], cover_type=images[0]["type"])
    return result
