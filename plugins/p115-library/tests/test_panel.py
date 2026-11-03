"""Panel static sanity: JS syntax + element ID cross-check.

The 1.7.1 regression (a missing ')' in playTest) shipped because nothing
validated the inline script: a single SyntaxError kills the whole <script>,
which takes down QR login and page navigation -- exactly what the user hit.
These tests make that class of failure impossible to ship again.
"""
import re,shutil,subprocess,tempfile,os
import pytest
import panel as panel_mod

@pytest.fixture(scope='module')
def split():
    m=re.search(r'<script>(.*)</script>',panel_mod.PANEL,re.S)
    assert m,'面板缺少内联 script'
    js=m.group(1)
    rest=panel_mod.PANEL.replace(m.group(0),'<script></script>')
    return js,rest

def test_script_js_syntax(split):
    js,_=split
    node=shutil.which('node')
    if not node:pytest.skip('node 不可用，跳过 JS 语法检查')
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False) as f:
        f.write(js);path=f.name
    try:
        r=subprocess.run([node,'--check',path],capture_output=True,text=True,timeout=30)
        assert r.returncode==0,'面板 JS 语法错误（浏览器中将导致整页交互失效）:\n'+r.stderr
    finally:os.unlink(path)

def test_script_element_ids_exist(split):
    js,rest=split
    js_ids=set(re.findall(r"querySelector(?:All)?\('#([\w-]+)'\)",js))
    html_ids=set(re.findall(r'id="([\w-]+)"',rest))
    missing=js_ids-html_ids
    assert not missing,'JS 引用了 HTML 中不存在的元素 ID: %s'%sorted(missing)

def test_pages_fields_have_controls(split):
    """Every field the JS PAGES map declares must have a matching
    data-field control in the HTML, otherwise loadPage crashes."""
    js,rest=split
    m=re.search(r'const PAGES=\{(.+?)\};',js,re.S)
    assert m,'面板缺少 PAGES 映射'
    declared=set(re.findall(r"'([\w_]+)'",m.group(1)))-{'mappings'}
    controls=set(re.findall(r'data-field="([\w_]+)"',rest))
    missing=sorted(declared-controls)
    assert not missing,'PAGES 声明的字段缺少面板控件（loadPage 将崩溃）: %s'%missing
    # every field with a control must also be loadable/saveable via PAGES
    orphan=sorted(controls-declared-{'mappings'})
    assert not orphan,'面板控件未在 PAGES 声明（保存/读取将丢失）: %s'%orphan

def test_dual_mount_base_and_theme_follow(split):
    """2.2.0：同一份 HTML 双挂载——老 /panel 与插件网页静态托管 /web/*。

    iframe 场景的 API 前缀推导必须两种路径都成立，否则网页里所有请求 404。"""
    js,rest=split
    assert '/(panel\\/?|web(\\/.*)?)$/' in js,'base 正则须同时匹配 /panel 与 /web/* 两种挂载路径'
    assert 'IN_APP' in js and "classList.add('in-app')" in js,'缺少 iframe 场景检测（主题跟随与 hash 路由都依赖它）'
    assert 'html.in-app body{background:transparent}' in rest,'iframe 场景背景必须透明以透出 MovieClaw 外框'
    assert '--mc-text' in rest,'应映射宿主 --mc-* 主题变量（带兜底值）'
    assert re.search(r"history\.replaceState\(null,'','#'\+p\)",js),'go() 须写 hash（宿主地址栏同步依赖它）'
    assert re.search(r"location\.hash\.slice\(1\)",js),'boot 须读取初始 hash（ #full 这类地址直达对应页面）'

def test_single_sidebar_entry_no_shortcuts():
    """2.4.1：侧边栏只留一个入口「115网盘」→ /apps/p115-library。

    多出来的 shortcuts（全量同步/网盘文件/运行日志）会被宿主渲染成额外入口，
    用户明确表示不需要——网页内部的左侧导航已经能到同样的页面。
    hash 落点能力保留（上面一条用例守着），只是不再向宿主声明额外入口。"""
    import json,pathlib
    web=pathlib.Path(__file__).parent.parent/'web'
    m=json.loads((web/'manifest.webmanifest').read_text())
    assert not m.get('shortcuts'),'侧边栏只保留一个入口：manifest 不得再声明 shortcuts'
    assert m.get('short_name')=='115网盘','唯一入口名称固定为 115网盘'
    assert m.get('start_url')=='.','唯一入口须落在网页根'

def test_web_manifest_and_icon():
    """2.2.0 插件网页契约（movieclaw plugin-web-pages 设计稿）：web/ 目录、
    标准 Web App Manifest、monochrome 图标。宿主侧边栏只读这些字段。"""
    import json,pathlib
    web=pathlib.Path(__file__).parent.parent/'web'
    assert (web/'manifest.webmanifest').is_file(),'插件网页必须有标准 Web App Manifest'
    m=json.loads((web/'manifest.webmanifest').read_text())
    assert m.get('short_name') and len(m['short_name'])<=8,'侧边栏文字 short_name 建议 ≤8 个汉字'
    assert m.get('start_url')=='.','start_url 应指向网页根（web/index.html）'
    mono=[i for i in m.get('icons',[]) if 'monochrome' in (i.get('purpose') or '')]
    assert mono,'图标须声明 purpose=monochrome（侧边栏遮罩渲染）'
    icon=pathlib.Path(__file__).parent.parent/'web'/mono[0]['src']
    assert icon.is_file(),'monochrome 图标文件缺失'
    assert icon.read_text().lstrip().startswith('<svg'),'图标须为 SVG'

def test_panel_exposes_rate_limit_controls_and_cooldown(split):
    """2.4.0：撞上 115 限流时同步其实在等冷却。

    没有「剩余等待」这一行，用户只能看见计数器不动——这正是本次反馈的现象；
    同时新增的两个配置项必须能被 load/save 读到（PAGES + NUM 覆盖）。"""
    js,rest=split
    assert 'throttle_wait' in js and '115 限速冷却中' in js,'状态区必须提示正在等待 115 冷却，否则看起来像卡死'
    assert 'retry_cooldown' in js and 'retry_max' in js,'缺少限流等待配置项'
    nums=re.search(r'const NUM=new Set\(\[(.+?)\]\)',js)
    assert nums and 'retry_cooldown' in nums.group(1) and 'retry_max' in nums.group(1),'新配置项须按数值读回，否则保存后变成字符串被宿主拒绝'

def test_panel_exposes_stop_and_clear_checkpoint(split):
    """2.6.0：面板必须提供「停止同步」与「清理断点」入口，且接口路径正确。

    停止按钮依赖 /strm/stop 让大库同步可被打断；清理断点依赖
    /strm/clear-checkpoint 让用户能触发一次从头完整扫描。"""
    js,rest=split
    assert 'stopSync' in js and '/strm/stop' in js,'缺少停止同步按钮/接口'
    assert 'clearCheckpoint' in js and '/strm/clear-checkpoint' in js,'缺少清理断点按钮/接口'
    assert 'btn-stop-full' in rest and 'btn-stop-incr' in rest,'全量/增量页都要有停止按钮'
    assert '断点续传' in rest,'全量页需说明断点续传与清理断点的用法'
    assert 'checkpoint_dirs' in js and '断点记录' in js,'状态区须显示断点记录数（排查「不跳过」的关键）'

def test_web_index_generated_from_panel():
    """web/index.html 由打包脚本从 panel.PANEL 生成——同一份 HTML 双挂载，
    不允许出现第二份手工副本漂移。这里验证生成物与常量逐字节一致。"""
    import pathlib
    web=pathlib.Path(__file__).parent.parent/'web'/'index.html'
    if not web.is_file():pytest.skip('打包前生成，源码树无此文件')
    assert web.read_text()==panel_mod.PANEL,'web/index.html 必须与 panel.PANEL 完全一致（由打包脚本生成）'


def test_every_settings_row_has_exactly_label_and_one_content_column():
    """2.7.8: a note appended next to .ctrl (instead of inside a wrapper div)
    became a third grid item and broke the two-column row layout."""
    from html.parser import HTMLParser
    class P(HTMLParser):
        def __init__(self):
            super().__init__();self.stack=[];self.bad=[]
        def handle_starttag(self,tag,attrs):
            if tag in ('br','img','input','meta','link','hr','source'):  # void
                if self.stack:self.stack[-1]['kids'].append(tag)
                return
            cls=dict(attrs).get('class') or ''
            if self.stack:self.stack[-1]['kids'].append(tag)
            self.stack.append({'tag':tag,'row':tag=='div' and 'row' in cls.split(),'kids':[],'pos':self.getpos()})
        def handle_endtag(self,tag):
            while self.stack:
                node=self.stack.pop()
                if node['row'] and len(node['kids'])!=2:self.bad.append((node['pos'],node['kids']))
                if node['tag']==tag:break
    html=panel_mod.PANEL
    html=html[:html.index('<script>')]+html[html.index('</script>')+9:]
    p=P();p.feed(html)
    assert not p.bad,'以下 .row 不是「label + 一个内容 div」两列结构（会歪）：%r'%p.bad[:5]


def test_usage_guide_page_in_console():
    """2.8.2: the usage guide lives in the console (sidebar entry + page),
    stating that STRM can only be written inside a MovieClaw media library."""
    html=panel_mod.PANEL
    assert 'data-page="guide"' in html and 'id="page-guide"' in html and "guide:'使用说明'" in html
    i=html.index('id="page-guide"');page=html[i:html.index('</section>',i)]
    assert '新建媒体库' in page and '媒体库目录' in page and '写入被拒绝' in page and '手动扫描' in page
    assert "go('guide')" in html[html.index('id="page-full"'):],'全量同步页的提示应能跳到使用说明'
    assert "if(!BOOT_HASH&&!v.cloud_cid&&!v.mappings)go('guide')" in html,'未配置时首次打开直接显示使用说明'


def test_sync_status_escapes_server_strings(split):
    """2.8.3: renderSyncStatus writes innerHTML; every server/115-derived
    string (error text, throttle reason, cron, state...) must go through esc()
    so a crafted file name or error message cannot inject markup."""
    js,_=split
    m=re.search(r"function esc\(v\)\{(.+?)\}\n",js)
    assert m and all(c in m.group(1) for c in ('&amp;','&lt;','&gt;','&quot;','&#39;')),'缺少 HTML 转义函数 esc()'
    body=re.search(r'function renderSyncStatus\(el,s\)\{(.+?)\nasync function refreshSync',js,re.S).group(1)
    for field in ('error','life_error','scan_notice','throttle_reason','cron','state','checkpoint_dirs','throttle_limit'):
        assert 'esc(s.'+field in body or 'esc(stateNames[s.state]||s.state' in body and field=='state','%s 未转义'%field
    raw=[x for x in re.findall(r'\$\{([^}`]*)\}',body) if not x.startswith('esc(') and x!="s.mode==='full'?'全量':'增量'"]
    assert not raw,'状态区存在未转义的插值: %r'%raw
