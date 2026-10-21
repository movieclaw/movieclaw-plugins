"""打包原生插件运行文件，排除测试、缓存、旧安装包和本地部署资料。"""

import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "tools/build_web.py"), "--check"], check=True)
    manifest = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text())
    output = ROOT / "dist" / f"{manifest['plugin']['id']}-{manifest['plugin']['version']}.mcplugin"
    output.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="watch-friends-package-") as directory:
        stage = Path(directory)
        for name in ("movieclaw_friends", "web"):
            shutil.copytree(
                ROOT / name,
                stage / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        for name in ("movieclaw-plugin.toml", "README.md"):
            shutil.copy2(ROOT / name, stage / name)
        subprocess.run(
            ["mclaw", "plugin", "pack", str(stage), "--output-file", str(output)], check=True
        )


if __name__ == "__main__":
    main()
