"""公开插件自己的 OAuth 应用标识；不包含 Client Secret 或任何用户令牌。"""

# MovieClaw 项目账号维护的公开应用；这不是用户凭据，不包含 Client Secret。
DEFAULT_CLIENT_ID = "aPq49pAxnq5HM6qVYY1vmKfHl6bV6S10aotXT5crbH0"


def application_credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    """留空即使用插件应用；旧的自定义设置保留，秘密不混用到默认应用。"""
    custom_id = client_id.strip()
    if not custom_id or custom_id == DEFAULT_CLIENT_ID:
        return DEFAULT_CLIENT_ID, ""
    return custom_id, client_secret
