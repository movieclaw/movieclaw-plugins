"""仓库 CI 的 unittest 入口；完整 pytest 回归保留在同一目录。"""


def load_tests(loader, standard_tests, pattern):
    # 官方 CI 仅安装宿主运行依赖，不能在发现 pytest 模块时隐式安装开发工具。
    # 显式载入原生加载／隐私边界的 unittest 套件；完整回归命令见 README。
    from . import ci_smoke

    return loader.loadTestsFromModule(ci_smoke)
