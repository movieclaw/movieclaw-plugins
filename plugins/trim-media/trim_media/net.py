"""飞牛影视服务的出网通道：统一走 SDK 的 ``net.http_transport``。

插件在独立进程里运行时，代理与出网审计由宿主提供；这里只做一层转发，
避免各模块各写一份。**注意要转导出**：只 ``from movieclaw_sdk import net``
的话，``trim_media.net`` 上并没有 ``http_transport``（踩过：报
``module 'trim_media.net' has no attribute 'http_transport'``）。
"""

from __future__ import annotations

from movieclaw_sdk.net import http_transport, proxy_url

__all__ = ["http_transport", "proxy_url"]
