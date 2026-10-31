"""123 网盘后端的进程外注册入口。"""

from __future__ import annotations

from movieclaw_kernel import ServiceKey, Stability
from movieclaw_sdk import plugin
from movieclaw_sdk.drives import DRIVE_BACKENDS

# SDK 尚未转出此公开服务键；进程外代理按已有契约名解析，不依赖宿主模块。
PLUGIN_DATA = ServiceKey("plugin-data", stability=Stability.EXPERIMENTAL)


@plugin("cloud-123", title="123 云盘", inject=(PLUGIN_DATA,))
async def apply(ctx) -> None:
    # 清单检查与 --describe 发生在插件依赖安装之前。
    from .backend import Pan123

    backend = Pan123(ctx.use(PLUGIN_DATA).scoped(ctx))
    await backend._uploads.data.ready()
    ctx.contribute(DRIVE_BACKENDS, "123", backend)
