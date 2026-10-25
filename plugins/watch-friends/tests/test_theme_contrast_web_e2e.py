"""真实同源 iframe 验证：旧宿主只传部分变量时仍保持可读，切主题也不会残留旧色。"""

from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1] / "movieclaw_friends" / "static"

HOST = """<!doctype html><style>
:root{color-scheme:light;--text:#151a23;--bg:#fafafa}
body{margin:0;background:var(--bg);color:var(--text)}
iframe{width:100%;height:800px;border:0}
</style><iframe src="/plugin/index.html"></iframe>"""
PAGE = """<!doctype html><html><head><meta name="color-scheme" content="dark">
<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="profile.css">
<link rel="stylesheet" href="host-ui.css"><script src="theme.js"></script></head>
<body><header class="site-header"><h1>映迹</h1><small class="muted">一起发现好故事</small></header>
<main id="workspace" class="workspace" data-active-tab="profile"><div class="page-heading"><div><h1>你的观影同好圈</h1><p class="muted">欢迎回来。你的分享，由你选择。</p></div></div>
<nav class="tabs"><button class="tab is-active">个人主页</button><button class="tab">我的好友</button></nav>
<section class="personal-hero"><div class="personal-identity"><div><h1>anlangao</h1><p class="muted">进入映迹</p></div></div>
<div class="personal-stats"><div class="personal-stat"><strong>1</strong><span>电影</span></div><div class="personal-stat"><strong>7</strong><span>电视剧</span></div><div class="personal-stat"><strong>28</strong><span>集</span></div></div></section>
<p class="field-help">仅本人可见</p><button class="button button-primary">保存设置</button>
<select><option>双向同步</option><option>只读取</option></select>
<dialog open class="ui-dialog"><div class="ui-dialog-head"><h2>同步详情</h2></div><div class="ui-dialog-body"><p class="muted">已完成</p><p class="watch-pending">待匹配</p></div></dialog>
</main></body></html>"""

# 把透明文字和表面叠到实际父背景上计算对比度，不能只比较 CSS 字面值。
CONTRAST = """el => {
const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
const ctx=canvas.getContext('2d',{willReadFrequently:true});
const color=(value,bg)=>{ctx.clearRect(0,0,1,1);if(bg){ctx.fillStyle=bg;ctx.fillRect(0,0,1,1);}ctx.fillStyle=value;ctx.fillRect(0,0,1,1);return Array.from(ctx.getImageData(0,0,1,1).data).slice(0,3);};
let chain=[];for(let n=el;n;n=n.parentElement)chain.unshift(n);
let bg=getComputedStyle(parent.document.body).backgroundColor;
for(const n of chain){let c=color(getComputedStyle(n).backgroundColor,bg);bg=`rgb(${c.join(',')})`;}
let fore=color(getComputedStyle(el).color,bg),back=color(bg);
const lum=c=>c.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
const a=lum(fore),b=lum(back);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
}"""


def route_assets(route):
    path = route.request.url.split("http://theme.test", 1)[1].split("?", 1)[0]
    if path == "/host":
        return route.fulfill(body=HOST, content_type="text/html")
    if path == "/plugin/index.html":
        return route.fulfill(body=PAGE, content_type="text/html")
    asset = ROOT / Path(path).name
    if not asset.is_file():
        return route.fulfill(status=404, body="missing")
    route.fulfill(
        body=asset.read_bytes(),
        content_type="text/css" if asset.suffix == ".css" else "application/javascript",
    )


def assert_readable(frame):
    for selector in (
        ".muted",
        ".field-help",
        ".tab:not(.is-active)",
        ".personal-stat span",
        ".personal-stat strong",
        ".button-primary",
        "select",
        "dialog .muted",
        ".watch-pending",
    ):
        for item in frame.locator(selector).all():
            assert item.evaluate(CONTRAST) >= 4.5, selector


def test_partial_host_palette_contrast_switching_and_transparent_frame(tmp_path):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 850}, reduced_motion="reduce"
        )
        page.route("http://theme.test/**", route_assets)
        page.goto("http://theme.test/host")
        frame = page.frames[1]
        # 模拟旧版只导出主文字和 color-scheme，其余变量缺失。
        frame.evaluate(
            "document.documentElement.style.cssText='--mc-text:#151a23;color-scheme:light'"
        )
        assert_readable(frame)
        page.screenshot(path=str(tmp_path / "light.png"))
        page.evaluate(
            "document.documentElement.style.cssText='color-scheme:dark;--text:#f3f5f9;--bg:#000000'"
        )
        frame.wait_for_function(
            "document.documentElement.dataset.mcColorScheme==='dark'"
        )
        assert_readable(frame)
        assert (
            frame.locator("body").evaluate("e=>getComputedStyle(e).backgroundColor")
            == "rgba(0, 0, 0, 0)"
        )
        page.screenshot(path=str(tmp_path / "dark.png"))
        # 回切浅色时移除旧的深色变量，不能留下上轮的白色说明字。
        page.evaluate(
            "document.documentElement.style.cssText='color-scheme:light;--text:#151a23;--bg:#fafafa'"
        )
        frame.wait_for_function(
            "document.documentElement.dataset.mcColorScheme==='light'"
        )
        assert_readable(frame)
        page.set_viewport_size({"width": 390, "height": 844})
        frame.wait_for_function("document.documentElement.scrollWidth<=innerWidth+1")
        browser.close()


def test_legacy_normal_scheme_and_standalone_canvas(tmp_path):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 850}, reduced_motion="reduce"
        )
        page.route("http://theme.test/**", route_assets)
        page.goto("http://theme.test/host")
        frame = page.frames[1]
        # 旧宿主 normal 会让浏览器选白色画布；应按深色文字 token 修正，而非当成浅色。
        page.evaluate(
            "document.documentElement.style.cssText='color-scheme:normal;--text:#f3f5f9;--bg:#111111'"
        )
        frame.wait_for_function(
            "document.documentElement.dataset.mcColorScheme==='dark'"
        )
        assert frame.locator("html").get_attribute("data-mc-opaque-canvas") is not None
        assert (
            frame.locator("html").evaluate("e=>getComputedStyle(e).backgroundColor")
            == "rgb(17, 17, 17)"
        )
        assert_readable(frame)
        # 颜色由宿主提供时应原样使用；移除后必须清除旧值，切回当前模式的回退色。
        page.evaluate(
            "document.documentElement.style.setProperty('--text-muted','#b7bdc9')"
        )
        pw.expect(frame.locator(".muted").first).to_have_css(
            "color", "rgb(183, 189, 201)"
        )
        page.evaluate("document.documentElement.style.removeProperty('--text-muted')")
        pw.expect(frame.locator(".muted").first).to_have_css(
            "color", "rgb(171, 178, 191)"
        )
        page.goto("http://theme.test/plugin/index.html")
        assert page.locator("html").get_attribute("data-mc-embedded") is None
        assert (
            page.locator("html").evaluate("e=>getComputedStyle(e).backgroundColor")
            == "rgb(10, 11, 16)"
        )
        browser.close()
