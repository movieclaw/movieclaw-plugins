"""插件设置：连接飞牛、路径映射、各能力开关。"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr

from .trim_api import TrimClient


class Config(BaseModel):
    """设置页表单：连接、路径映射、各能力的开关。"""

    base_url: str = Field(
        "", title="飞牛地址", description="影视服务地址，如 http://nas:5666（带不带 /v 都行）"
    )
    username: str = Field("", title="用户名")
    password: SecretStr | None = Field(None, title="密码")
    access_code: SecretStr | None = Field(
        None, title="访问码（可选）", description="开了设备访问码才填"
    )
    verify_tls: bool = Field(
        True, title="校验证书", description="飞牛用自签 https 证书时关掉，否则连不上"
    )
    member_username: str = Field(
        "",
        title="这个飞牛账号对应哪个成员",
        description=(
            "MovieClaw 成员用户名；留空表示超管。只有这个成员的进度会写进飞牛，"
            "从飞牛拉回的进度也只记在名下"
        ),
    )
    path_map_from: str = Field(
        "",
        title="飞牛路径前缀",
        description="飞牛库目录的路径开头（如 /mnt/media/Movies）；配合下一项把库接成本地库",
    )
    path_map_to: str = Field(
        "",
        title="本机路径前缀",
        description="上面那个前缀在本机对应的路径；两个都填才接入，只填一个等于没配",
    )
    auto_adopt: bool = Field(
        True, title="自动接入新库", description="飞牛上新建的库在同步时自动建成本地媒体库"
    )
    sync_progress: bool = Field(
        True, title="同步观看进度", description="双向：MC ↔ 飞牛，两边都有的片才同步"
    )
    sync_markers: bool = Field(
        True, title="同步片头片尾到飞牛", description="MC 识别出的结论写回飞牛，按季生效"
    )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.username and self.password is not None)


def client_of(config: Config) -> TrimClient:
    return TrimClient(
        config.base_url,
        config.username,
        config.password.get_secret_value() if config.password else "",
        config.access_code.get_secret_value() if config.access_code else "",
        verify=config.verify_tls,
    )
