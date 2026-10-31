"""插件配置模型：设置页由 MovieClaw 根据这些字段自动生成。

枚举值直接用中文（主程序设置表单把枚举值当标签渲染，不支持单独的显示名）；
旧版本存的英文值经 before 校验器迁移，保证升级不丢配置。
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class Condition(StrEnum):
    """哪些演员需要处理。值即设置页显示的中文标签。"""

    ALL = "全部"
    NAME = "演员非中文"
    ROLE = "角色非中文"


#: 旧版英文值 → 新中文值（升级迁移）
_LEGACY_CONDITION = {
    "all": Condition.ALL,
    "name": Condition.NAME,
    "role": Condition.ROLE,
}


class AvatarPolicy(StrEnum):
    """头像来源策略。值即设置页显示的中文标签。"""

    TMDB_FIRST = "保留现有头像"
    DOUBAN_FIRST = "豆瓣优先"
    NONE = "不改动头像"


_LEGACY_AVATAR_POLICY = {
    "tmdb_first": AvatarPolicy.TMDB_FIRST,
    "douban_first": AvatarPolicy.DOUBAN_FIRST,
    "none": AvatarPolicy.NONE,
}


class Settings(BaseModel):
    enabled: bool = Field(
        default=True,
        title="启用插件",
        description="关闭后不再响应入库事件和定时补跑，已在跑的任务会结束。",
    )
    libraries: list[str] = Field(
        default_factory=list,
        title="媒体库",
        description=(
            "只处理这些库；填媒体库名称（如：电影、国产剧、电视4），也兼容数字 id；"
            "留空 = 全部电影/剧集库。库名见「设置 → 媒体库」或 mclaw library list。"
        ),
    )
    condition: Condition = Field(
        default=Condition.ALL,
        title="处理条件",
        description=(
            "全部 = 姓名或角色有一个非中文就处理；"
            "演员非中文 = 只看姓名；角色非中文 = 只看角色。"
        ),
    )
    remove_non_chinese: bool = Field(
        default=False,
        title="删除仍无中文的演员",
        description="处理完仍然没有中文名/角色的演员，是否从条目演员表里移除。默认保留。",
    )
    remove_no_avatar: bool = Field(
        default=False,
        title="删除无头像的演员",
        description=(
            "处理完（含豆瓣头像补齐）仍然没有任何头像的演员，是否从条目演员表里移除。"
            "默认保留。与「删除仍无中文的演员」配合可实现只留有头像且有中文信息的演员。"
        ),
    )
    force_name: bool = Field(
        default=False,
        title="强制覆盖中文名",
        description="关闭时只补空值/非中文名；打开后连已有中文名也按豆瓣覆盖。",
    )
    force_role: bool = Field(
        default=False,
        title="强制覆盖中文角色",
        description="关闭时只补空值/非中文角色；打开后连已有中文角色也按豆瓣覆盖。",
    )
    avatar_policy: AvatarPolicy = Field(
        default=AvatarPolicy.TMDB_FIRST,
        title="头像策略",
        description=(
            "保留现有头像 = 优先用 TMDB/本地头像，缺了才用豆瓣；"
            "豆瓣优先 = 优先豆瓣头像；不改动头像 = 完全不碰头像。"
        ),
    )
    public_base_url: str = Field(
        default="",
        title="外部访问地址",
        description=(
            "本应用的完整外部地址，如 https://movieclaw.example.com。"
            "留空时读「设置 → 应用设置 → 外部访问地址」。"
        ),
    )
    douban_api_base_url: str = Field(
        default="https://m.douban.com/rexxar/api/v2",
        title="豆瓣接口地址",
        description="需要反代或镜像时修改；留空使用默认地址。",
    )
    delay_seconds: int = Field(
        default=0,
        ge=0,
        le=3600,
        title="入库延迟（秒）",
        description="收到新片事件后等多久再处理；元数据还没刮完时任务会自动重试。",
    )
    max_items_per_run: int = Field(
        default=100,
        ge=1,
        le=1000,
        title="每轮最多处理",
        description="定时补跑一轮最多排多少条；首次全库回填可以调大或分几次手动跑。",
    )
    batch_size: int = Field(
        default=6,
        ge=1,
        le=50,
        title="每批并发显示",
        description=(
            "活动中心同时可见的本插件任务数：一批处理完并从活动里消失后，才排下一批。"
            "调小更清爽，调大吞吐更高。"
        ),
    )
    dry_run: bool = Field(
        default=False,
        title="只预览不写入",
        description="打开后只计算差异、写报告，不改 NFO、不触发刷新。",
    )
    keep_person_links: bool = Field(
        default=True,
        title="保留人物页链接",
        description=(
            "改名后是否继续写 <tmdbid>。保留时点演员卡会进 TMDB 人物页（姓名可能仍是英文）；"
            "关闭后改名演员不再可点，避免跳到不一致的人物页。"
        ),
    )

    @field_validator("condition", mode="before")
    @classmethod
    def _migrate_condition(cls, value):  # noqa: ANN001 - pydantic validator
        """旧版存的英文值迁移为中文值。"""
        if isinstance(value, str):
            return _LEGACY_CONDITION.get(value.strip(), value.strip() or value)
        return value

    @field_validator("avatar_policy", mode="before")
    @classmethod
    def _migrate_avatar_policy(cls, value):  # noqa: ANN001 - pydantic validator
        if isinstance(value, str):
            return _LEGACY_AVATAR_POLICY.get(value.strip(), value.strip() or value)
        return value

    @field_validator("public_base_url", "douban_api_base_url")
    @classmethod
    def _valid_http_url(cls, value: str, info) -> str:  # noqa: ANN001 - pydantic validator
        value = (value or "").strip().rstrip("/")
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("必须是 http:// 或 https:// 开头的完整地址")
        return value

    def library_names(self) -> list[str]:
        """配置的媒体库条目（名称或 id 字符串），去空白去空项。"""
        return [str(raw).strip() for raw in self.libraries if str(raw).strip()]
