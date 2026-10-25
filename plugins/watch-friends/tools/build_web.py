"""生成宿主可托管的原生插件网页；静态源文件保持唯一，避免双份页面逐渐分叉。"""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "movieclaw_friends" / "static"
TARGET = ROOT / "web"
FILES = (
    "index.html",
    "app.js",
    "style.css",
    "feed.js",
    "artwork.js",
    "feed.css",
    "community.js",
    "network.js",
    "availability.js",
    "availability.css",
    "info-help.js",
    "settings-ui.js",
    "icons.js",
    "profile.js",
    "profile.css",
    "host-ui.css",
    "ui.js",
    "alignment.js",
    "sync-card.js",
    "auto-sync.js",
    "resume-sync.js",
    "tasks.js",
    "display-policy.js",
    "manifest.webmanifest",
    "icon.svg",
    "icon-mask.svg",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
    "lucide-LICENSE.txt",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查 web 产物是否与源文件一致")
    args = parser.parse_args()
    stale = []
    names = list(FILES) + [
        path.relative_to(SOURCE).as_posix() for path in sorted((SOURCE / "avatars").glob("*.svg"))
    ]
    for name in names:
        content = (SOURCE / name).read_bytes()
        destination = TARGET / name
        if args.check:
            if not destination.is_file() or destination.read_bytes() != content:
                stale.append(name)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    if stale:
        raise SystemExit(
            "原生网页产物需要更新：" + "、".join(stale) + "；请运行 python3 tools/build_web.py"
        )
    print("原生网页产物已同步。" if not args.check else "原生网页产物检查通过。")


if __name__ == "__main__":
    main()
