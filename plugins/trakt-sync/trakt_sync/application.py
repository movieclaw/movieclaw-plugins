"""公开插件自己的 OAuth 应用标识；不包含 Client Secret 或任何用户令牌。"""

DEFAULT_CLIENT_ID = "I29BaLQkaaqx3rE86POqLek5BO5kEvRw6ZD4zDD2ZP8"


def application_credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    """留空即使用插件应用；旧的自定义设置保留，秘密不混用到默认应用。"""
    custom_id = client_id.strip()
    return (custom_id, client_secret) if custom_id else (DEFAULT_CLIENT_ID, "")
