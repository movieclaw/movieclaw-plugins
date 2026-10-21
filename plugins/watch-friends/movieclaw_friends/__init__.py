"""映迹：由 MovieClaw 加载、使用插件私有存储的原生 Web 插件。"""

# 宿主仅登记完整隔离包名，包内需显式导入子模块，不能依赖父命名空间存在。
from .native import apply
from .version import __version__

__all__ = ["apply", "__version__"]
