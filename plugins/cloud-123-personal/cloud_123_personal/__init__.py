"""独立的 123 个人账号协议适配器，扫描契约缺口见 README。"""
from movieclaw_sdk import plugin
from movieclaw_api.plugins.keys import PLUGIN_DATA
from movieclaw_sdk.drives import DRIVE_BACKENDS

from .backend import Personal123


@plugin("cloud-123-personal", title="123 云盘个人账号（实验版）", permissions=(), inject=(PLUGIN_DATA,))
async def apply(ctx):
    backend = Personal123(store=ctx.use(PLUGIN_DATA).scoped(ctx))
    ctx.effect(backend.aclose)
    ctx.contribute(DRIVE_BACKENDS, "123-personal", backend)
