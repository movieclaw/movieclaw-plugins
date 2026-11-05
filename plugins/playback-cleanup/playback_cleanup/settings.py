"""插件设置：全部存在插件数据里，由插件页面「设置」读写（页面是唯一入口）。

宿主的通用设置表单列不出服务器上有哪些库和成员，所以这里不作为 ``config=`` 交给宿主，
只用来校验页面提交的内容并给出默认值。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DELAY_MIN = 5
DELAY_MAX = 300
DELAY_DEFAULT = 45
#: 超管的成员 id（宿主约定）
ADMIN_MEMBER_ID = 0


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    library_id: int | None = Field(
        default=None, ge=1, description="目标媒体库；不选（None）时插件什么都不做"
    )
    member_ids: list[int] = Field(
        default_factory=list, max_length=200, description="要清续播进度的成员；0 = 超管"
    )
    delay_seconds: int = Field(
        default=DELAY_DEFAULT,
        ge=DELAY_MIN,
        le=DELAY_MAX,
        description="播放结束后延迟秒数：躲开客户端停止后的最后一次进度上报",
    )
    sweep_enabled: bool = Field(default=True, description="兜底扫描：每小时扫一遍残留的续播进度")
    enabled: bool = Field(default=True, description="启用：关掉后播放事件和定时扫描都不做事")
    dry_run: bool = Field(
        default=True,
        description="只预览：播放事件和兜底扫描只记录将会清什么，不真的写；手动「立即清理一次」不受影响",
    )

    @field_validator("member_ids")
    @classmethod
    def _members(cls, value: list[int]) -> list[int]:
        out: list[int] = []
        for raw in value:
            mid = int(raw)
            if mid < 0:
                raise ValueError("成员 id 不能为负数")
            if mid not in out:
                out.append(mid)
        return out

    @classmethod
    def load(cls, raw: Any) -> Settings:
        """从插件数据读出来的值；坏数据回落默认值，不让插件起不来。"""
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls.model_validate(raw)
        except ValueError:
            return cls()

    def selection_state(self) -> str:
        """``no_selection``（没选库）/ ``no_members`` / ``disabled`` / ``ready``。"""
        if self.library_id is None:
            return "no_selection"
        if not self.member_ids:
            return "no_members"
        if not self.enabled:
            return "disabled"
        return "ready"
