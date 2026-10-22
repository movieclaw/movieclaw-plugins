"""公开插件自己的 OAuth 应用标识；不包含 Client Secret 或任何用户令牌。"""

# 上架阻塞项：由 MovieClaw 维护者提供组织维护的公开 Client ID；不分发个人应用。
# 不在源码放 Client Secret。无 secret 的实际刷新/撤销仍需在该应用上单独验收。
DEFAULT_CLIENT_ID = ""


def application_credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    """留空即使用插件应用；旧的自定义设置保留，秘密不混用到默认应用。"""
    custom_id = client_id.strip()
    return (custom_id, client_secret) if custom_id else (DEFAULT_CLIENT_ID, "")
