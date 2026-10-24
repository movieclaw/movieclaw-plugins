"""原生页面与旧管理链接共用同一份正文，避免修复只落到其中一个入口。"""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
page = (root / "pt_pack/assets/panel.html").read_text()
(root / "web/index.html").write_text(page)
(root / "icon.svg").write_bytes((root / "web/brand.svg").read_bytes())
