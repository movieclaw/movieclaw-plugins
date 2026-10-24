"""插件自己发的事件与载荷（清单 ``[[events]]`` / ``[[notifications]]`` 与这里一一对应）。

单独成模块：入口模块要 ``register``、保活轮要 ``publish``，放这里两边都能导入而不成环。
"""

from __future__ import annotations

from movieclaw_sdk.events import plugin_event
from pydantic import BaseModel, ConfigDict, Field

PLUGIN_ID = "pt-keepalive"


class AuthExpired(BaseModel):
    """凭据失效事件的载荷（通知模板用 ``{site_name}``）。"""

    model_config = ConfigDict(frozen=True)

    site_id: str
    site_name: str


class DailyReport(BaseModel):
    """保活日报事件的载荷。

    ``detail`` 是给通知正文用的一行汇总——通知模板只做字段替换、不会拼接数组，
    所以连续失败明细在这里先拼好，``failures`` 保留结构化形式给 Webhook / 别的插件。
    """

    model_config = ConfigDict(frozen=True)

    ok_count: int
    signin_count: int
    failed_count: int
    human_count: int
    skipped_count: int = 0
    failures: list[str] = Field(default_factory=list)
    detail: str = ""


AUTH_EXPIRED = plugin_event(
    f"{PLUGIN_ID}.auth.expired", AuthExpired, title="PT 站点登录失效"
)
DAILY_REPORT = plugin_event(f"{PLUGIN_ID}.report.daily", DailyReport, title="PT 保活日报")
#: 连续失败升级：日报默认关闭，失败不能靠它兜底，所以单独一条默认开
FAILURE_REPORT = plugin_event(
    f"{PLUGIN_ID}.report.failures", DailyReport, title="PT 站点保活连续失败"
)
