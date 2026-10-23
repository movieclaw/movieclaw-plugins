"""从 SVG 母版导出移动端图标；仅开发时使用 Playwright，插件运行无图像依赖。"""

import base64
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "movieclaw_friends" / "static"


def full_bleed(source):
    """系统会裁切桌面图标：背景铺满，字形仍位于中央安全区。"""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.fromstring(source)
    for element in list(tree):
        if element.get("id") == "yingji-tile":
            element.attrib.update(x="0", y="0", width="512", height="512", rx="0")
        elif element.get("id") == "yingji-edge":
            tree.remove(element)
    return ET.tostring(tree, encoding="unicode")


def main():
    from playwright.sync_api import sync_playwright

    source = (SOURCE / "icon.svg").read_text()
    variants = (
        ("icon-192.png", 192, source),
        ("icon-512.png", 512, source),
        ("apple-touch-icon.png", 180, full_bleed(source)),
        ("icon-maskable-512.png", 512, full_bleed(source)),
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for filename, size, svg in variants:
                page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
                uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
                page.set_content(
                    '<style>html,body{margin:0;background:transparent}img{display:block;width:100vw;height:100vh}</style>'
                    f'<img src="{uri}" alt="">'
                )
                page.locator("img").evaluate("image => image.decode()")
                page.locator("img").screenshot(path=str(SOURCE / filename), omit_background=True)
                page.close()
                print(f"已导出 {filename}（{size} × {size}）")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
