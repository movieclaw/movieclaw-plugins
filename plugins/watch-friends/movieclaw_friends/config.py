"""插件配置只来自自身环境，不导入或修改 MovieClaw 的配置与数据库。"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def origin_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError("插件公开地址格式无效") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("插件公开地址必须是完整的 HTTP(S) 源地址，不包含路径或凭据")
    try:
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("插件公开地址的域名或端口无效") from exc
    if ":" in host:
        host = f"[{host}]"
    # 浏览器 Origin 会省略默认端口；规范化后同源校验和跨服握手使用相同表示。
    suffix = f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    return f"{parsed.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class Settings:
    """每个插件实例连接一个 MovieClaw；所有新增数据写入独立 data_dir。"""

    upstream_url: str
    public_url: str
    data_dir: Path
    admin_token: str = ""
    cookie_secure: bool = False
    allow_private_peers: bool = False
    demo_enabled: bool = True
    native: bool = False
    federation_endpoints: dict[str, str] = field(default_factory=dict)
    network_endpoint: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        upstream = os.environ.get("MOVIECLAW_URL", "").strip().rstrip("/")
        public = os.environ.get("FRIENDS_PUBLIC_URL", "").strip()
        if not upstream or not public:
            raise ValueError("请先配置 MOVIECLAW_URL 和 FRIENDS_PUBLIC_URL")
        parsed = urlsplit(upstream)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MOVIECLAW_URL 必须是有效的 HTTP(S) 地址，不能包含凭据")
        return cls(
            upstream_url=upstream,
            public_url=origin_url(public),
            data_dir=Path(os.environ.get("FRIENDS_DATA_DIR", "./data")),
            admin_token=os.environ.get("MOVIECLAW_ADMIN_TOKEN", ""),
            cookie_secure=os.environ.get("FRIENDS_COOKIE_SECURE", "false").lower() == "true",
            allow_private_peers=os.environ.get("FRIENDS_ALLOW_PRIVATE_PEERS", "false").lower()
            == "true",
            demo_enabled=os.environ.get("FRIENDS_DEMO_ENABLED", "true").lower() == "true",
        )
