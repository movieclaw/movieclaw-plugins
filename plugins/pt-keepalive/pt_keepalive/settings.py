"""设置模型（设置页表单）。刻意保持最少：不提供站点排除与间隔配置——
启用范围 = 宿主已配置且验证通过的站点全集，周期在「设置→定时任务」改。

M-Team 是唯一需要插件自己保存凭据的站点：宿主的 API Key 调不动它的保活接口
（只认网页会话），只能账号密码 + TOTP 动态码登录。这三个字段都是 ``SecretStr``，
宿主用 SecretBox 加密落盘、界面上不回显。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from pt_keepalive.mteam import Credentials, credentials


class Settings(BaseModel):
    enabled: bool = Field(
        default=True,
        title="启用保活",
        description="关闭后定时任务跑空轮，不再访问任何站点",
    )
    signin_enabled: bool = Field(
        default=True,
        title="尝试每日签到",
        description="NexusPHP 系站点访问签到页并按页面提示判定；关闭后只确认登录态，不产生签到动作",
    )
    daily_report: bool = Field(
        default=False,
        title="每天发保活日报",
        description="每轮跑完发一条汇总通知；凭据失效告警不受此开关控制（始终发）",
    )
    mteam_username: str = Field(
        default="",
        title="M-Team 账号",
        description="馒头站点用户名。留空则不做 M-Team 保活（它的保活接口不认宿主的 API Key）",
        max_length=80,
    )
    mteam_password: SecretStr | None = Field(
        default=None,
        title="M-Team 密码",
        description="与账号一起用于登录网页会话；只在本机加密保存",
    )
    mteam_totp_secret: SecretStr | None = Field(
        default=None,
        title="M-Team 两步验证密钥",
        description=(
            "站点开了两步验证时填绑定 TOTP 的那串 Base32 密钥（otpauth:// 整条链接也可以）；"
            "没开就留空"
        ),
    )


def _secret_value(value: SecretStr | str | None) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


def mteam_credentials(config: Settings) -> Credentials | None:
    """设置里的 M-Team 凭据；账号或密码为空返回 ``None``（没配置就等于不启用）。"""
    return credentials(
        config.mteam_username,
        _secret_value(config.mteam_password),
        _secret_value(config.mteam_totp_secret),
    )
