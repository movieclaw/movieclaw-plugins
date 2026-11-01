PANEL = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>115 网盘与 STRM 媒体库</title><style>
:root{color-scheme:dark;--blue:#7fb0ff;--blue-bg:rgba(127,176,255,.12);--green:#4ade80;--orange:#f5c451;--red:#ff6b6b;
--border:rgba(255,255,255,.08);--bg:#000;--input:rgba(0,0,0,.25);--text:#f3f5f9;--muted:rgba(243,245,249,.62);--faint:rgba(243,245,249,.36);
--accent:#cdd6e6;--accent-strong:#eef2f8;--accent-soft:rgba(205,214,230,.14);--accent-ring:rgba(200,218,255,.55);
--glass-fill:rgba(255,255,255,.04);--glass-fill-hover:rgba(255,255,255,.075);--glass-fill-active:rgba(255,255,255,.12);
--surface-sidebar:rgba(22,22,24,.55);--surface-raised:rgb(21,23,29)}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.22) transparent}
html.in-app{--text:var(--mc-text,#f3f5f9);--muted:var(--mc-text-muted,rgba(243,245,249,.62));--faint:var(--mc-text-faint,rgba(243,245,249,.36));--accent:var(--mc-accent,#cdd6e6);--accent-strong:var(--mc-accent-strong,#eef2f8);--border:var(--mc-line,rgba(255,255,255,.08));--surface-raised:var(--mc-surface-raised,rgb(21,23,29));--glass-fill:var(--mc-glass-fill,rgba(255,255,255,.04));--glass-fill-hover:var(--mc-glass-fill-hover,rgba(255,255,255,.075))}
html.in-app body{background:transparent}
*::-webkit-scrollbar{width:8px;height:8px}*::-webkit-scrollbar-thumb{background:rgba(255,255,255,.22);border-radius:4px}*::-webkit-scrollbar-track{background:transparent}
[hidden]{display:none!important}
body{background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,system-ui,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;margin:0}
.layout{display:flex;min-height:100vh}
aside.side{width:212px;background:var(--surface-sidebar);backdrop-filter:blur(20px) saturate(1.2);-webkit-backdrop-filter:blur(20px) saturate(1.2);border-right:1px solid var(--border);padding:10px;flex-shrink:0;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-size:15px;font-weight:600;padding:10px 12px 14px;white-space:nowrap;color:var(--text)}
.nav-group{margin-bottom:2px}
.gtitle{display:flex;align-items:center;gap:9px;padding:9px 12px;border-radius:8px;cursor:pointer;color:var(--muted);font-size:13px;user-select:none}
.gtitle:hover{background:var(--glass-fill)}
.nav-group.open>.gtitle{color:var(--text)}
.chev{margin-left:auto;color:var(--faint);transition:transform .15s;display:inline-flex}
.nav-group.open .chev{transform:rotate(180deg)}
.nav-items{display:none;padding:2px 0 4px}
.nav-group.open .nav-items{display:block}
.nav-item,.nav-link{display:flex;align-items:center;gap:9px;padding:8px 12px 8px 22px;border-radius:8px;cursor:pointer;color:var(--muted);font-size:13px;user-select:none;margin-bottom:2px}
.nav-link{padding-left:12px}
.nav-item:hover,.nav-link:hover{background:var(--glass-fill-hover)}
.nav-item.active,.nav-link.active{color:var(--text);background:var(--accent-soft);box-shadow:inset 0 0 0 1px rgba(205,214,230,.28)}
.ic{width:16px;height:16px;flex-shrink:0}
main{flex:1;max-width:980px;padding:18px 22px 60px;min-width:0}
h1{font-size:19px;font-weight:600;margin:6px 0 18px;color:var(--text)}
.card{position:relative;border-radius:12px;border:1px solid rgba(255,255,255,.08);background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,0) 46%),rgba(255,255,255,.045);box-shadow:inset 0 1px 0 rgba(255,255,255,.09);padding:22px 26px}
.banner{padding:10px 14px;border-radius:8px;margin:0 0 18px;font-size:13px}
.banner.info{background:rgba(127,176,255,.08);border:1px solid rgba(127,176,255,.22);color:#b9d4ff}
.banner.warn{background:rgba(245,196,81,.08);border:1px solid rgba(245,196,81,.25);color:var(--orange);margin:8px 0 0}
.badge{padding:10px 14px;border-radius:8px;font-size:13px;margin-top:8px}
.badge.ok{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.28);color:var(--green)}
.badge.err{background:rgba(255,107,107,.1);border:1px solid rgba(255,107,107,.28);color:var(--red)}
.row{display:grid;grid-template-columns:190px minmax(0,1fr);gap:14px;align-items:start;margin:15px 0}
.row>label{text-align:right;color:var(--muted);padding-top:8px;font-size:13px}
.row>label .req{color:var(--red);margin-right:3px}
.ctrl{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input,select{background:var(--input);border:1px solid var(--border);border-radius:8px;padding:7px 12px;font:inherit;color:var(--text);min-width:0;transition:box-shadow .16s ease,border-color .16s ease}
input:focus,select:focus{outline:none;border-color:rgba(200,218,255,.4);box-shadow:inset 0 0 0 1px var(--accent-ring)}
input[readonly]{color:var(--faint)}
input::placeholder,select::placeholder{color:var(--faint)}
.wide{flex:1;min-width:200px}
.unit{background:var(--glass-fill);border-radius:8px;padding:7px 12px;color:var(--faint);font-size:13px}
.btn{border:none;border-radius:999px;padding:7px 16px;cursor:pointer;font:inherit;white-space:nowrap;display:inline-flex;align-items:center;justify-content:center;gap:6px;transition:background .15s ease,border-color .15s ease,color .15s ease,filter .15s ease,transform .12s ease}
.btn.blue{background:linear-gradient(145deg,#f6f8fc 0%,#ccd6e6 100%);color:#141821;font-weight:600;box-shadow:0 6px 18px rgba(180,198,230,.22),inset 0 1px 0 rgba(255,255,255,.7)}
.btn.blue:hover{filter:brightness(1.04)}.btn.blue:active{transform:scale(.96)}
.btn.green,.btn.orange,.btn.ghost{background:var(--glass-fill);border:1px solid rgba(255,255,255,.09);color:var(--text);box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}
.btn.green{color:var(--green)}.btn.orange{color:var(--orange)}
.btn.green:hover,.btn.orange:hover,.btn.ghost:hover{background:var(--glass-fill-hover);border-color:rgba(255,255,255,.15)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.actions{display:flex;gap:12px;margin-top:26px;justify-content:center;flex-wrap:wrap}
.chips{display:inline-flex;border:1px solid var(--border);border-radius:999px;overflow:hidden;background:var(--input)}
.chips button{padding:6px 16px;background:transparent;border:none;cursor:pointer;font:inherit;color:var(--muted);transition:background .15s ease,color .15s ease}
.chips button:hover{color:var(--text)}
.chips button.active{color:var(--text);background:var(--accent-soft);box-shadow:inset 0 0 0 1px rgba(205,214,230,.35)}
.tags{background:var(--input);border:1px solid var(--border);border-radius:8px;padding:6px 8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;flex:1;min-width:220px}
.tags .tag{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:1px 4px 1px 8px;font-size:13px;display:inline-flex;align-items:center;color:var(--text)}
.tags .tag b{cursor:pointer;color:var(--faint);font-weight:400;padding:2px 6px}
.tags .tag b:hover{color:var(--red)}
.tags input{border:none;background:transparent;flex:1;min-width:90px;padding:4px}
.tags input:focus{border:none;box-shadow:none}
.note{color:var(--faint);font-size:12px;margin-top:4px}
.cronbox{border:1px solid var(--border);border-radius:8px;margin-top:8px;overflow:hidden;background:rgba(0,0,0,.2)}
.cronbox .head{background:var(--glass-fill);padding:8px 12px;color:var(--muted);font-size:13px;border-bottom:1px solid var(--border)}
.cronbox ol{margin:0;padding:10px 12px 10px 34px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:13px}
.status{margin-top:22px;border-top:1px solid var(--border);padding-top:14px;font-size:13px;color:var(--muted)}
.status b{color:var(--text)}
.status .kv{display:flex;gap:6px;flex-wrap:wrap;margin:3px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--faint);font-weight:500}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;z-index:50;padding:16px}
.modal .box{background:rgba(21,23,29,.96);border-radius:14px;border:1px solid rgba(255,255,255,.08);box-shadow:0 18px 44px rgba(0,0,0,.5),inset 0 0 0 1px rgba(255,255,255,.08);padding:22px 24px;width:min(560px,94vw);max-height:88vh;overflow:auto}
.modal h3{margin:0 0 14px;font-size:17px;color:var(--text)}
.guide{font-size:13.5px;line-height:1.75;color:var(--muted)}
.guide .lead{border-radius:10px;padding:14px 16px;margin:0 0 20px;background:rgba(245,196,81,.08);border:1px solid rgba(245,196,81,.3);color:var(--orange);font-size:14px;line-height:1.7}
.guide .lead b{color:var(--orange)}
.guide .step{display:grid;grid-template-columns:34px minmax(0,1fr);gap:12px;margin:0 0 18px}
.guide .num{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--accent-soft);color:var(--accent-strong);font-weight:600}
.guide .step h4{margin:3px 0 4px;font-size:14.5px;color:var(--text)}
.guide .step ul{margin:4px 0 0;padding-left:18px}
.guide code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;background:rgba(255,255,255,.07);border-radius:4px;padding:1px 5px;color:var(--text)}
.guide b{color:var(--text)}
.guide table{margin:6px 0 4px}
.guide td{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:top}
.guide td:first-child{color:var(--text);width:34%}
.guide h3{margin:26px 0 10px}
.guide .go{color:var(--blue);cursor:pointer}
.crumbs{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px;font-size:13px}
.crumbs a{color:var(--blue);cursor:pointer;text-decoration:none}
.crumbs a:hover{text-decoration:underline}
.dirlist{max-height:300px;overflow:auto;border:1px solid var(--border);border-radius:8px}
.dirlist div{padding:9px 12px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.05);color:var(--text)}
.dirlist div:hover{background:var(--glass-fill-hover)}
.qrwrap{text-align:center}
.qrwrap img{width:230px;height:230px;border:1px solid rgba(255,255,255,.15);border-radius:12px;background:#fff;padding:8px}
#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:99;display:flex;flex-direction:column;gap:8px;align-items:center}
#toast div{padding:9px 18px;border-radius:999px;background:rgba(21,23,29,.96);border:1px solid rgba(255,255,255,.08);box-shadow:0 8px 24px rgba(0,0,0,.45);font-size:13px}
#toast .ok{color:var(--green)}#toast .err{color:var(--red)}#toast .info{color:var(--blue)}
.queue-item{display:flex;justify-content:space-between;gap:10px;padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.05);font-size:13px;align-items:center}
.queue-item .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mutbar button{margin-right:6px}
a{color:var(--blue)}
@media (max-width:860px){.layout{flex-direction:column}aside.side{width:auto;height:auto;position:static;border-right:none;border-bottom:1px solid var(--border);display:flex;gap:2px;overflow-x:auto;padding:8px;align-items:center}.brand{display:none}.nav-group{display:flex;align-items:center;gap:2px;margin:0;flex-shrink:0}.gtitle{padding:7px 10px;white-space:nowrap}.gtitle .ic{display:none}.chev{display:none}.nav-group .nav-items{display:flex;gap:2px;padding:0}.nav-item,.nav-link{padding:7px 10px;white-space:nowrap;margin:0}.nav-item .ic,.nav-link .ic{display:none}main{padding:14px 12px 50px}.row{grid-template-columns:1fr;gap:6px}.row>label{text-align:left;padding-top:0}.card{padding:16px}}
</style>
<div class="layout">
<aside class="side">
<div class="brand">115 网盘与 STRM</div>
<div class="nav-link" data-page="guide" onclick="go('guide')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>使用说明</div>
<div class="nav-group open">
<div class="gtitle" onclick="this.parentElement.classList.toggle('open')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.14.31.22.65.22 1"/></svg>核心配置<span class="chev"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></span></div>
<div class="nav-items">
<div class="nav-item active" data-page="account" onclick="go('account')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>115账号</div>
<div class="nav-item" data-page="strm" onclick="go('strm')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>STRM配置</div>
</div>
</div>
<div class="nav-group open">
<div class="gtitle" onclick="this.parentElement.classList.toggle('open')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>数据同步<span class="chev"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></span></div>
<div class="nav-items">
<div class="nav-item" data-page="full" onclick="go('full')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>全量同步</div>
<div class="nav-item" data-page="incr" onclick="go('incr')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>增量同步</div>
</div>
</div>
<div class="nav-link" data-page="files" onclick="go('files')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>网盘文件</div>
<div class="nav-link" data-page="logs" onclick="go('logs')"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="12" y2="17"/></svg>运行日志</div>
</aside>
<main>
<h1 id="page-title">核心配置</h1>

<section id="page-guide" class="card page guide" hidden>
<div class="lead"><b>先建媒体库，再同步。</b>本插件只能在 MovieClaw <b>已有媒体库的目录（及其子目录）</b>里创建 STRM——插件申请的写入权限就是「媒体库目录读写」。STRM 目录不在任何媒体库里，同步会提示「写入被拒绝 / 没有获得访问授权」，一个文件也写不出来。</div>
<div class="step"><div class="num">1</div><div><h4>准备 STRM 目录</h4>
<ul><li>例如 <code>/media/strm/电影</code>、<code>/media/strm/剧集</code>。填 MovieClaw 看得到的路径，<b>Docker 部署填容器内路径</b>。</li>
<li>目录要允许 MovieClaw 的运行用户写入（Linux 下目录属 root 时需 <code>chown</code> / <code>chmod</code>）。建议与真实视频目录分开。</li></ul></div></div>
<div class="step"><div class="num">2</div><div><h4>在 MovieClaw 里新建媒体库，路径指向这个目录</h4>
<ul><li>MovieClaw → 媒体库 → 新建媒体库：电影选 <code>/media/strm/电影</code>，剧集选 <code>/media/strm/剧集</code>；也可以只建一个指向 <code>/media/strm</code> 的媒体库，下面的子目录都能写。</li>
<li>媒体库建好后同步仍提示写入被拒绝：到 MovieClaw「插件」页把本插件停用再启用一次。</li></ul></div></div>
<div class="step"><div class="num">3</div><div><h4>登录 115</h4>
<ul><li>到 <a class="go" onclick="go('account')">核心配置 → 115账号</a> 扫码登录（或填写 Cookie）。</li></ul></div></div>
<div class="step"><div class="num">4</div><div><h4>配置同步目录</h4>
<ul><li>到 <a class="go" onclick="go('full')">数据同步 → 全量同步</a>：「115媒体库cid」点「点击输入」选网盘目录；「本地STRM目录」填<b>第 2 步媒体库里的路径</b>，如 <code>/media/strm/电影</code>。</li>
<li>电影、剧集分开同步时用「多目录映射」：每条的本地目录都必须在某个媒体库之内，且相互不能相同或嵌套。</li>
<li>点「保存配置」。</li></ul></div></div>
<div class="step"><div class="num">5</div><div><h4>开始同步</h4>
<ul><li>点「开始全量同步」：STRM 与图片 / NFO / 字幕边扫边写入，进度实时保存，中途停止下次接着来。</li>
<li><b>全量同步完成后不会自动通知媒体库扫描</b>，请在 MovieClaw 里对该媒体库手动扫描一次；之后网盘有变动触发的增量同步，有变化时会自动通知扫描。</li></ul></div></div>
<h3>常见问题</h3>
<table><tbody>
<tr><td>「STRM 目录写入被拒绝」「没有获得访问 … 的授权」</td><td>本地 STRM 目录不在任何媒体库里：按第 2 步新建媒体库指向它（或把目录挪进已有媒体库），必要时停用再启用插件。</td></tr>
<tr><td>「Permission denied」「Errno 13」</td><td>目录在媒体库里，但操作系统不允许 MovieClaw 写入：检查目录属主 / 权限。</td></tr>
<tr><td>同步完成但媒体库里看不到</td><td>全量同步不通知扫描：在 MovieClaw 里手动扫描该媒体库。</td></tr>
<tr><td>填了宿主机路径（如 <code>/volume1/...</code>）</td><td>Docker 部署要填容器内路径——就是在 MovieClaw 里新建媒体库时看到的那个路径。</td></tr>
<tr><td>同步出错</td><td>到 <a class="go" onclick="go('logs')">运行日志</a> 查看，红色条目会写明原因与出错位置。</td></tr>
</tbody></table>
</section>

<section id="page-account" class="card page" hidden>
<div class="banner info">请先在 115 手机 APP 里打开生活事件记录，然后清空；然后再开始使用</div>
<div class="row"><label><span class="req">*</span>115 Cookie</label>
<div><div class="ctrl"><input id="f-cookie" data-field="cookie" class="wide" type="password" autocomplete="off" placeholder="扫码登录后自动填充，也可手动粘贴"><button class="btn green" onclick="checkAccount()">✓ 检测可用性</button></div>
<div id="account-badge" class="badge ok" hidden></div>
<div class="note">手动修改 Cookie 并保存后生效；留空保存不会改变已保存的凭据。</div></div></div>
<div class="row"><label>API User-Agent</label>
<div><div class="ctrl"><input id="f-api_user_agent" data-field="api_user_agent" class="wide" placeholder="扫码登录无需填写；手动粘贴 Cookie 时填写获取它的浏览器 UA"></div>
<div class="note">115 会把凭据与签发时的 User-Agent 绑定。扫码登录自动使用并记住一致的 UA；只有手动粘贴 Cookie 时才需要填写「获取该 Cookie 时所用客户端的 UA」（留空使用 Mozilla/5.0，仅适配传统网页 Cookie）。</div></div></div>
<div class="row"><label><span class="req">*</span>cookie对应设备类型</label>
<div class="ctrl"><select id="f-device" class="wide" style="max-width:320px"></select><button class="btn blue" onclick="qrLogin()">▦ 二维码登录</button><button class="btn ghost" onclick="diagNet()">✚ 网络诊断</button></div>
</div>
<div class="row"><label>API请求间隔</label>
<div><div class="ctrl"><input id="f-request_interval" data-field="request_interval" type="number" min="0.3" max="30" step="0.1" style="width:110px"><span class="unit">秒</span></div>
<div class="banner warn">⚠ 设置 API 请求间隔可以减少风控概率</div></div></div>
<div class="row"><label>撞到风控后</label>
<div><div class="ctrl"><input id="f-retry_cooldown" data-field="retry_cooldown" type="number" min="5" max="600" style="width:110px"><span class="unit">秒后继续，最多重试</span><input id="f-retry_max" data-field="retry_max" type="number" min="0" max="10" style="width:80px"><span class="unit">次</span></div>
<div class="note">115 返回「操作太频繁 / 请稍后再试」或网络抖动时，不再让整次同步失败：暂停等待（首次为上述秒数，之后翻倍：30→60→120…，单次最长 600 秒）后自动继续扫描并从原处接着生成 STRM，已生成的 STRM 不会丢失。等待期间「115同步」页会显示剩余时间。0 次 = 立即失败（旧行为）。大库建议同时把「API请求间隔」调大到 2~3 秒。</div></div></div>
<div class="actions"><button class="btn blue" onclick="savePage('account')">💾 保存配置</button><button class="btn orange" onclick="resetPage('account')">↻ 重置配置</button></div>
</section>

<section id="page-strm" class="card page" hidden>
<div class="banner info">STRM 播放地址由 MovieClaw 按当前访问地址自动签发并验签，无需另行部署直链服务</div>
<div class="row"><label>strm直连地址</label><div><div class="ctrl"><input id="f-strm_base_url" data-field="strm_base_url" class="wide" placeholder="留空 = 按当前访问地址自动签发；外网填 http(s)://域名或IP[:端口]"></div><div class="note">留空时按当前访问的 MovieClaw 地址自动签发；需要在外网环境播放 STRM 时，填写外网可达地址（如 http://www.example.com:9577），该地址必须能路由回本 MovieClaw 服务，路径与签名保持不变。下方示例实时预览效果。外网地址变更后，用下方「存量STRM地址替换」把已生成的 STRM 刷新为新地址。</div></div></div>
<div class="row"><label>存量STRM地址替换</label><div>
<div class="ctrl"><button class="btn ghost need-login" onclick="rewriteStrm(true)">🔍 预览</button><button class="btn orange need-login" onclick="rewriteStrm(false)">✎ 执行替换</button></div>
<div id="rewrite-result" class="badge ok" hidden></div>
<div class="note">外网地址（域名/IP/端口）变更后，已生成的 STRM 里仍是旧地址，播放器无法访问。此工具把插件生成的存量 STRM 内地址统一刷新为「上方 strm直连地址（留空则当前访问地址）」，路径与签名保持不变：不访问 115、无需重新同步网盘，几秒完成。只处理插件生成且未被手工修改的文件；建议先预览确认数量，再执行替换。</div>
</div></div>
<div class="row"><label>strm直连格式</label><div class="ctrl"><span class="chips" data-field="strm_link_format"><button data-value="pick_code">pick_code</button><button data-value="pick_code_name">pick_code_name</button></span></div></div>
<div class="row"><label>是否保留文件后缀</label><div><div class="ctrl"><span class="chips" data-field="strm_keep_suffix"><button data-value="true">是</button><button data-value="false">否</button></span></div><div class="note">否（默认）：电影.mkv → 电影.strm；同一目录里只差扩展名的两个视频（电影.mkv + 电影.mp4）只保留体积较大的那个。是：电影.mkv → 电影.mkv.strm，文件名原样照搬、两个版本都保留；切换后已有 STRM 会换成新名字，媒体库需重新识别。</div></div></div>
<div class="row"><label>strm文件本地存在时</label><div><div class="ctrl"><span class="chips" data-field="strm_exist_policy"><button data-value="overwrite">覆盖生成</button><button data-value="skip">直接跳过</button></span></div><div class="note">无论哪种策略，被手工修改过的 STRM 都不会被覆盖（只跳过该文件并计数，不影响其余文件）。</div></div></div>
<div class="row"><label>播放方式</label><div><div class="ctrl"><span class="chips" data-field="play_mode"><button data-value="302">302 直连</button><button data-value="proxy">服务器中转</button></span></div><div class="note">302 直连：播放器跟随跳转直接从 115 CDN 拉流，服务器不消耗带宽；服务器中转：由 MovieClaw 代理拉流再转发给播放器（等同 alist 的代理模式），兼容所有播放器与受限直链。302 播放异常时先切换此项；仍不行时到「网盘文件」页点「诊断」并回报结果。</div></div></div>
<div class="row"><label>预取后续剧集</label><div><div class="ctrl"><input id="f-prefetch_episodes" data-field="prefetch_episodes" type="number" min="0" max="5" style="width:110px"><span class="unit">集（0 = 关闭）</span></div><div class="note">播放剧集时，在后台用同一播放器提前签好后面几集的 115 直链（一次请求批量签发），缓存到链接自身的有效期——点「下一集」不再等 1~3 秒签发。按文件名自然排序找下一集，季末自动接下一季；电影（文件夹里只有一个视频）不受影响。</div></div></div>
<div class="row"><label>直链缓存时间</label><div><div class="ctrl"><input id="f-link_cache_minutes" data-field="link_cache_minutes" type="number" min="1" max="60" style="width:110px"><span class="unit">分钟</span></div><div class="note">签发的 115 直链在此时间内缓存复用：拖动进度、加载字幕轨等连续请求无需逐次签发（每次签发受 API 间隔限制，约 1~3 秒，正是字幕加载慢的根源）。播放中偶发直链过期报错时可调小此值。</div></div></div>
<div class="row"><label>strm生成示例</label><div class="ctrl"><input id="strm-example" class="wide" readonly></div></div>
<div class="actions"><button class="btn blue" onclick="savePage('strm')">💾 保存配置</button><button class="btn orange" onclick="resetPage('strm')">↻ 重置配置</button></div>
</section>

<section id="page-full" class="card page" hidden>
<div class="banner warn" style="margin:0 0 10px;line-height:1.7"><b>使用前必读：</b>插件只能在 MovieClaw <b>已有媒体库的目录</b>里创建 STRM。请先在 MovieClaw「媒体库 → 新建媒体库」把路径指向你要放 STRM 的目录（如 <code>/media/strm/电影</code>，Docker 填容器内路径），再把下面的「本地STRM目录」填成这个目录或它的子目录；否则同步会提示「写入被拒绝 / 没有获得访问授权」。<a class="go" style="color:var(--blue);cursor:pointer;margin-left:6px" onclick="go('guide')">查看完整使用说明 →</a></div>
<div class="banner info">① 点「开始全量同步」前请先「保存配置」　② 全量同步完成后请在 MovieClaw 里手动扫描一次媒体库；之后增量同步有变化时会自动通知扫描　③ 同步运行中可点「⏸ 停止同步」，进度实时保存，下次继续　④ 断点续传 / 「🗑️ 清理断点」仅对「逐目录扫描」生效</div>
<div class="banner warn login-warn" hidden>尚未登录 115：目录选择与同步功能暂不可用。请先到 <a style="color:var(--blue);cursor:pointer" onclick="go('account')">核心配置 → 115账号</a> 扫码登录或填写 Cookie。</div>
<div class="row"><label><span class="req">*</span>同步模式</label><div><div class="ctrl"><span class="chips" data-field="sync_mode"><button data-value="normal">普通模式</button><button data-value="fast">快速模式</button></span></div><div class="note">快速模式：信任同步记录，跳过未变化文件的读回校验，对全量与增量均生效，速度最快。普通模式：逐个读回校验（可发现并拒绝被外部改动的文件），正确性最强。两种模式都只重写有差异的文件。</div></div></div>
<div class="row"><label>单次扫描最大条目数</label><div><div class="ctrl"><input id="f-max_entries" data-field="max_entries" type="number" min="0" max="100000000" step="1000" style="width:140px"><span class="unit">个文件（0 = 不限）</span></div><div class="note">默认 0 = 不限制，文件再多也照常同步。这只是可选的安全阀：填一个数后，若某个映射目录（含子目录）在 115 上的文件总数（含图片/NFO 等全部文件）超过它，会在扫描第一页就停下并提示，防止误把整个网盘当媒体库。</div></div></div>
<div class="row"><label><span class="req">*</span>网盘扫描方式</label><div><div class="ctrl"><span class="chips" data-field="scan_strategy"><button data-value="fast">极速扫描</button><button data-value="legacy">逐目录扫描</button></span></div><div class="note">极速扫描（推荐）：整棵目录树一次性分页列出全部文件（每页 1150 条），目录名用目录树接口一次取回，请求数与目录数量无关；接口不可用时自动退回逐目录扫描并写入运行日志。逐目录扫描：旧方式，每个目录至少一次请求，大库可能要数小时，仅供排查。</div></div></div>
<div class="row"><label><span class="req">*</span>115媒体库cid</label>
<div><div class="ctrl"><input id="f-cloud_cid" data-field="cloud_cid" class="wide" placeholder="例如 3075962308214349164"><button class="btn blue need-login" id="btn-picker" onclick="openPicker()">点击输入</button></div>
<div class="ctrl" style="margin-top:8px"><input id="cid-path" class="wide" readonly placeholder="选择后显示对应网盘路径"></div></div></div>
<div class="row"><label><span class="req">*</span>本地STRM目录</label><div><div class="ctrl"><input id="f-strm_directory" data-field="strm_directory" class="wide" placeholder="须在媒体库目录内，如 /media/strm/电影"></div><div class="note"><b>必须是 MovieClaw 媒体库的目录或其子目录。</b>先在 MovieClaw 新建媒体库并指向该目录，再填到这里（Docker 填容器内路径）。不在任何媒体库里的路径会被拒绝写入；媒体库建好后如仍被拒绝，在 MovieClaw「插件」页停用再启用本插件。</div></div></div>
<div class="row"><label>多目录映射</label>
<div><div id="map-list"></div><div class="ctrl" style="margin-top:8px"><button class="btn ghost" onclick="addMappingRow()">＋ 添加映射</button><button class="btn ghost" onclick="resetMappings()">清空（改回单目录）</button></div>
<div class="note">配置映射后上方单目录（115媒体库cid + 本地STRM目录）被忽略；每条映射独立选择 115 目录与本地 STRM 目录，<b>每个本地目录都必须在某个 MovieClaw 媒体库之内</b>（如电影 → <code>/media/strm/电影</code>、剧集 → <code>/media/strm/剧集</code>，各建一个媒体库或共用指向 <code>/media/strm</code> 的媒体库），目录之间不能相同或嵌套。最多 8 条，保存后生效。</div></div></div>
<div class="row"><label><span class="req">*</span>视频文件后缀</label><div class="ctrl"><div class="tags" data-field="video_suffixes" id="f-suffixes"></div></div></div>
<div class="row"><label>最小视频</label><div class="ctrl"><input id="f-min_video_mb" data-field="min_video_mb" type="number" min="0" style="width:110px"><span class="unit">MB</span></div></div>
<div class="row"><label>图片/NFO/字幕同步</label><div><div class="ctrl"><span class="chips" data-field="sidecar_sync"><button data-value="true">启用</button><button data-value="false">禁用</button></span></div><div class="note">把网盘里的刮削元数据一起复制到 STRM 目录对应位置（经 115 直链下载，单文件最大 20MB）：<b>视频所在目录、它的各级上级目录</b>（剧集/季/合集层的 tvshow.nfo、poster.jpg、season01-poster.jpg 等）以及其中的 extrafanart / extrathumbs 文件夹里的全部 .nfo / .jpg / .jpeg / .png / .webp / .tbn 与字幕（.srt/.ass/.ssa/.vtt/.sub/.idx/.sup）。不含视频的目录（如相册）不同步。随扫描边下载，停止后进度保留；本地已有且不是插件下载的同名文件绝不覆盖。</div></div></div>
<div class="row"><label>清理网盘已删除的STRM</label><div><div class="ctrl"><span class="chips" data-field="strm_cleanup"><button data-value="false">关闭</button><button data-value="true">开启</button></span></div><div class="note">开启后，核对确认网盘已删除的视频会移除插件生成且未被修改的 STRM 与附属副本；默认关闭，只增不删。</div></div></div>
<div class="actions"><button class="btn blue" onclick="savePage('full')">💾 保存配置</button><button class="btn orange" onclick="resetPage('full')">↻ 重置配置</button><button class="btn blue need-login" id="btn-full" onclick="startSync('full')">▶ 开始全量同步</button><button class="btn red need-login" id="btn-stop-full" onclick="stopSync()" hidden>⏸ 停止同步</button><button class="btn ghost" onclick="clearCheckpoint()">🗑️ 清理断点</button><button class="btn ghost" onclick="exportConfig()">⬇ 导出配置</button><button class="btn ghost" onclick="importConfig()">⬆ 导入配置</button></div>
<div class="status" id="full-status"></div>
</section>

<section id="page-incr" class="card page" hidden>
<div class="banner info">任何设备上传或移动文件到 115 监控库都会即时触发增量核对；cron 表达式用于定时兜底</div>
<div class="banner warn login-warn" hidden>尚未登录 115：目录选择与同步功能暂不可用。请先到 <a style="color:var(--blue);cursor:pointer" onclick="go('account')">核心配置 → 115账号</a> 扫码登录或填写 Cookie。</div>
<div class="row"><label><span class="req">*</span>同步任务cron表达式</label>
<div><div class="ctrl"><input id="f-sync_cron" data-field="sync_cron" class="wide" style="font-family:ui-monospace,Menlo,monospace" placeholder="*/20 8-23 * * *"></div>
<div class="cronbox"><div class="head">▣ 最近5次运行时间</div><ol id="cron-runs"><li>输入表达式后自动计算</li></ol></div>
<div class="note" id="cron-error" style="color:var(--red)"></div></div></div>
<div class="row"><label>监控115生活事件</label><div class="ctrl"><span class="chips" data-field="life_events"><button data-value="true">开启</button><button data-value="false">关闭</button></span><div class="note">监测上传/移动/接收/新建目录/删除/改名等操作事件，检测到即触发目录核对；浏览/星标忽略。事件逐条写入运行日志「115事件」。</div></div></div>
<div class="row"><label>生活事件检查间隔</label><div class="ctrl"><input id="f-life_interval" data-field="life_interval" type="number" min="30" max="3600" style="width:110px"><span class="unit">秒</span></div></div>
<div class="actions"><button class="btn blue" onclick="savePage('incr')">💾 保存配置</button><button class="btn orange" onclick="resetPage('incr')">↻ 重置配置</button><button class="btn blue need-login" id="btn-incr" onclick="startSync('incremental')">▶ 开始增量同步</button><button class="btn red need-login" id="btn-stop-incr" onclick="stopSync()" hidden>⏸ 停止同步</button></div>
<div class="status" id="incr-status"></div>
</section>

<section id="page-files" class="card page" hidden>
<div class="banner info">按 115 媒体库 CID 浏览网盘；文件下载经 302 跳转（播放方式为服务器中转时经插件转发）。文件行「诊断」一键检测直链签发与 CDN 拉流，302 播放异常时用它取回报错详情</div>
<div class="row"><label>目录 ID</label><div class="ctrl"><input id="cid" value="0" size="18"><button class="btn blue" onclick="offset=0;browse()">打开目录</button><button class="btn ghost" onclick="offset=Math.max(0,offset-pageSize);browse()">上一页</button><button class="btn ghost" onclick="offset+=pageSize;browse()">下一页</button><button class="btn ghost mut" onclick="newFolder()">新建目录</button></div></div>
<table><thead><tr><th>名称</th><th>大小</th><th>操作</th></tr></thead><tbody id="files"></tbody></table>
</section>

<section id="page-logs" class="card page" hidden>
<div class="banner info">集中查看运行事件与同步历史；红色为出错条目，直接给出原因。</div>
<div class="row"><label>运行事件</label><div><div class="cronbox"><div class="head">▣ 最近 100 条事件（系统/登录/配置/同步/播放/115事件）</div><ol id="runtime-log"><li>暂无事件</li></ol></div></div></div>
<div class="row"><label>同步历史</label><div><div class="cronbox"><div class="head">▣ 最近 20 次同步</div><ol id="sync-history"><li>尚未有运行记录</li></ol></div><div class="note">包含手动与定时触发的全量/增量运行：耗时、扫描条目、写入/未变化/附属/清理，多目录映射各带一行独立明细；同名冲突列出保留与跳过的文件；失败记录原因。</div></div></div>
</section>
</main>
</div>

<div id="qr-modal" class="modal" hidden><div class="box qrwrap">
<h3>115 扫码登录</h3>
<p style="color:var(--muted);font-size:13px;margin:0 0 12px">用 115 手机客户端扫码，选择「设备登录」并确认。同类型设备互相挤占，请选择未被占用的类型。</p>
<img id="qr" alt="115 登录二维码">
<p id="login-state" style="margin:12px 0"></p>
<div class="actions" style="margin-top:6px"><button class="btn blue" onclick="regenQr()">↻ 重新生成</button><button class="btn ghost" onclick="closeQr()">关闭</button></div>
</div></div>

<div id="cid-modal" class="modal" hidden><div class="box">
<h3>选择 115 目录</h3>
<p style="color:var(--muted);font-size:13px;margin:0 0 12px">在网盘目录树中逐级点选，用于确定「115 媒体库 CID」对应的目录；也可以关闭弹窗直接手填 CID。</p>
<div class="crumbs" id="picker-crumbs"></div>
<div class="dirlist" id="picker-list"></div>
<div class="actions"><button class="btn blue" onclick="pickCurrent()">选择当前目录</button><button class="btn ghost" onclick="document.querySelector('#cid-modal').hidden=true">取消</button></div>
</div></div>

<div id="diag-modal" class="modal" hidden><div class="box" style="max-width:600px">
<h3>播放诊断</h3>
<pre id="diag-report" style="white-space:pre-wrap;font-size:12px;line-height:1.7;max-height:56vh;overflow:auto;color:var(--muted);margin:0 0 8px"></pre>
<div class="actions"><button class="btn ghost" onclick="document.querySelector('#diag-modal').hidden=true">关闭</button></div>
</div></div>

<div id="toast"></div>
<script>
const base=location.pathname.replace(/\/(panel\/?|web(\/.*)?)$/,'');
// 2.2.0：同一份页面双挂载——直接打开的 /panel（老入口）与宿主静态托管的 /web/*（插件网页 iframe）。
// iframe 场景下跟随宿主注入的 --mc-* 主题变量（带兜底值），背景透明透出 MovieClaw 外框。
const IN_APP=/\/web(\/|$)/.test(location.pathname);
if(IN_APP)document.documentElement.classList.add('in-app');
const NUM=new Set(['request_interval','min_video_mb','life_interval','timeout','page_size','max_entries','link_cache_minutes','prefetch_episodes','retry_max','retry_cooldown']);
const BOOL=new Set(['strm_keep_suffix','sidecar_sync','strm_cleanup','life_events']);
const PAGES={account:['cookie','api_user_agent','request_interval','retry_cooldown','retry_max'],strm:['strm_base_url','strm_link_format','strm_keep_suffix','strm_exist_policy','play_mode','prefetch_episodes','link_cache_minutes'],full:['sync_mode','scan_strategy','max_entries','cloud_cid','strm_directory','video_suffixes','min_video_mb','sidecar_sync','strm_cleanup','mappings'],incr:['sync_cron','life_events','life_interval'],logs:[]};
const TITLES={guide:'使用说明',account:'核心配置',strm:'核心配置',full:'数据同步',incr:'数据同步',files:'网盘文件',logs:'运行日志'};
const stateNames={scanning:'扫描中',idle:'尚未运行',running:'同步中',completed:'已完成',failed:'失败',cancelled:'已停止',interrupted:'上次运行已中断',waiting_config:'等待配置',waiting_login:'等待登录'};
let config={},allowChanges=false,loggedIn=false,syncRunning=false,offset=0,pageSize=1000,loginTimer,qrRefresh=0,qrDevice='';

function toast(msg,type='info'){let box=document.querySelector('#toast');let el=document.createElement('div');el.className=type;el.textContent=msg;box.append(el);setTimeout(()=>el.remove(),3600)}
function setLoggedIn(v){loggedIn=v;applyGates()}
function applyGates(){document.querySelectorAll('.need-login').forEach(b=>{if(b.id==='btn-full'||b.id==='btn-incr')b.disabled=!loggedIn||syncRunning;else b.disabled=!loggedIn;b.title=b.disabled&&!loggedIn?'请先在「核心配置 → 115账号」登录':''});let sf=document.getElementById('btn-stop-full'),si=document.getElementById('btn-stop-incr');if(sf)sf.hidden=!syncRunning;if(si)si.hidden=!syncRunning;document.querySelectorAll('.login-warn').forEach(b=>b.hidden=loggedIn)}
async function api(path,opts={}){let r=await fetch(base+path,{credentials:'same-origin',...opts});let d=await r.json().catch(()=>({}));if(!r.ok){let msg=typeof d.detail==='string'?d.detail:'请求失败，请检查登录和插件设置';if(!opts.silent)toast(msg,'err');throw Error(msg)}return d}
function fmtSpace(v){if(typeof v!=='number'||v<=0)return '—';let u=['GB','TB','PB'],n=v/1024**3,i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return n.toFixed(2)+' '+u[i]}
function fmtTime(t){if(!t)return '—';let d=new Date(t*1000),p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`}

// ---------- navigation ----------
function go(p){document.querySelectorAll('.nav-item,.nav-link').forEach(n=>n.classList.toggle('active',n.dataset.page===p));document.querySelectorAll('.page').forEach(pg=>pg.hidden=pg.id!=='page-'+p);document.querySelector('#page-title').textContent=TITLES[p]||'';if(p==='files')refresh();if(p==='logs')refreshLogs();if(IN_APP&&location.hash.slice(1)!==p)history.replaceState(null,'','#'+p)}

// ---------- form widgets ----------
function initChips(){document.querySelectorAll('.chips').forEach(c=>{c.querySelectorAll('button').forEach(b=>b.onclick=e=>{e.preventDefault();c.querySelectorAll('button').forEach(x=>x.classList.remove('active'));b.classList.add('active');if(c.dataset.field==='strm_link_format'||c.dataset.field==='strm_keep_suffix')renderExample()})})}
function setChips(field,value){let c=document.querySelector(`.chips[data-field="${field}"]`);if(!c)return;let s=String(value);c.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.value===s))}
function chipsValue(field){let b=document.querySelector(`.chips[data-field="${field}"] button.active`);return b?b.dataset.value:undefined}
function initTags(){let box=document.querySelector('#f-suffixes');let input=document.createElement('input');input.placeholder='输入后缀后回车添加';box.append(input);box.onclick=()=>input.focus();input.onkeydown=e=>{if(e.key==='Enter'||e.key===','){e.preventDefault();let v=input.value.trim().replace(/^\./,'').toLowerCase();if(v){if(!/^[a-z0-9]{1,10}$/.test(v)){toast('后缀只能包含字母和数字','err');return}addTag(v)}input.value=''}else if(e.key==='Backspace'&&!input.value){let tags=box.querySelectorAll('.tag');if(tags.length)tags[tags.length-1].remove()}}}
function addTag(v){let box=document.querySelector('#f-suffixes');if([...box.querySelectorAll('.tag')].some(t=>t.dataset.v===v))return;let tag=document.createElement('span');tag.className='tag';tag.dataset.v=v;tag.append(document.createTextNode(v));let x=document.createElement('b');x.textContent='×';x.onclick=()=>tag.remove();tag.append(x);box.insertBefore(tag,box.querySelector('input'))}
function setTags(csv){document.querySelectorAll('#f-suffixes .tag').forEach(t=>t.remove());(csv||'').split(',').map(s=>s.trim()).filter(Boolean).forEach(addTag)}
function tagsValue(){return [...document.querySelectorAll('#f-suffixes .tag')].map(t=>t.dataset.v).join(',')}

// ---------- multi-directory mappings (qmediasync SyncPath style) ----------
let mappingRows=[];
function parseMappings(raw){try{let v=JSON.parse(raw||'');return Array.isArray(v)?v:[]}catch(e){return []}}
function mappingsValue(){return mappingRows.length?JSON.stringify(mappingRows.map(m=>({name:(m.name||'').trim(),cloud_cid:(m.cid||'').trim(),strm_directory:(m.dir||'').trim()}))):''}
function addMappingRow(name='',cid='',dir=''){if(mappingRows.length>=8){toast('最多 8 条映射','err');return}mappingRows.push({name,cid,dir});renderMappings()}
function resetMappings(){if(mappingRows.length&&!confirm('清空多目录映射，改回使用上方单目录配置？'))return;mappingRows=[];renderMappings()}
function renderMappings(){let box=document.querySelector('#map-list');box.replaceChildren();
for(let i=0;i<mappingRows.length;i++){let m=mappingRows[i];let row=document.createElement('div');row.className='ctrl';row.style.cssText='flex-wrap:wrap;gap:6px;margin-top:6px';
let name=document.createElement('input');name.placeholder='名称（如：电影）';name.style.width='120px';name.value=m.name;name.oninput=()=>m.name=name.value;
let cid=document.createElement('input');cid.placeholder='115 目录 CID';cid.style.width='170px';cid.value=m.cid;cid.oninput=()=>m.cid=cid.value.trim();
let pick=document.createElement('button');pick.className='btn blue need-login';pick.textContent='选择';pick.onclick=()=>openPickerFor(cid);
let dir=document.createElement('input');dir.placeholder='媒体库内的 STRM 目录，如 /media/strm/movies';dir.style.flex='1';dir.minWidth='200px';dir.value=m.dir;dir.oninput=()=>m.dir=dir.value.trim();
let del=document.createElement('button');del.className='btn orange';del.textContent='删除';del.onclick=()=>{mappingRows.splice(i,1);renderMappings()};
row.append(name,cid,pick,dir,del);box.append(row)}
if(mappingRows.length)box.append(Object.assign(document.createElement('div'),{className:'note',textContent:`共 ${mappingRows.length} 条映射，保存后各映射独立同步。单目录配置在存在映射时被忽略。`}));
applyGates()}
function exportConfig(){let a=document.createElement('a');a.href=base+'/config/export';a.click();toast('配置备份已下载（不含 Cookie）','ok')}
function importConfig(){let input=document.createElement('input');input.type='file';input.accept='.json,application/json';
input.onchange=async()=>{let f=input.files[0];if(!f)return;try{let parsed=JSON.parse(await f.text());let values=parsed&&typeof parsed==='object'?(parsed.values||parsed):null;if(!values||typeof values!=='object'||Array.isArray(values))throw Error('备份文件格式不正确');
await api('/config/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});toast('配置已导入并生效（Cookie 不受影响）','ok');await loadConfig();refreshAccount();refreshSync()}catch(e){toast('导入失败：'+e.message,'err')}};input.click()}
async function refreshLogs(){try{let d=await api('/log/list');
let ol=document.querySelector('#runtime-log');ol.replaceChildren();
if(!d.events.length){ol.append(Object.assign(document.createElement('li'),{textContent:'暂无事件'}))}
for(let e of d.events){let li=document.createElement('li');li.textContent=`${e.time} [${e.source}] ${e.message}`;if(e.level==='error')li.style.color='var(--red)';else if(e.level==='warn')li.style.color='var(--orange)';ol.append(li)}
ol=document.querySelector('#sync-history');ol.replaceChildren();
if(!d.history.length){ol.append(Object.assign(document.createElement('li'),{textContent:'尚未有运行记录'}))}
for(let h of d.history){let li=document.createElement('li');
let head=document.createElement('div');
head.textContent=h.time+' '+(h.mode==='full'?'全量':'增量')+' · 耗时 '+(h.elapsed!=null?h.elapsed+'s':'—')+' · 扫描 '+(h.seen||0)+' · 写入 '+(h.written||0)+' · 未变化 '+(h.unchanged||0)+' · 附属 '+(h.sidecars||0)+' · 清理 '+(h.deleted||0)+(h.conflicts?' · 同名冲突 '+h.conflicts:'');
li.append(head);
for(let m of (h.mappings||[])){let sub=document.createElement('div');sub.style.opacity='.85';sub.style.fontSize='12px';
sub.textContent='　↳ '+m.name+'：扫描 '+(m.seen||0)+' · 写入 '+(m.written||0)+' · 未变化 '+(m.unchanged||0)+' · 附属 '+(m.sidecars||0)+' · 清理 '+(m.deleted||0)+(m.conflicts?' · 冲突 '+m.conflicts:'');
li.append(sub);
for(let c of (m.conflict_detail||[])){let cd=document.createElement('div');cd.style.color='var(--orange)';cd.style.fontSize='12px';cd.textContent='　　⚠ '+c;li.append(cd)}}
if(h.error){let err=document.createElement('div');err.style.color='var(--red)';err.textContent='失败：'+h.error;li.append(err)}
ol.append(li)}
}catch(e){}}

// ---------- config load / save ----------
async function loadConfig(){try{config=await api('/config');let v=config.values;
for(let f of['request_interval','retry_cooldown','retry_max','cloud_cid','strm_directory','strm_base_url','min_video_mb','life_interval','link_cache_minutes','max_entries','prefetch_episodes']){let el=document.querySelector(`#f-${f}`)||document.querySelector(`[data-field="${f}"]`);if(el&&el.tagName==='INPUT')el.value=v[f]??''}
for(let f of['sync_cron']){let el=document.querySelector('#f-'+f);if(el)el.value=v[f]??''}
for(let f of['strm_link_format','strm_keep_suffix','strm_exist_policy','play_mode','sync_mode','scan_strategy','sidecar_sync','strm_cleanup','life_events'])setChips(f,v[f]);
setTags(v.video_suffixes||config.defaults.video_suffixes);
mappingRows=parseMappings(v.mappings);renderMappings();
document.querySelector('#f-cookie').placeholder=config.secrets_set.includes('cookie')?'已设置（扫码或手动粘贴），留空保持不变':'尚未设置，可扫码登录或手动粘贴';
let sel=document.querySelector('#f-device');sel.replaceChildren();for(const[id,name]of Object.entries(config.devices)){let o=document.createElement('option');o.value=id;o.textContent=`${name} - ${id}`;sel.append(o)}sel.value=config.device||'wechatmini';
document.querySelector('#f-strm_base_url').value=v.strm_base_url||'';
renderExample();previewCron();setLoggedIn(config.secrets_set.includes('cookie'));
}catch(e){}}
function renderExample(){let raw=config.sample_play_url||'';if(!raw){document.querySelector('#strm-example').value='';return}
let custom=document.querySelector('#f-strm_base_url').value.trim().replace(/\/+$/,'');
if(custom){try{let u=new URL(raw);let c=new URL(custom);raw=c.origin+u.pathname+(u.search||'')}catch(e){}}
raw=raw.replace('/play/EXAMPLE','/play/abchrb6gnrw0hhh80');if(chipsValue('strm_link_format')==='pick_code_name')raw+='?/'+encodeURIComponent('钢铁侠.2008.1080p.BluRay.X264.DTS-TnT.mkv');document.querySelector('#strm-example').value=raw}
function readField(f){if(f==='mappings')return mappingsValue();if(f==='video_suffixes')return tagsValue();let c=document.querySelector(`.chips[data-field="${f}"]`);if(c){let v=chipsValue(f);if(BOOL.has(f))return v==='true';return v}let el=document.querySelector('#f-'+f)||document.querySelector(`input[data-field="${f}"]`);if(!el)return undefined;let v=el.value.trim();if(NUM.has(f))return v===''?undefined:Number(v);return v}
async function savePage(page){let patch={};for(let f of PAGES[page]){let v=readField(f);if(v===undefined)continue;if(f==='cookie'&&!v)continue;patch[f]=v}
if(!Object.keys(patch).length){toast('没有需要保存的修改');return}
try{await api('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values:patch})})}catch(e){return}
toast('配置已保存并已生效','ok');await loadConfig();refreshAccount();refreshSync()}
async function resetPage(page){let fields=PAGES[page].filter(f=>f!=='cookie');let msg='确定将本页配置重置为默认值吗？';if(page==='account'&&confirm('重置 115 账号配置会同时清除已保存的 Cookie（退出登录）。确定全部重置？'))fields=PAGES[page];else if(!confirm(msg))return;
try{await api('/config/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fields})})}catch(e){return}
toast('已重置并已生效','ok');await loadConfig();refreshAccount();refreshSync()}

// ---------- 115账号 ----------
function accountBadgeText(d){if(d.error)return null;let name=d.user_name||('UID '+d.uid);if(d.space_total==null&&d.space_used==null)return `✔ 账号：${name}（凭据有效，115 未返回容量明细）`;return `✔ 账号：${name}，容量：${fmtSpace(d.space_used)} / ${fmtSpace(d.space_total)}（剩余 ${fmtSpace(d.space_free)}）`}
async function refreshAccount(){try{let d=await api('/login/info');setLoggedIn(!!d.logged_in);let badge=document.querySelector('#account-badge');if(!d.logged_in){badge.hidden=true;return}let text=accountBadgeText(d);if(!text){badge.className='badge err';badge.textContent='凭据已保存，但检测失败：'+d.error;badge.hidden=false;return}badge.className='badge ok';badge.textContent=text;badge.hidden=false}catch(e){}}
async function checkAccount(){try{let d=await api('/login/info?refresh=true');setLoggedIn(!!d.logged_in);let badge=document.querySelector('#account-badge');if(!d.logged_in){badge.className='badge err';badge.textContent='尚未登录：请扫码或粘贴 Cookie 后保存。';badge.hidden=false;return}let text=accountBadgeText(d);if(!text){badge.className='badge err';badge.textContent='Cookie 不可用：'+d.error;badge.hidden=false;return}badge.className='badge ok';badge.textContent=text;badge.hidden=false}catch(e){}}
async function qrLogin(){if(loggedIn&&!confirm('检测到已登录。重新扫码会在 115 端替换同类型设备的登录（同类型互踢），确定继续？'))return;qrRefresh=0;let device=document.querySelector('#f-device').value;
let img=document.querySelector('#qr');img.style.visibility='hidden';document.querySelector('#login-state').textContent='正在向 115 获取二维码，通常需要几秒…';document.querySelector('#qr-modal').hidden=false;
await fetchQr(device)}
async function fetchQr(device,auto){qrDevice=device;let img=document.querySelector('#qr');img.style.visibility='hidden';document.querySelector('#login-state').textContent=auto?'二维码已过期，正在自动刷新…':'正在向 115 获取二维码，通常需要几秒…';
try{await api('/login/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device})})}catch(e){document.querySelector('#login-state').textContent='获取二维码失败：'+e.message+'（网络到 115 可能较慢）。可点击「重新生成」重试';return}
img.onload=()=>img.style.visibility='visible';img.src=base+'/login/qr?t='+Date.now();document.querySelector('#login-state').textContent=auto?'二维码已自动刷新，请重新扫码':'请用 115 手机客户端扫码（长时间未确认会自动刷新）';pollLogin()}
function regenQr(){fetchQr(qrDevice||document.querySelector('#f-device').value)}
function closeQr(){document.querySelector('#qr-modal').hidden=true;clearTimeout(loginTimer)}
async function pollLogin(){clearTimeout(loginTimer);try{let d=await api('/login/poll',{method:'POST'});document.querySelector('#login-state').textContent=({waiting:'等待扫码…',scanned:'已扫描，请在手机上确认',logged_in:'✔ 登录成功，凭据已加密保存',expired:'二维码已过期，正在自动刷新…',cancelled:'已取消登录'})[d.state];if(d.state==='logged_in'){setLoggedIn(true);qrRefresh=0;setTimeout(()=>{closeQr();refreshAccount();loadConfig()},1200)}else if(d.state==='expired'){qrRefresh++;if(qrRefresh<=5)await fetchQr(qrDevice,true);else document.querySelector('#login-state').textContent='二维码已多次过期，请检查网络后点击「重新生成」'}
if(['waiting','scanned'].includes(d.state))loginTimer=setTimeout(pollLogin,2500)}catch(e){loginTimer=setTimeout(pollLogin,5000)}}

async function diagNet(){toast('正在检测本服务器到 115 扫码接口的连通性…','info');try{let d=await api('/login/diag');if(d.ok)toast(`网络正常：取码/换Cookie接口连接耗时 ${d.elapsed} 秒`,'ok');else{let names={token:'取码(qrcodeapi)',confirm:'换Cookie(passportapi)',cookie:'当前Cookie探测'};let parts=Object.entries(d.checks||{}).map(([k,v])=>`${names[k]||k}：${v.detail}`).join('；');toast(`诊断结果：${parts||'未知原因'}（耗时 ${d.elapsed} 秒）`,'err')}}catch(e){}}

// ---------- CID picker ----------
let pickerCid='0',pickerPath=[],pickerInput=null;
function pickerMsg(text,color){let list=document.querySelector('#picker-list');let crumbs=document.querySelector('#picker-crumbs');crumbs.replaceChildren();let root=document.createElement('a');root.textContent='根目录';root.onclick=()=>loadDirs('0');crumbs.append(root);list.replaceChildren(Object.assign(document.createElement('div'),{textContent:text,style:'color:'+(color||'var(--faint)')}))}
async function openPickerFor(input){pickerInput=input;document.querySelector('#cid-modal').hidden=false;if(!loggedIn){pickerMsg('尚未登录：请先在「核心配置 → 115账号」扫码登录或填写 Cookie，再选择目录。','var(--red)');return}await loadDirs((input&&input.value.trim())||'0')}
async function openPicker(){await openPickerFor(document.querySelector('#f-cloud_cid'))}
async function loadDirs(cid){pickerMsg('正在加载 115 目录，首次可能需要几秒…');
try{let d=await api('/dirs?cid='+encodeURIComponent(cid));pickerCid=d.cid;pickerPath=d.path||[];let crumbs=document.querySelector('#picker-crumbs');crumbs.replaceChildren();let root=document.createElement('a');root.textContent='根目录';root.onclick=()=>loadDirs('0');crumbs.append(root);for(let p of pickerPath){crumbs.append(document.createTextNode(' / '));let a=document.createElement('a');a.textContent=p.name;a.onclick=()=>loadDirs(p.cid);crumbs.append(a)}
let list=document.querySelector('#picker-list');list.replaceChildren();if(!d.items.length)list.append(Object.assign(document.createElement('div'),{textContent:'（没有子目录）'}));for(let item of d.items){let row=document.createElement('div');row.textContent='📁 '+item.name;row.onclick=()=>loadDirs(item.id);list.append(row)}}catch(e){pickerMsg('加载失败：'+e.message+'。常见原因：Cookie 失效、服务器访问 115 超时（最长 30 秒）。可稍后点击上方面包屑重试。','var(--red)')}}
function pickCurrent(){if(pickerInput)pickerInput.value=pickerCid;if(pickerInput===document.querySelector('#f-cloud_cid'))document.querySelector('#cid-path').value=pickerPath.length?'/'+pickerPath.map(p=>p.name).join('/'):'/（根目录）';document.querySelector('#cid-modal').hidden=true}
async function resolveCidPath(){let cid=document.querySelector('#f-cloud_cid').value.trim();if(!cid){document.querySelector('#cid-path').value='';return}try{let d=await api('/dirs?cid='+encodeURIComponent(cid));document.querySelector('#cid-path').value=(d.path&&d.path.length)?'/'+d.path.map(p=>p.name).join('/'):'/（根目录）'}catch(e){document.querySelector('#cid-path').value='（无法解析该 CID，请检查）'}}

// ---------- cron preview ----------
let cronTimer;
function previewCron(){clearTimeout(cronTimer);cronTimer=setTimeout(async()=>{let expr=document.querySelector('#f-sync_cron').value.trim();let list=document.querySelector('#cron-runs'),err=document.querySelector('#cron-error');err.textContent='';try{let d=await api('/sync/next-runs'+(expr?'?expr='+encodeURIComponent(expr):''));list.replaceChildren();for(let r of d.runs){let li=document.createElement('li');li.textContent=r;list.append(li)}}catch(e){list.replaceChildren();err.textContent=e.message}},400)}

// ---------- sync status & actions ----------
function renderSyncStatus(el,s){if(!el)return;let next=s.next_run_at?fmtTime(s.next_run_at):'—';
let progress=s.state==='running'?`<div class="kv" style="color:var(--orange)">实时进度：已扫描 ${s.seen||0} · 写入 ${s.written||0} · 未变化 ${s.unchanged||0}${s.skipped?` · 跳过 ${s.skipped}`:''}${s.running_for?` · 已运行 ${s.running_for}s`:''}（每 5 秒自动刷新）</div>`:'';
// 2.4.0：撞上 115 限速时同步并没有失败，只是在等冷却——没有这行就只看见计
// 数器不动，看上去像卡死（此前多次被当成故障）。
let cooling=s.state==='running'&&s.throttle_wait>0?`<div class="kv" style="color:var(--orange)">⏸ 115 限速冷却中：${s.throttle_wait}s 后自动继续扫描并继续生成 STRM${s.throttle_count?`（本次第 ${s.throttle_count}/${s.throttle_limit||'?'} 次等待）`:''}${s.throttle_reason?`<div style="opacity:.8;font-size:12px">${s.throttle_reason}</div>`:''}已生成的 STRM 不受影响，无需从头重来。</div>`:'';
// 写入数长时间不涨通常是在校验已存在的 STRM（内容一致就不需要重写），而不是卡住。
let verifying=s.state==='running'&&!s.throttle_wait&&(s.seen||0)>0&&!(s.written||0)&&(s.unchanged||0)>0?`<div class="kv" style="opacity:.8;font-size:12px">正在边扫描边核对：当前文件已存在且内容一致，计入「未变化」而不重复写入；遇到新文件或有变化的文件会立即生成。</div>`:'';
el.innerHTML=`<div class="kv"><b>状态：</b>${stateNames[s.state]||s.state||'—'}${s.mode?`（${s.mode==='full'?'全量':'增量'}）`:''}${s.mapping_count>1?` · ${s.mapping_count} 个映射`:''}</div>`+progress+cooling+verifying+
`<div class="kv"><b>上次同步：</b>${fmtTime(s.synced_at)} · 写入 ${s.written||0} · 未变化 ${s.unchanged||0} · 附属 ${s.sidecars||0} · 清理 ${s.deleted||0}${s.skipped?` · 跳过 ${s.skipped}`:''}</div>`+
`<div class="kv"><b>下次计划：</b>${next} · cron：${s.cron||'—'}</div>`+
`<div class="kv"><b>断点记录：</b>${s.checkpoint_dirs?`已记录 ${s.checkpoint_dirs} 个目录，下次「全量同步」将跳过它们；点「🗑️ 清理断点」可重新完整扫描`:`暂无——第一次全量同步完整跑完后才会积累`}</div>`+
(s.error?`<div class="kv" style="color:var(--red)">${s.error}</div>`:'')+(s.life_error?`<div class="kv" style="color:var(--orange)">${s.life_error}</div>`:'')+(s.scan_notice?`<div class="kv" style="color:var(--orange)">${s.scan_notice}</div>`:'')+(s.skipped?`<div class="kv" style="color:var(--orange)">上次同步跳过 ${s.skipped} 个网盘异常条目（未生成 STRM），原因见「运行日志」</div>`:'')}
async function refreshSync(){try{let s=await api('/strm/status');renderSyncStatus(document.querySelector('#full-status'),s);renderSyncStatus(document.querySelector('#incr-status'),s);syncRunning=s.state==='running';applyGates()}catch(e){}}
async function startSync(mode){try{await api('/strm/'+mode,{method:'POST'});toast(mode==='full'?'全量同步已开始':'增量同步已开始','ok');setTimeout(refreshSync,800)}catch(e){}}
async function stopSync(){try{let d=await api('/strm/stop',{method:'POST'});toast(d.message||(d.ok?'同步已停止':'当前没有正在进行的同步'),d.ok?'ok':'err');setTimeout(refreshSync,800)}catch(e){toast('停止失败：'+(e.message||e),'err')}}
async function clearCheckpoint(){if(!confirm('清除断点记录后，下次「全量同步」将从头完整扫描所有目录。\n\n一般无需使用；仅当需要重新完整核对时才清除。\n\n确认清除？'))return;try{let d=await api('/strm/clear-checkpoint',{method:'POST'});toast('已清除 '+d.cleared+' 个映射的断点记录','ok')}catch(e){toast('清除失败：'+(e.message||e),'err')}}

// ---------- 存量 STRM 地址替换（2.1.0） ----------
async function rewriteStrm(preview){
 if(!preview&&!confirm('确认把存量 STRM 内的播放地址替换为当前配置的直连地址？建议先点「预览」确认数量。'))return;
 let badge=document.querySelector('#rewrite-result');badge.hidden=true;
 try{let d=await api('/strm/rewrite',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dry_run:preview})});
 let parts=[`目标地址：${d.target}`,`记录 ${d.scanned} 个`,d.dry_run?`可替换 ${d.rewritten}`:`已替换 ${d.rewritten}`,`已是最新 ${d.unchanged}`];
 if(d.missing)parts.push(`文件缺失 ${d.missing}`);
 if(d.modified)parts.push(`已被修改跳过 ${d.modified}`);
 if(d.mismatched)parts.push(`其他地址跳过 ${d.mismatched}`);
 if(d.foreign)parts.push(`非本插件文件 ${d.foreign}`);
 badge.className='badge '+(d.rewritten||d.unchanged?'ok':'err');
 badge.textContent=parts.join(' · ')+(d.examples&&d.examples.length?'　示例：'+d.examples[0]:'');
 badge.hidden=false;
 toast(preview?'预览完成，未修改任何文件':'地址替换完成，播放即可恢复','ok');
 if(!preview)refreshSync();
 }catch(e){}}

// ---------- 网盘文件 ----------
function button(label,fn){let b=document.createElement('button');b.className='btn ghost';b.textContent=label;b.onclick=fn;return b}
async function refresh(){try{let s=await api('/status');allowChanges=s.allow_cloud_changes;document.querySelectorAll('.mut').forEach(e=>e.hidden=!allowChanges)}catch(e){}}
async function browse(){try{const cid=document.querySelector('#cid').value;let d=await api('/browse?cid='+encodeURIComponent(cid)+'&offset='+offset);pageSize=d.items.length||pageSize;let table=document.querySelector('#files');table.replaceChildren();for(let f of d.items){let tr=document.createElement('tr');let name=document.createElement('td');name.textContent=(f.is_dir?'📁 ':'🎞 ')+f.name;let size=document.createElement('td');size.textContent=f.is_dir?'—':(f.size/1024/1024).toFixed(1)+' MiB';let controls=document.createElement('td');controls.className='mutbar';if(f.is_dir)controls.append(button('打开',()=>{document.querySelector('#cid').value=f.id;offset=0;browse()}));else if(f.pickcode){let a=document.createElement('a');a.textContent='下载';a.href=base+'/download/'+encodeURIComponent(f.pickcode);controls.append(a);controls.append(button('诊断',()=>playTest(f.pickcode,f.name)))}if(allowChanges){controls.append(button('改名',()=>change('rename',f)),button('移动',()=>change('move',f)),button('复制',()=>change('copy',f)),button('回收站',()=>change('delete',f)))}tr.append(name,size,controls);table.append(tr)}}catch(e){}}
async function change(action,f){let name='',parent_id=document.querySelector('#cid').value;if(action==='rename'){name=prompt('新名称（包含原扩展名）',f.name);if(!name)return}if(action==='move'||action==='copy'){parent_id=prompt('目标目录 ID');if(!parent_id)return}if(!confirm(`确认对「${f.name}」执行${{rename:'改名',move:'移动',copy:'复制',delete:'移到回收站'}[action]}？`))return;try{await api('/files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,file_id:f.id,parent_id,name,confirmed:true})});await browse()}catch(e){}}
async function newFolder(){let name=prompt('新目录名称');if(!name)return;try{await api('/files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'mkdir',parent_id:document.querySelector('#cid').value,name,confirmed:true})});await browse()}catch(e){}}
async function playTest(pc,name){
 let box=document.querySelector('#diag-report');box.textContent='正在检测：直链签发 → CDN 拉流探测……';
 document.querySelector('#diag-modal').hidden=false;
 try{
  let r=await api('/play/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pickcode:pc})});
  let lines=['文件：'+(name||pc),'提取码：'+pc,'','—— 直链签发（proapi downurl）——'];
  for(let s of r.sign){let t=s.ok?('✓ '+s.label+'  f='+s.f+'  host='+s.host):('✗ '+s.label+'  '+(s.error||''));lines.push(t)}
  lines.push('','—— CDN 拉流探测（Range 0-1023）——');
  if(r.cdn.length){for(let c of r.cdn)lines.push((c.ok?'✓ HTTP ':'✗ HTTP ')+c.status+'  '+c.label)}else lines.push('（签发失败，跳过拉流探测）');
  lines.push('','提示：签发全部失败多为凭据/风控问题；签发成功但拉流探测有 ✗，说明直链绑定 UA 或 Cookie——切换「播放方式 → 服务器中转」即可播放。');
  box.textContent=lines.join('\n');
 }catch(e){box.textContent='诊断请求失败：'+e}}


// ---------- boot ----------
initChips();initTags();applyGates();refreshLogs();
document.querySelector('#f-sync_cron').addEventListener('input',previewCron);
document.querySelector('#f-strm_base_url').addEventListener('input',renderExample);
document.querySelector('#f-cloud_cid').addEventListener('change',resolveCidPath);
const BOOT_HASH=location.hash.slice(1);
loadConfig().then(()=>{resolveCidPath();let v=(config&&config.values)||{};if(!BOOT_HASH&&!v.cloud_cid&&!v.mappings)go('guide')});refreshAccount();refreshSync();refresh();
go(Object.prototype.hasOwnProperty.call(PAGES,location.hash.slice(1))||['files','logs','guide'].includes(location.hash.slice(1))?location.hash.slice(1):'account');
setInterval(()=>{refreshAccount();refreshSync();refreshLogs()},5000);
</script></html>'''
