# SPDX-License-Identifier: GPL-3.0-or-later
"""115 网盘存储后端插件：往注册表 ``drive-backends`` 贡献 115 的协议实现。"""

from __future__ import annotations

from movieclaw_sdk import plugin
from movieclaw_sdk.drives import DRIVE_BACKENDS

from .backend import Pan115


@plugin("cloud-115", title="115 网盘")
async def apply(ctx) -> None:
    ctx.contribute(DRIVE_BACKENDS, "115", Pan115())
