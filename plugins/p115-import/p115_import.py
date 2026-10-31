"""115 分享转存入库：扫码登录 115，把分享里的片子按媒体库规则转存、改名、放进对应库目录。

两条链路（批量异名改名使用网页接口）：
- 登录：扫码拿网页 Cookie（**转存必须用它**，115 开放平台没有转存接口）；
- 文件操作：批量异名改名走网页接口；建目录、移动等有授权时优先走官方 OpenAPI。

流程：识别（走 MovieClaw 自己的 TMDB 收敛与媒体库路由）→ 和库里已有的同集文件比体积
（只保留最大的那份）→ 转存进库网盘目录下的隐藏暂存目录 → 按命名模板改名 → 移进正式目录 →
媒体库逐文件核验新文件后保留旧版本，单独预览并明确确认具体清单后才清理旧版。

对外命令（插件路由自动成为 mclaw 命令）：
    mclaw plugins p115-import auth web start --app apple_tv
    mclaw plugins p115-import auth web status <id> --wait-seconds 120
    mclaw plugins p115-import auth open start
    mclaw plugins p115-import plan <链接>
    mclaw plugins p115-import apply <链接> --yes
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr

from movieclaw_api.plugins.keys import HOST_OPS, PLUGIN_CALLBACKS, PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.ops import OpsError
from movieclaw_sdk.jobs import (
    JOB_HANDLERS,
    PLUGIN_JOBS,
    JobBlocked,
    JobContext,
    JobRetry,
    RegisteredJobHandler,
    ResourceRef,
)

import p115api as api
import p115naming as naming
import p115recognition as recognition
import p115flow as flow
import p115batch as batch
import p115requests as requests
import p115links as links
import p115board as board
import p115repair as repair
import p115controls as controls
import p115cleanup as old_cleanup
from p115compat import cloud_scan_body, library_languages, naming_title
from p115storage import ChunkedStore

PLUGIN_ID = "p115-import"
VERSION = "0.4.7"
JOB_TRANSFER = "p115-import:transfer"
#: 库网盘目录下的暂存目录名（点开头，媒体库扫描会跳过它）
STAGING_DIR = ".p115-import"
#: 本机 MovieClaw 端口（同机内部转发用回环）
HOST_PORT = 3000
#: 一次最多处理多少个视频（防手滑把整库塞进来）
MAX_FILES = 400
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup")


class Settings(BaseModel):
    app_id: str = Field(
        "",
        pattern=r"^\d{0,20}$",
        title="115 开放平台 AppID",
        description="在 115 开放平台申请到的 AppID。填了并完成授权后，建目录/单项改名/移动/删除优先走官方 OpenAPI；批量异名改名使用已扫码的网页接口。留空则文件操作走网页接口。",
    )
    login_app: str = Field(
        "Apple TV 端",
        title="扫码登录端",
        description="登录 115 用的客户端类型。必须选一个你其他工具（MovieClaw 自己的网盘、CloudDrive2、OpenList）没在用的端，否则会互相踢下线。",
        json_schema_extra={
            "enum": [
                "Apple TV 端",
                "鸿蒙端",
                "Linux 客户端",
                "Mac 客户端",
                "Windows 客户端",
                "电视端",
                "微信小程序",
                "网页端",
            ],
        },
    )
    offline_enabled: bool = Field(
        True,
        title="允许电驴 / 磁力（115 云下载）",
        description="打开后 plan / apply 也接受 ed2k:// 与 magnet: 链接，由 115 云下载下到暂存目录再入库。关闭则只接受 115 分享链接。云下载会占用 115 的离线配额，且能否成功取决于 115 服务器上有没有这份资源。",
    )
    offline_retry: bool = Field(
        False,
        title="云下载失败自动重试",
        description="打开后云下载失败会在 5 分钟后自动再试一次（仍失败就停下报错）。默认关闭：115 那边失败通常重试也一样，报错给你更清楚。",
    )
    wecom_entry: bool = Field(
        True,
        title="接受企业微信转发的链接",
        description="打开后接受 wecom-notify 转发过来的 115 分享链接 / ed2k / 磁力并直接转存入库。企业微信的接收与发送都由 wecom-notify 负责，这里只做转存。",
    )
    wecom_user: str = Field(
        "",
        title="结果推给谁（企业微信 UserID）",
        description="转存结果推给这个人（成员账号，不是姓名）。谁能发链接由 wecom-notify 的白名单决定；这里只影响结果发给谁，留空则用 wecom-notify 设置里的接收人。",
    )
    wecom_push_url: str = Field(
        "",
        title="结果通知地址（wecom-notify 的推送接口）",
        description="填 wecom-notify 插件给出的签名推送地址；转存成功/失败都由它发消息。留空则结果只写日志。",
    )
    dry_run: bool = Field(
        False,
        title="只预演不执行",
        description="打开后 apply 只做识别与体积比较，不转存、不改名、不删任何东西。",
    )


# ---------------------------------------------------------------------- 小工具

_pan = None
_submit_lock = None


def get_pan(ctx, store):
    # 运行时共用连接池/令牌刷新锁，避免同账号并发刷新和每次请求重新握手。
    return _pan if _pan is not None else api.Pan(store, lambda: (ctx.config.app_id or "").strip())


def _fmt_size(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _basename(path):
    return os.path.basename((path or "").replace("\\", "/").rstrip("/"))


def _is_subtitle(name):
    return (name or "").lower().endswith(SUBTITLE_EXTS)


def _with_ext(stem, original):
    return f"{stem}{os.path.splitext(original)[1]}"


def _strip_dup_suffix(name):
    """115 重名时会加「(1)」，认回原名。"""
    stem, dot, ext = (name or "").rpartition(".")
    cleaned = re.sub(r"\s*\(\d+\)$", "", stem)
    return f"{cleaned}.{ext}" if dot else name


def decide(new_size, old_sizes):
    """同一个（片 / 季集）该不该用新文件替换库里的：返回 (动作, 原因)。

    规则：只保留体积最大的那份。
    - 库里没有 → 收下；
    - 库里已有同样大小 → 跳过（同一份文件，不重复占空间）；
    - 新的严格更大 → 收下，较小旧版本保留，需单独确认清理；
    - 新的更小或一样大 → 跳过，库里的不动。
    """
    sizes = [int(s or 0) for s in old_sizes]
    if not sizes:
        return "replace", ""
    if int(new_size or 0) in sizes:
        return "keep_old", "库里已有同样大小的文件"
    biggest = max(sizes)
    if int(new_size or 0) > biggest:
        return "replace", ""
    return "keep_old", f"库里已有更大的版本（{_fmt_size(biggest)}）"


LOGIN_CHOICES = [
    ("apple_tv", "Apple TV 端", "推荐：一般没人用它登录 115"),
    ("harmony", "鸿蒙端", "很少见，基本不会撞"),
    ("os_linux", "Linux 客户端"),
    ("os_mac", "Mac 客户端"),
    ("os_windows", "Windows 客户端"),
    ("tv", "电视端"),
    ("wechatmini", "微信小程序", "会踢掉手机上的 115 小程序登录"),
    ("web", "网页端", "会和浏览器里的 115 网页版互相踢下线"),
]



_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>115 扫码登录 · MovieClaw</title>
<style>
 :root{color-scheme:dark light}
 body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      max-width:640px;margin:0 auto;padding:28px 20px 60px;background:#0f1115;color:#e8eaf0}
 h1{font-size:20px;margin:0 0 6px}
 p.hint{color:#98a2b3;font-size:13px;margin:0 0 22px}
 label{display:block;font-size:13px;color:#c7cdda;margin:0 0 6px}
 select,button{font-size:15px;padding:9px 12px;border-radius:8px;border:1px solid #2a2f3a;
      background:#171a21;color:#e8eaf0;width:100%;box-sizing:border-box}
 button{cursor:pointer;background:#2f6feb;border-color:#2f6feb;font-weight:600;margin-top:14px}
 button:disabled{opacity:.55;cursor:default}
 .card{margin-top:22px;background:#151922;border:1px solid #232833;border-radius:14px;padding:20px;text-align:center}
 .qr{width:240px;height:240px;background:#fff;border-radius:10px;padding:10px;box-sizing:border-box;margin:0 auto}
 .qr img{width:100%;height:100%;display:block}
 .status{margin-top:16px;font-size:14px;color:#c7cdda;min-height:22px}
 .ok{color:#5ad17a} .err{color:#f4736f}
 code{background:#1d222c;padding:2px 6px;border-radius:5px;font-size:12.5px;word-break:break-all}
 .row{display:flex;gap:10px;align-items:center}
 .row>*{flex:1}
 small{color:#7b8494}
</style></head><body>
<h1>115 扫码登录</h1>
<p class="hint">选好登录端 → 点「生成二维码」→ 用 115 App 扫码确认。登录态加密存在插件里，之后转存都靠它。</p>
<label for="app">登录端</label>
<select id="app"></select>
<small>同一个登录端同一时间只能在线一个；选你别的工具（MovieClaw 网盘 / CloudDrive2 / OpenList）没在用的那个。</small>
<p class="hint" id="needlogin" hidden style="margin-top:12px;color:#f4736f">
  这个页面要带着 MovieClaw 的登录态才能打开。如果你是从别的设备打开的，请先在该设备登录 MovieClaw 再来。
</p>
<button id="go">生成二维码</button>
<div class="card" id="card" hidden>
  <div class="qr" id="qrbox"></div>
  <div class="status" id="status">等待扫码…</div>
</div>
<div class="card" id="done" hidden>
  <div class="status ok" id="donemsg"></div>
</div>
<h1 style="font-size:17px;margin-top:38px">开放平台授权（可选）</h1>
<p class="hint">填了 AppID 才需要：授权后建目录 / 单项改名 / 移动 / 删除优先走官方 OpenAPI；<b>转存与批量异名改名走扫码那套网页接口</b>（115 开放平台没有转存接口）。</p>
<div id="opensection" hidden>
  <button id="goopen">授权开放平台（AppID <span id="appidtext"></span>）</button>
  <div class="card" id="opencard" hidden>
    <div class="qr" id="openqrbox"></div>
    <div class="status" id="openstatus">等待授权…</div>
  </div>
</div>
<div id="opennote" class="hint" hidden>还没填 AppID：在插件「设置」里填上 115 开放平台 AppID，再回来这里授权。</div>
<script>
const choices = __CHOICES__;
const sel = document.getElementById('app');
choices.forEach(([v,tip,help]) => {
  const o = document.createElement('option');
  o.value = v; o.textContent = help ? `${tip} — ${help}` : tip;
  sel.appendChild(o);
});
sel.value = __DEFAULT__;
let timer = null;
async function api(path, body) {
  const r = await fetch(`/api/v1/plugins/p115-import${path}`, {
    method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type':'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.message || `HTTP ${r.status}`);
  return data.data || data;
}
function show(el){ el.hidden = false; }
document.getElementById('go').onclick = async () => {
  const btn = document.getElementById('go');
  btn.disabled = true;
  document.getElementById('card').hidden = true;
  document.getElementById('done').hidden = true;
  try {
    const res = await api('/auth/web/start', {app: sel.value});
    document.getElementById('qrbox').innerHTML =
      `<img alt="二维码" src="${res.qrcode_image}">`;
    document.getElementById('status').className = 'status';
    document.getElementById('status').textContent = `等待扫码（登录端：${res.app_title}）…`;
    show(document.getElementById('card'));
    const id = res.id;
    clearInterval(timer);
    timer = setInterval(async () => {
      try {
        const st = await api(`/auth/web/status?login_id=${encodeURIComponent(id)}&wait_seconds=0`);
        const s = document.getElementById('status');
        if (st.status === 'scanned') { s.textContent = '已扫码，请在手机上点确认…'; }
        else if (st.status === 'pending') { s.textContent = `等待扫码（登录端：${res.app_title}）…`; }
        else if (st.status === 'done') {
          clearInterval(timer); btn.disabled = false;
          document.getElementById('card').hidden = true; show(document.getElementById('done'));
          document.getElementById('donemsg').textContent = st.message || '登录成功';
        } else {
          clearInterval(timer); btn.disabled = false;
          s.className = 'status err'; s.textContent = st.message || '登录失败';
        }
      } catch (e) { /* 轮询失败不打断 */ }
    }, 2000);
  } catch (e) {
    btn.disabled = false;
    document.getElementById('card').hidden = true;
    show(document.getElementById('done'));
    const d = document.getElementById('donemsg');
    d.className = 'status err';
    d.textContent = /401|未登录|UNAUTHORIZED/i.test(e.message)
      ? '没有 MovieClaw 登录态：请先在本机登录 MovieClaw，再打开这个页面。'
      : e.message;
  }
};

// ---- 开放平台授权 ----
const APP_ID = __APP_ID__;
const openReady = __OPEN_READY__;
if (APP_ID) {
  document.getElementById('appidtext').textContent = APP_ID;
  document.getElementById('opensection').hidden = false;
  if (openReady) {
    document.getElementById('openstatus').textContent = '已授权，文件操作走 OpenAPI';
  }
} else {
  document.getElementById('opennote').hidden = false;
}
let openTimer = null;
const goOpen = document.getElementById('goopen');
if (goOpen) goOpen.onclick = async () => {
  goOpen.disabled = true;
  document.getElementById('opencard').hidden = true;
  try {
    const res = await api('/auth/open/start', {});
    document.getElementById('openqrbox').innerHTML = `<img alt="二维码" src="${res.qrcode_image}">`;
    document.getElementById('openstatus').textContent = '等待授权…';
    document.getElementById('opencard').hidden = false;
    clearInterval(openTimer);
    openTimer = setInterval(async () => {
      try {
        const st = await api(`/auth/open/status?login_id=${encodeURIComponent(res.id)}&wait_seconds=0`);
        const s2 = document.getElementById('openstatus');
        if (st.status === 'scanned') s2.textContent = '已扫码，请在手机上点确认…';
        else if (st.status === 'pending') s2.textContent = '等待授权…';
        else if (st.status === 'done') {
          clearInterval(openTimer); goOpen.disabled = false;
          document.getElementById('opencard').hidden = true;
          s2.textContent = 'OpenAPI 授权成功';
          document.getElementById('openstatus').textContent = '已授权，文件操作走 OpenAPI';
        } else {
          clearInterval(openTimer); goOpen.disabled = false;
          s2.className = 'status err'; s2.textContent = st.message || '授权失败';
        }
      } catch (e) {}
    }, 2000);
  } catch (e) {
    goOpen.disabled = false;
    alert(e.message);
  }
};
</script></body></html>"""


#: 设置里给用户看的中文名 → 115 的登录端标识（同时接受直接写标识）
LOGIN_LABELS = {title: value for value, title, *_ in LOGIN_CHOICES}
LOGIN_LABELS.update({value: value for value, *_ in LOGIN_CHOICES})


def login_app_of(*values):
    """把用户给的登录端（中文名或标识）换成 115 的标识；认不出时返回 None。"""
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        if text in LOGIN_LABELS:
            return LOGIN_LABELS[text]
        for title, key in LOGIN_LABELS.items():
            if text == title or text.startswith(title):
                return key
    return None


def _login_choices():
    return [[value, title, (rest[0] if rest else "")] for value, title, *rest in LOGIN_CHOICES]


def _login_page_html(ctx, openapi_ready=False):
    """登录页：选登录端 → 出二维码 → 轮询结果；配了 AppID 的还能就地授权 OpenAPI。"""
    import json as _json

    html = _LOGIN_PAGE
    html = html.replace("__CHOICES__", _json.dumps(_login_choices(), ensure_ascii=False))
    current = login_app_of(ctx.config.login_app) or "apple_tv"
    html = html.replace("__DEFAULT__", _json.dumps(current))
    html = html.replace("__APP_ID__", _json.dumps((ctx.config.app_id or "").strip()))
    html = html.replace("__OPEN_READY__", "true" if openapi_ready else "false")
    return html


def _looks_like_share(link: str) -> bool:
    """能不能当 115 分享链接解析（内部入库入口用它挡掉无关文本）。"""
    try:
        api.parse_share_link(link)
        return True
    except ValueError:
        return False


def _short(link: str, limit: int = 28) -> str:
    text = (link or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reason(exc: Exception, limit: int = 80) -> str:
    text = getattr(exc, "detail", None) or str(exc) or exc.__class__.__name__
    return str(text)[:limit]


def _qr_data_url(text):
    """把扫码地址画成二维码，返回 data URL（网页、终端、Markdown 都能直接显示）。

    不用 115 的二维码图片地址：那需要浏览器能连上 115，内网/反代场景经常显示不出来。
    """
    if not text:
        return ""
    try:
        import base64
        import io

        import qrcode
        import qrcode.image.svg

        image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        buffer = io.BytesIO()
        image.save(buffer)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{payload}"
    except Exception:  # noqa: BLE001 -- 画不出来就只给链接，不影响登录
        return ""


# ---------------------------------------------------------------------- 计划


class Plan(BaseModel):
    """一次导入的完整计划。apply 之前会重新算一遍，不复用旧结论。"""

    status: str = "ready"            # ready / ambiguous / not_found / nothing_to_do
    message: str = ""
    candidates: list[dict] = Field(default_factory=list)
    recognition: list[dict] = Field(default_factory=list)
    request_ref: str = ""
    unit_map: dict[str, list[int] | None] = Field(default_factory=dict)
    naming_context: dict = Field(default_factory=dict)

    share_code: str = ""
    receive_code: str = ""
    share_title: str = ""
    share_user: str = ""

    kind: str = "movie"
    tmdb_id: int | None = None
    title: str = ""
    year: int | None = None
    naming_title: str = ""
    naming_languages: list[str] = Field(default_factory=list)
    media_item_id: int | None = None

    library_id: int | None = None
    library_name: str = ""
    entry_dir_name: str = ""
    staging_dir_name: str = ""
    cloud_account_id: int = 0
    cloud_root_id: str = ""
    cloud_root_path: str = ""

    items: list[dict] = Field(default_factory=list)   # 要转存的文件
    skipped: list[dict] = Field(default_factory=list)
    old_files: list[dict] = Field(default_factory=list)
    receive_ids: list[str] = Field(default_factory=list)
    total_new_bytes: int = 0
    total_old_bytes: int = 0


async def _library_files(ops, library_id, media_item_id):
    """条目在这个库里的文件（体积、季集、网盘路径），用来比体积。"""
    if not media_item_id:
        return []
    detail = await ops.call(
        "library.items.get", {"library_id": library_id, "media_item_id": media_item_id}
    )
    return [
        {
            "file_id": row.get("id"),
            "name": row.get("file_name"),
            "size": int(row.get("size_bytes") or 0),
            "season": int(row.get("season_number") or 0),
            "episode": int(row.get("episode_number") or 0),
            "remote_path": ((row.get("cloud") or {}).get("remote_path") or ""),
            "cloud_only":bool(row.get("cloud")) and (row.get("cloud") or {}).get("local") is False
                         and (row.get("cloud") or {}).get("state")=="verified",
            "missing": bool(row.get("missing")) or row.get("state") not in (None, "in_place"),
        }
        for row in ((detail or {}).get("files") or [])
    ]


async def _existing_item(ops, library_id, tmdb_id, kind, details=None):
    """库里是否已有这部片：影片详情带出条目 id，再按库过滤它的文件。"""
    ref = f"tmdb:{'movie' if kind == 'movie' else 'tv'}:{int(tmdb_id)}"
    # 库存查询失败不能伪装成“没有”，否则会重复转存并跳过洗版保护。
    detail = details if details is not None else await ops.call(
        "discover.get-title-details", {"title_ref": ref})
    media_item_id = None
    for link in ((detail or {}).get("library_links") or []):
        if link.get("library_id") == library_id:
            media_item_id = link.get("media_item_id")
            break
    if media_item_id is None:
        status = ((detail or {}).get("title") or {}).get("library_status") or {}
        if status.get("library_count"):
            media_item_id = status.get("media_item_id")
    if media_item_id is None:
        return None, []
    return media_item_id, await _library_files(ops, library_id, media_item_id)


def _existing_dir_name(root_path, existing):
    """库里已经有这部片时，沿用它在网盘上的那个条目目录名。

    目录名是历史生成物（可能按旧模板、或被截断过），新文件放进另一个名字的目录
    就会变成两个条目；已有目录优先，只在没有时按当前模板新算一个。
    """
    prefix = (root_path or "").rstrip("/") + "/"
    for row in existing or ():
        remote = (row.get("remote_path") or "").replace("\\", "/")
        if not remote.startswith(prefix):
            continue
        rest = remote[len(prefix):]
        head = rest.split("/", 1)[0]
        if head:
            return head
    return ""


async def _target(ctx, ops, *, kind, title, year, tmdb_id, resolved):
    """复用宿主识别/分库结论，读取目标网盘目录与命名模板。"""
    if resolved.get("status") != "ready":
        return {"status": resolved.get("status") or "not_found",
                "message": resolved.get("message") or (
                    "无法确定作品，请补充片名和年份，不需要自行查编号"),
                "recognition": resolved.get("recognition") or [],
                "candidates": resolved.get("candidates") or []}

    tmdb_id = int(resolved["tmdb_id"])
    kind = resolved["kind"] or kind
    title = resolved.get("title") or title
    year = resolved.get("year") or year
    library_id = resolved.get("library_id")
    if not library_id:
        raise HTTPException(status_code=409, detail="没有可用的目标媒体库，请先在「媒体库」里建一个")

    libraries = await ops.call("library.list", {"scope": "all"})
    library = next((row for row in libraries or [] if row.get("id") == library_id), None)
    if library is None:
        raise HTTPException(status_code=409, detail=f"媒体库 {library_id} 不存在")
    folders = library.get("cloud_folders") or []
    primary = next((f for f in folders if f.get("is_primary")), None) or (
        folders[0] if folders else None)
    if primary is None:
        raise HTTPException(
            status_code=409,
            detail=f"媒体库「{library.get('name')}」没有挂在网盘上的目录；这个插件只能往网盘目录里放片",
        )
    if "115" not in str(primary.get("backend") or "115"):
        raise HTTPException(409,detail="目标库的主网盘不是115，不能把文件投到另一家网盘的目录编号")

    # 元数据与配置互不依赖，一次并行获取；同一份详情同时用于标准名和库存定位。
    scrape, details = await asyncio.gather(
        ops.call("scrape.show", {}),
        ops.call("discover.get-title-details", {"title_ref":f"tmdb:{kind}:{tmdb_id}"}),
    )
    templates = naming.Templates.from_settings((scrape or {}).get("setting") or {},
                                               library.get("scrape_overrides") or {})
    official = (details or {}).get("title") or {}
    title = official.get("title") or title
    year = official.get("release_year") or year
    languages = library_languages(scrape or {}, library.get("scrape_overrides") or {})
    active_templates = (templates.entry_dir,templates.movie_file) if kind=="movie" else (
        templates.entry_dir,templates.season_dir,templates.episode_file)
    needs_title = any(re.search(r"\{title(?::[^}]*)?\}", value) for value in active_templates)
    canonical_title = naming_title(details or {},languages,getattr(ctx,"locale","zh-Hans"),needs_title=needs_title)
    media_item_id, existing = await _existing_item(ops, library_id, tmdb_id, kind, details=details)
    identity = {"original_title":official.get("original_title"),
                "english_title":((details or {}).get("metadata") or {}).get("english_title"),
                "imdb_id":None,"douban_id":None}
    # 台账已有作品时，可用其真实季号给本体批次季集仲裁提供守卫。
    known_seasons = sorted({row["season"] for row in existing if row["season"] > 0})

    return {
        "status": "ready",
        "tmdb_id": tmdb_id,
        "kind": kind,
        "title": title,
        "year": year,
        "library_id": library_id,
        "library_name": library.get("name") or "",
        "media_item_id": media_item_id,
        "templates": templates,
        "naming_title": canonical_title,
        "naming_languages": languages,
        "naming_context": identity,
        "known_seasons": known_seasons,
        "cloud_folder_id": primary.get("id"),
        "cloud_account_id": int(primary.get("account_id") or 0),
        "cloud_root_id": str(primary.get("remote_dir_id") or ""),
        "cloud_root_path": primary.get("display_path") or "",
        # 目录名：库里已有就沿用（历史目录名可能被截断过），否则自己按模板渲染。
        # 注意不能用 dl.resolve-target 给的 entry_dir：条目还没建档时它渲染不出
        # {tmdb_id}，会把 [tmdb=…] 整组丢掉，落成「魔女嘉莉-2026」这种缺 tmdb 的名字。
        "entry_dir_name": _existing_dir_name(primary.get("display_path") or "", existing)
        or naming.entry_name(templates, canonical_title, year, tmdb_id, **identity),
        "existing": existing,
    }


def _unit_of(rel_path, kind, name):
    """一个文件在库里的落点：季集号、目标文件名（不含扩展名）、季目录名。"""
    if kind == "tv":
        season, episode = naming.episode_from_path(rel_path or name)
        if episode is None:
            return None
        return season, episode
    return 0, 0


def _plan_entry(target, *, rel_path, size, name, share_id="", subs=(), old_rows=(), unit=None):
    """算出一个文件的目标名字、动作与要删掉的旧版本。"""
    tpl = target["templates"]
    kind = target["kind"]
    canonical_title = target.get("naming_title") or target["title"]
    unit = unit if unit is not None else _unit_of(rel_path, kind, name)
    if unit is None:
        return None
    season, episode = unit
    attrs = recognition.parsed(name)
    resolution = attrs.get("resolution") or naming.resolution_of(name)
    extra = dict(target.get("naming_context") or {})
    extra.update(media_source=attrs.get("media_source"),release_group=attrs.get("release_group"),
                 video_codec=attrs.get("video_codec"),hdr=" ".join(attrs.get("hdr") or []) or None,
                 audio=" ".join(attrs.get("audio") or []) or None,release_name=os.path.splitext(name)[0])
    if kind == "tv":
        stem = naming.episode_file_name(
            tpl, canonical_title, target["year"], season, episode,
            resolution=resolution, tmdb_id=target["tmdb_id"], **extra,
        )
        season_dir = naming.season_dir_name(tpl, season, canonical_title, target["year"],
                                            tmdb_id=target["tmdb_id"],**(target.get("naming_context") or {}))
    else:
        stem = naming.movie_file_name(
            tpl, canonical_title, target["year"], tmdb_id=target["tmdb_id"],
            resolution=resolution, **extra,
        )
        season_dir = ""
    old_rows = [row for row in old_rows if not row.get("missing")]
    if not size and old_rows:
        action, why = "replace", "体积待云下载完成后与实时库存比较，尚不删除旧文件"
    else:
        action, why = decide(size, [row["size"] for row in old_rows])
    return {
        "share_path": rel_path or name,
        "share_id": share_id,
        "size": size,
        "resolution": resolution,
        "season": season,
        "episode": episode,
        "season_dir": season_dir,
        "file_name": _with_ext(stem, name),
        "action": action,
        "reason": why,
        "subs": list(subs),
        "existing": [
            {"file_id": row["file_id"], "name": row["name"], "size": row["size"],
             "remote_path": row["remote_path"], "missing": row["missing"]}
            for row in old_rows
        ],
    }


async def _recognize_input(ctx, ops, share_title, paths, body):
    if _core_parser is None:
        raise HTTPException(status_code=503, detail="本体名称解析器尚未就绪")
    try:
        await _core_parser.ensure()
        evidence = await asyncio.to_thread(recognition.collect_evidence, share_title, paths, body)
        result = await recognition.recognize(ops, evidence, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="尚未批准读取本体名称识别模型，请在插件权限中确认只读模型目录") from exc
    except OpsError as exc:
        raise HTTPException(status_code=502, detail=f"MovieClaw 作品识别暂时失败：{exc.message}；未执行转存") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return evidence, result


_core_parser = None


def _finalize_selection(base):
    try:
        chosen, discarded = flow.selected_items(base.items,base.kind)
    except ValueError as exc:
        raise HTTPException(409,detail=str(exc)) from exc
    for row in discarded:
        row["action"]="keep_batch"
        row["reason"]="同批同季集已有更大或相同版本，只转存一份"
        base.skipped.append({"file":row["share_path"],"size":row["size"],"reason":row["reason"]})
    base.receive_ids=list(dict.fromkeys(x for row in chosen for x in [row["share_id"],*(row.get("sub_ids") or [])]))
    base.total_new_bytes=sum(int(row.get("size") or 0) for row in chosen)
    units={(row["season"],row["episode"]) for row in chosen}
    old={row["file_id"]:row for row in base.old_files if (row["season"],row["episode"]) in units}
    base.old_files=list(old.values())
    base.total_old_bytes=sum(int(row.get("size") or 0) for row in base.old_files)


async def build_plan(ctx, body) -> dict:
    """识别 + 路由 + 比体积，算出这次要做什么。只读，不改任何东西。

    支持两类来源：
    - 115 分享链接（转存，秒级）；
    - 电驴 / 磁力 / 直链（走 115 云下载；ed2k 带体积，能先比体积再决定要不要下）。
    """
    link = (body.link or "").strip()
    if not link:
        raise HTTPException(status_code=400, detail="请提供 115 分享链接，或电驴 / 磁力链接")

    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    pan = get_pan(ctx, store)
    ops = await ctx.use(HOST_OPS).client(ctx)

    offline = api.parse_offline(link)
    if offline:
        if not ctx.config.offline_enabled:
            raise HTTPException(
                status_code=400,
                detail="插件设置里「允许电驴 / 磁力（115 云下载）」是关的；要用电驴/磁力请先打开它",
            )
        return await _offline_plan(ctx, pan, ops, offline, body)

    try:
        share_code, receive_code = api.parse_share_link(link)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}；也支持电驴（ed2k://…）和磁力链",
        ) from exc

    try:
        meta, tree = await asyncio.gather(
            pan.share_meta(share_code, receive_code),
            pan.share_tree(share_code, receive_code, max_files=MAX_FILES * 8),
        )
    except api.ShareLocked as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except api.ShareGone as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except api.AuthExpired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (api.RateLimited,api.RiskControl):
        raise
    except api.ApiError as exc:
        if exc.code in api.BAD_PARAM_CODES or any(word in exc.message for word in ("安全上限","目录过多")):
            raise HTTPException(status_code=400,detail=exc.message) from exc
        raise HTTPException(status_code=502,detail=exc.message) from exc

    videos = [
        f for f in tree
        if not f.is_dir and naming.is_video(f.name) and not naming.is_sample(f.name)
    ]
    if not videos:
        raise HTTPException(status_code=400, detail="这个分享里没有找到视频文件")
    if len(videos) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"这个分享有 {len(videos)} 个视频，超过单次上限 {MAX_FILES} 个；请一次只给我一部片（或一季）",
        )

    evidence, resolved = await _recognize_input(
        ctx, ops, meta.get("title") or "", [v.rel_path or v.name for v in videos], body,
    )
    kind = resolved.get("kind") or evidence["kind"]
    base = Plan(
        share_code=share_code,
        receive_code=receive_code,
        share_title=meta.get("title") or "",
        share_user=meta.get("user") or "",
        kind=kind,
        title=resolved.get("title") or body.title or "",
        year=resolved.get("year") or body.year,
        recognition=resolved.get("recognition") or [],
    )
    target = await _target(ctx, ops, kind=kind, title=base.title, year=base.year,
                           tmdb_id=body.tmdb_id, resolved=resolved)
    if target.get("status") != "ready":
        base.status = target.get("status") or "not_found"
        base.candidates = target.get("candidates") or []
        base.message = target.get("message") or "没能确定是哪部作品"
        return base.model_dump()

    for key in ("tmdb_id", "kind", "title", "year", "library_id", "library_name",
                "entry_dir_name", "cloud_account_id", "cloud_root_id", "cloud_root_path",
                "media_item_id"):
        setattr(base, key, target[key])
    base.naming_context = target.get("naming_context") or {}
    base.naming_title = target.get("naming_title") or base.title
    base.naming_languages = target.get("naming_languages") or []
    base.staging_dir_name = f"{STAGING_DIR}/share-{share_code}-{flow.digest([base.kind,base.tmdb_id,base.library_id])[:10]}"
    paths = [v.rel_path or v.name for v in videos]
    if base.kind == "tv":
        base.unit_map = {p:list(u) if u else None for p,u in
            naming.batch_units(paths,entry_name=base.share_title,known_seasons=target.get("known_seasons")).items()}

    old_by_unit: dict[tuple[int, int], list[dict]] = {}
    for row in target["existing"]:
        key = (row["season"], row["episode"]) if base.kind == "tv" else (0, 0)
        if not row.get("missing"):
            old_by_unit.setdefault(key, []).append(row)

    # 视频同名字幕（同一目录下、名字前缀一致）
    subs_by_video: dict[str, list[str]] = {}
    for sub in tree:
        if sub.is_dir or not _is_subtitle(sub.name):
            continue
        parent = sub.rel_path.rpartition("/")[0]
        stem = os.path.splitext(sub.name)[0]
        for video in videos:
            vparent = video.rel_path.rpartition("/")[0]
            vstem = os.path.splitext(video.name)[0]
            if parent == vparent and (stem == vstem or stem.startswith(vstem + ".")):
                subs_by_video.setdefault(video.rel_path, []).append(sub.rel_path)
                break

    total_new = 0
    total_old = 0
    for video in videos:
        unit = base.unit_map.get(video.rel_path or video.name) if base.kind == "tv" else (0,0)
        if base.kind == "tv" and unit is None:
            base.skipped.append({"file":video.rel_path,"size":video.size,
                                 "reason":"本体批次季集仲裁未通过，不能默认塞进第1季"})
            continue
        old = old_by_unit.get(tuple(unit or (0, 0)), [])
        entry = _plan_entry(
            dict(target, templates=target["templates"]),
            rel_path=video.rel_path, size=video.size, name=video.name,
            share_id=video.share_id, subs=subs_by_video.get(video.rel_path, []),
            old_rows=old, unit=tuple(unit),
        )
        if entry is not None:
            entry["sha1"] = video.sha1
            entry["sub_ids"] = [s.share_id for s in tree if s.rel_path in entry.get("subs",[])]
        if entry is None:
            base.skipped.append({
                "file": video.rel_path,
                "size": video.size,
                "reason": "认不出第几季第几集（要有 SxxEyy / EPxx / 第xx集 或 Season 目录），已跳过",
            })
            continue
        base.items.append(entry)
        if entry["action"] != "replace":
            base.skipped.append({"file": video.rel_path, "size": video.size,
                                 "reason": entry["reason"] or "库里的版本不更小，保留原样"})
            continue
        total_new += entry["size"]
        base.receive_ids.append(entry["share_id"])
        for path in entry["subs"]:
            match = next((item for item in tree if item.rel_path == path), None)
            if match is not None:
                base.receive_ids.append(match.share_id)
        for row in old:
            if row["file_id"] is None:
                continue
            base.old_files.append({
                "file_id": row["file_id"],
                "name": row["name"],
                "size": row["size"],
                "remote_path": row["remote_path"],
                "season": entry["season"],
                "episode": entry["episode"],
                "media_item_id": base.media_item_id,
            })
            total_old += row["size"]

    _finalize_selection(base)
    total_new, total_old = base.total_new_bytes, base.total_old_bytes
    if base.receive_ids:
        base.status = "ready"
        base.message = (
            f"《{base.title}》{f'（{base.year}）' if base.year else ''} → "
            f"{base.library_name} / {base.entry_dir_name}："
            f"{sum(1 for i in base.items if i['action'] == 'replace')} 个文件进库"
            f"（{_fmt_size(total_new)}）"
            + (f"，旧版本 {len(base.old_files)} 个（{_fmt_size(total_old)}）保留，入库后可单独确认清理"
               if base.old_files else "")
            + (f"，{len(base.skipped)} 个跳过" if base.skipped else "")
        )
    else:
        have_existing=any(i.get("action")=="keep_old" for i in base.items)
        base.status = "nothing_to_do" if have_existing else "ambiguous"
        base.message = (f"《{base.title}》没有需要转存的文件：库里已有的都更大或一样大"
                        if have_existing else "作品已识别，但季集或文件落点不能确定，未转存，请补充季集信息")
    return base.model_dump()


async def _offline_plan(ctx, pan, ops, items: list, body) -> dict:
    """电驴 / 磁力：先按链接自带的名字与体积算，能判断"库里更大"就不消耗离线配额。"""
    if len(items) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多 {MAX_FILES} 个链接")
    if not await pan.open_login():
        raise HTTPException(
            status_code=409,
            detail="电驴 / 磁力走 115 云下载，需要开放平台授权：先填 AppID 并执行 auth open start",
        )

    names = [i.name for i in items]
    guessed = next((name for name in names if name), "")
    evidence, resolved = await _recognize_input(ctx, ops, "", names, body)
    kind = resolved.get("kind") or evidence["kind"]
    base = Plan(
        share_code="",
        receive_code="",
        share_title=guessed,
        share_user="云下载",
        kind=kind,
        title=resolved.get("title") or body.title or "",
        year=resolved.get("year") or body.year,
        recognition=resolved.get("recognition") or [],
    )
    target = await _target(ctx, ops, kind=kind, title=base.title, year=base.year,
                           tmdb_id=body.tmdb_id, resolved=resolved)
    if target.get("status") != "ready":
        base.status = target.get("status") or "not_found"
        base.candidates = target.get("candidates") or []
        base.message = target.get("message") or "没能确定是哪部作品"
        return base.model_dump()

    for key in ("tmdb_id", "kind", "title", "year", "library_id", "library_name",
                "entry_dir_name", "cloud_account_id", "cloud_root_id", "cloud_root_path",
                "media_item_id"):
        setattr(base, key, target[key])
    digest = hashlib.sha1(
        "\n".join(sorted(i.info_hash or i.url for i in items)).encode()
    ).hexdigest()[:12]
    base.staging_dir_name = f"{STAGING_DIR}/offline-{digest}"
    base.naming_context = target.get("naming_context") or {}
    base.naming_title = target.get("naming_title") or base.title
    base.naming_languages = target.get("naming_languages") or []
    if base.kind == "tv":
        base.unit_map = {p:list(u) if u else None for p,u in naming.batch_units(
            names,entry_name=guessed,known_seasons=target.get("known_seasons")).items()}

    old_by_unit: dict[tuple[int, int], list[dict]] = {}
    for row in target["existing"]:
        key = (row["season"], row["episode"]) if base.kind == "tv" else (0, 0)
        if not row.get("missing"):
            old_by_unit.setdefault(key, []).append(row)

    total_new = 0
    total_old = 0
    for item in items:
        unit = base.unit_map.get(item.name) if base.kind == "tv" else (0,0)
        if base.kind == "tv" and unit is None:
            base.skipped.append({"file":item.name,"size":item.size,"reason":"季集信息不够，无法安全归位"})
            continue
        old = old_by_unit.get(tuple(unit or (0, 0)), [])
        entry = _plan_entry(
            target, rel_path=item.name, size=item.size, name=item.name,
            share_id=item.url, old_rows=old, unit=tuple(unit),
        )
        if entry is None:
            base.skipped.append({"file": item.name, "size": item.size,
                                 "reason": "认不出第几季第几集，已跳过"})
            continue
        entry["source"] = item.kind
        entry["info_hash"] = item.info_hash
        base.items.append(entry)
        if entry["action"] != "replace":
            base.skipped.append({"file": item.name, "size": item.size,
                                 "reason": entry["reason"] or "库里的版本不更小，保留原样"})
            continue
        total_new += entry["size"]
        base.receive_ids.append(item.url)
        for row in old:
            if row["file_id"] is None:
                continue
            base.old_files.append({
                "file_id": row["file_id"], "name": row["name"], "size": row["size"],
                "remote_path": row["remote_path"], "season": entry["season"],
                "episode": entry["episode"], "media_item_id": base.media_item_id,
            })
            total_old += row["size"]

    _finalize_selection(base)
    total_new, total_old = base.total_new_bytes, base.total_old_bytes
    if base.receive_ids:
        base.status = "ready"
        unknown = sum(1 for i in base.items
                      if i["action"] == "replace" and not i["size"])
        base.message = (
            f"《{base.title}》{f'（{base.year}）' if base.year else ''} → "
            f"{base.library_name} / {base.entry_dir_name}："
            f"{sum(1 for i in base.items if i['action'] == 'replace')} 个文件走 115 云下载"
            + (f"（{_fmt_size(total_new)}）" if not unknown else "（体积未知，下完再比）")
            + (f"，旧版本 {len(base.old_files)} 个保留，待单独确认清理" if base.old_files else "")
            + (f"，{len(base.skipped)} 个跳过" if base.skipped else "")
        )
    else:
        have_existing=any(i.get("action")=="keep_old" for i in base.items)
        base.status = "nothing_to_do" if have_existing else "ambiguous"
        base.message = (f"《{base.title}》不用下载：库里已有的都更大或一样大" if have_existing else
                        "作品已识别，但链接中没有可安全归位的季集或文件名，请补充信息")
    return base.model_dump()


async def preview_plan(ctx,body):
    """交互预览把已分类错误翻成人话；后台受理仍保留原异常类型供调度判断。"""
    try:
        return await build_plan(ctx,body)
    except (JobBlocked,api.RiskControl) as exc:
        raise HTTPException(409,detail=exc.message) from exc
    except api.RateLimited as exc:
        raise HTTPException(429,detail=exc.message) from exc
    except api.ApiError as exc:
        raise HTTPException(502,detail=exc.message) from exc


# ---------------------------------------------------------------------- 执行


async def _wait_job(ops,job_id,*,deadline=600):
    try:
        await flow.wait_scan(ops,job_id,deadline=deadline)
        return True,0
    except JobRetry:
        return False,0


async def _push_wecom(ctx, text: str) -> None:
    """把结果交给 wecom-notify 发（出口统一在那个插件，凭据与排版都归它）。"""
    url = (ctx.config.wecom_push_url or "").strip()
    port = HOST_PORT
    try:
        from movieclaw_api.core.config import get_settings as _gs

        port = int(getattr(_gs(), "port", HOST_PORT) or HOST_PORT)
    except Exception:  # noqa: BLE001 -- 拿不到就用默认端口
        pass
    if url and "/api/v1/hooks/" in url:
        # 同机调用走回环：不依赖反代与证书，路径里的密钥原样保留
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        url = urlunsplit(("http", f"127.0.0.1:{port}", parts.path, parts.query, ""))
    if not url:
        ctx.logger.info("没配 wecom-notify 的推送地址，结果只写日志：%s",
                        text.replace("\n", " | "))
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json={"text": text})
        if response.status_code >= 400:
            ctx.logger.warning("wecom-notify 没接受这条推送（HTTP %s）", response.status_code)
    except Exception as exc:  # noqa: BLE001 -- 通知失败不影响入库
        ctx.logger.warning("推给 wecom-notify 失败（%s），结果保留在活动任务中", exc.__class__.__name__)


async def run_plan(ctx, plan, context=None):
    """仅适配 115 搬运，持久化/重试/扫描/台账验收复用 MovieClaw。"""
    store=ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    ops=await ctx.use(HOST_OPS).client(ctx)
    pan=get_pan(ctx,store)
    pipeline=flow.Flow(ctx,pan,ops,store,context,plan,existing=_existing_item)
    return await pipeline.run()


# ---------------------------------------------------------------------- 登录


async def _login_start(ctx, body):
    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    pan = get_pan(ctx, store)
    app = login_app_of(body.app, ctx.config.login_app) or "apple_tv"
    if app not in api.LOGIN_APPS:
        raise HTTPException(status_code=400, detail=f"不认识的登录端：{body.app or ctx.config.login_app}")
    token = await pan.qr_token(app)
    flow_id = uuid.uuid4().hex[:12]
    await store.set("login_flow", {"id": flow_id, "app": app, **token,
                                   "created": int(time.time())}, secret=True)
    return {
        "id": flow_id,
        "app": app,
        "app_title": api.LOGIN_APPS[app],
        "qrcode_image": _qr_data_url(token["qrcode"]),
        "scan_url": token["qrcode"],
        "message": (
            f"用 115 App 扫码（登录端：{api.LOGIN_APPS[app]}）。"
            f"打不开图就手工打开：{token['qrcode']}"
        ),
    }


async def _login_status(ctx, login_id, wait_seconds=0):
    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    pan = get_pan(ctx, store)
    flow = await store.get("login_flow", default=None)
    if not flow or flow.get("id") != login_id:
        raise HTTPException(status_code=404, detail="这次登录已经结束，请重新执行 auth web start")
    deadline = time.time() + max(0, int(wait_seconds or 0))
    while True:
        status = await pan.qr_status(flow)
        if status == 1:
            return {"status": "scanned", "message": "已扫码，请在手机上点确认"}
        if status == 2:
            cookies = await pan.qr_result(flow["app"], flow["uid"])
            await pan.save_web_login(cookies, flow["app"])
            await store.delete("login_flow")
            try:
                name = await pan.account_name()
            except api.ApiError:
                name = ""
            return {"status": "done", "account": name,
                    "message": f"登录成功{f'：{name}' if name else ''}"}
        if status == -1:
            await store.delete("login_flow")
            return {"status": "expired", "message": "二维码过期了，请重新执行 auth web start"}
        if status == -2:
            await store.delete("login_flow")
            return {"status": "cancelled", "message": "手机上取消了登录"}
        if time.time() >= deadline:
            return {"status": "pending", "message": "还在等扫码",
                    "qrcode_image": _qr_data_url(flow.get("qrcode") or ""),
                    "scan_url": flow.get("qrcode")}
        await asyncio.sleep(1.0)


async def _open_start(ctx):
    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    pan = get_pan(ctx, store)
    app_id = (ctx.config.app_id or "").strip()
    if not app_id:
        raise HTTPException(status_code=400,
                            detail="先在插件「设置」里填上 115 开放平台 AppID，再来授权")
    token = await pan.open_device_code(app_id)
    flow_id = uuid.uuid4().hex[:12]
    await store.set("open_flow", {"id": flow_id, "app_id": app_id, **token,
                                 "created": int(time.time())}, secret=True)
    return {"id": flow_id, "app_id": app_id, "qrcode_image": _qr_data_url(token["qrcode"]),
            "scan_url": token["qrcode"],
            "message": f"用 AppID {app_id} 绑定的 115 账号扫码授权。打不开图就手工打开：{token['qrcode']}"}


async def _open_status(ctx, login_id, wait_seconds=0):
    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    pan = get_pan(ctx, store)
    flow = await store.get("open_flow", default=None)
    if not flow or flow.get("id") != login_id:
        raise HTTPException(status_code=404, detail="这次授权已经结束，请重新发起")
    deadline = time.time() + max(0, int(wait_seconds or 0))
    while True:
        status = await pan.qr_status(flow)
        if status == 1:
            return {"status": "scanned", "message": "已扫码，请在手机上点确认"}
        if status == 2:
            data = await pan.open_device_token(flow["uid"])
            await pan.save_open_login(data, flow["app_id"])
            await store.delete("open_flow")
            return {"status": "done", "message": "OpenAPI 授权成功"}
        if status == -1:
            await store.delete("open_flow")
            return {"status": "expired", "message": "二维码过期了，请重新授权"}
        if status == -2:
            await store.delete("open_flow")
            return {"status": "cancelled", "message": "手机上取消了授权"}
        if time.time() >= deadline:
            return {"status": "pending", "message": "还在等授权",
                    "qrcode_image": _qr_data_url(flow.get("qrcode") or "")}
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------- 插件本体


class PlanBody(BaseModel):
    link: str = Field(
        description="115 分享链接（…/s/xxx?password=1234 或「分享码-提取码」），"
                    "或电驴 ed2k:// 链接 / 磁力 magnet: 链接（走 115 云下载）"
    )
    tmdb_id: int | None = Field(None, description="确认过的 TMDB 条目 id（识别有歧义时从候选里挑）")
    kind: str | None = Field(None, description="movie / tv；不给就按文件名自动判断")
    title: str | None = Field(None, description="片名（识别不准时手动指定）")
    year: int | None = Field(None, description="年份（识别不准时手动指定）")


class ApplyBody(PlanBody):
    yes: bool = Field(False, description="确认执行；不加 --yes 只预演")
    notify: bool = Field(False, description="完成后把结果通知到已配的通知人（网页提交框默认勾选；命令行默认不打扰）")


class RecognizeBody(BaseModel):
    share_title: str = Field("", max_length=1000, description="分享原始标题（可为空）")
    paths: list[str] = Field(min_length=1, max_length=MAX_FILES, description="分享内原始视频相对路径列表，只识别，不转存")
    kind: str | None = Field(None, description="movie / tv；不给则从目录和文件推导")
    title: str | None = Field(None, max_length=100, description="可选的用户确认片名")
    year: int | None = Field(None, ge=1888, le=2100, description="可选年份")
    tmdb_id: int | None = Field(None, ge=1, description="可选明确身份，仍须本体核验")


class FixDirBody(BaseModel):
    library_id: int = Field(description="媒体库 id")
    media_item_id: int = Field(description="库内条目 id（library items list / search library 给的）")
    dry_run: bool = Field(False, description="只预演：说清楚会改成什么，不动网盘")


class RepairNamesBody(BaseModel):
    job_id: str = Field(description="已成功且核验入库的本插件转存任务编号")
    yes: bool = Field(False, description="确认还原临时名；不带yes只读取网盘真实名字预演")


class CleanupOldBody(BaseModel):
    job_id: str = Field(min_length=1, description="已成功核验的115转存任务编号")
    file_ids: list[int] = Field(default_factory=list, description="本次预览展示并明确确认删除的完整旧文件编号清单")
    approval_token: str | None = Field(None, description="本次预览返回的15分钟确认票据，不可复用历史清单")
    yes: bool = Field(False, description="明确确认删除预览中的具体旧文件与附属文件；不带yes只演练")


class CleanupStagingBody(BaseModel):
    library_id: int = Field(description="媒体库编号，仅检查该库主网盘根直下的.p115-import")
    expected_dir_id: str | None = Field(None, description="预演确认的暂存父目录编号；执行时必须一致")
    yes: bool = Field(False, description="确认清理；不带yes只列出完整内容并判空，不修改网盘")


class IngestBody(PlanBody):
    user: str = Field("", description="发起人（用于结果通知）")
    message_id: str = Field("", description="可选消息编号，用于入口重发追踪")


class LoginBody(BaseModel):
    app: str | None = Field(
        None,
        description="登录端：apple_tv / harmony / os_linux / os_mac / os_windows / tv / wechatmini / web",
    )


class CancelBody(BaseModel):
    job_id: str = Field(min_length=1, description="要取消的115入库任务编号")


class ResumeBody(BaseModel):
    job_id: str = Field(description="需要处理的任务编号（看板或任务页给的）")
    tmdb_id: int | None = Field(None, ge=1, description="从候选里确认的 TMDB 编号；给了就在原链接上重新提交")
    kind: str | None = Field(None, description="movie / tv；与编号成对给出")
    title: str | None = Field(None, max_length=100, description="可选的确认片名")
    year: int | None = Field(None, ge=1888, le=2100, description="可选的年份")


@plugin(
    PLUGIN_ID,
    title="115 分享转存入库",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_JOBS, PLUGIN_CALLBACKS, PLUGIN_FILES),
    permissions=(
        "dl.resolve-target",
        "discover.get-title-details",
        "library.list",
        "library.items.get",
        "library.scan.start",
        "library.items.delete-file",
        "jobs.show",
        "jobs.list",
        "jobs.retry",
        "jobs.cancel",
        "scrape.show",
    ),
    config=Settings,
)
async def apply(ctx: Context) -> None:
    global _core_parser, _pan, _submit_lock
    _core_parser = recognition.CoreParser(ctx.use(PLUGIN_FILES).scoped(ctx), ctx.logger)
    ctx.effect(_core_parser.close)
    store = ChunkedStore(ctx.use(PLUGIN_DATA).scoped(ctx))
    _pan=api.Pan(store,lambda:(ctx.config.app_id or "").strip())
    ctx.effect(_pan.close)
    _submit_lock=asyncio.Lock()
    routes = ctx.use(PLUGIN_ROUTES)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    ops = await ctx.use(HOST_OPS).client(ctx)
    runner=requests.RequestRunner(ctx,store,jobs,ops,plan_body=PlanBody,build_plan=build_plan,push=_push_wecom,dispatch_lock=_submit_lock)

    async def handler(context: JobContext, data: dict) -> dict:
        plan = await store.get(data["plan_ref"],default=None) if data.get("plan_ref") else data.get("plan")
        if not plan:
            raise JobBlocked("转存计划不存在，未执行任何修改，请重新提交链接")
        try:
            result=await run_plan(ctx,plan,context)
            ref=plan.get("request_ref")
            result["request_ref"]=ref
            result["job_id"]=context.job_id
            if ref:
                original=await store.get("input:"+ref,default={}) or {}
                checkpoint=await store.get("request-state:"+ref,default={}) or {}
                result.setdefault("metrics",{})["plan_seconds"]=checkpoint.get("plan_seconds")
                await store.set("request-result:"+ref,result)
                if original.get("user") and not checkpoint.get("notified"):
                    await _push_wecom(ctx,"【MovieClaw】"+result.get("message","已处理")
                        +(f"\n已核验，可在「{result.get('library_name') or '媒体库'}」播放" if result.get("inventory_verified") else ""))
                    checkpoint["notified"]=True
                    await store.set("request-state:"+ref,checkpoint,secret=True)
            return result
        except (api.AuthExpired,api.ShareGone,api.ShareLocked,api.Conflict) as exc:
            await requests.notify_pause(ctx,store,ops,context,plan.get("request_ref"),exc.message,_push_wecom)
            raise JobBlocked(exc.message) from exc
        except api.RiskControl as exc:
            await requests.notify_pause(ctx,store,ops,context,plan.get("request_ref"),exc.message,_push_wecom)
            raise JobBlocked(exc.message) from exc
        except (api.ApiError,OpsError) as exc:
            message = exc.message if isinstance(exc,api.ApiError) else "宿主接口暂时不可用"
            delay = getattr(exc,"retry_after",None) or 30
            await requests.notify_pause(ctx,store,ops,context,plan.get("request_ref"),message,_push_wecom,retry=True)
            raise JobRetry(message+"；检查点已保存，稍后自动继续",delay_seconds=delay) from exc
        except (JobBlocked, JobRetry) as exc:
            ctx.logger.warning("《%s》处理暂停：%s",plan.get("title") or "转存",exc.message)
            await requests.notify_pause(ctx,store,ops,context,plan.get("request_ref"),exc.message,_push_wecom,
                                        retry=isinstance(exc,JobRetry))
            raise

    ctx.contribute(JOB_HANDLERS,JOB_TRANSFER,RegisteredJobHandler(handler,frozenset({1}),title="115 转存与库存核验"))
    ctx.contribute(JOB_HANDLERS,requests.JOB,RegisteredJobHandler(runner.handle,frozenset({1}),title="115 链接受理与识别"))

    async def staging_root(library_id):
        libraries=await ops.call("library.list",{"scope":"all"})
        library=next((row for row in libraries if row.get("id")==library_id),None)
        if library is None:
            raise HTTPException(404,detail="媒体库不存在")
        folders=library.get("cloud_folders") or []
        primary=next((row for row in folders if row.get("is_primary")),None) or (folders[0] if folders else None)
        if primary is None or "115" not in str(primary.get("backend") or ""):
            raise HTTPException(409,detail="该库没有可用的115主网盘目录")
        return primary

    async def cleanup_handler(context,data):
        await context.raise_if_cancelled()
        primary=await staging_root(data["library_id"])
        if str(primary["remote_dir_id"])!=str(data["root_id"]) or int(primary["account_id"])!=int(data["account_id"]):
            raise JobBlocked("媒体库网盘位置已变化，未清理任何目录")
        return await flow.cleanup_empty_holder(_pan,primary["remote_dir_id"],context=context,
                                                expected_id=data["expected_dir_id"])

    ctx.contribute(JOB_HANDLERS,"p115-import:cleanup-staging",RegisteredJobHandler(
        cleanup_handler,frozenset({1}),title="清理115空暂存父目录"))

    async def repair_evidence(job_id):
        job=await ops.call("jobs.show",{"job_id":job_id})
        if job.get("job_type")!=JOB_TRANSFER or job.get("status")!="succeeded":
            raise JobBlocked("只能修复本插件已经成功核验的转存任务")
        plan_ref=(job.get("input_data") or {}).get("plan_ref") or ""
        if not plan_ref.startswith("transfer:"):
            raise JobBlocked("原任务缺少持久化计划引用")
        plan=await store.get(plan_ref,default=None)
        if not plan:
            raise JobBlocked("原任务计划不存在，未改名")
        key="flow:"+flow.digest([plan.get("request_ref") or plan.get("staging_dir_name"),plan.get("library_id"),plan.get("tmdb_id")])
        state=await store.get(key,default={}) or {}
        rows=await repair.inspect_names(_pan,plan,state,state.get("result") or job.get("result") or {})
        return plan,rows

    async def repair_handler(context,data):
        try:
            plan,rows=await repair_evidence(data["job_id"])
            await context.update_progress(mode="waiting",phase="organize",message="按原导入检查点核对远端ID并还原临时名")
            renamed=await repair.repair_names(_pan,rows,context)
            await staging_root(plan["library_id"])
            started=await ops.call("library.scan.start",{"library_id":plan["library_id"],"body":cloud_scan_body(plan)})
            await flow.wait_scan(ops,started["job_id"],context,deadline=600)
            detail=await ops.call("library.items.get",{"library_id":plan["library_id"],"media_item_id":data["media_item_id"]})
            cloud_paths={(r.get("cloud") or {}).get("remote_path") for r in detail.get("files",[]) if not r.get("missing")}
            if any(r["canonical_path"] not in cloud_paths for r in rows):
                raise JobRetry("远端已还原，媒体库展示路径尚未同步，稍后重查",delay_seconds=30)
            stale=[r["id"] for r in detail.get("files",[]) if " - p115-" in r.get("file_name","")]
            return {"status":"done","title":plan["title"],"media_item_id":data["media_item_id"],
                "checked":len(rows),"renamed":len(renamed),"remote_names_verified":True,
                "legacy_display_file_ids":stale,"scan_job_id":started["job_id"],
                "message":"已核验远端规范名与媒体库网盘路径；旧展示名需宿主支持同步" if stale else "规范名已还原并核验"}
        except api.ApiError as exc:
            raise JobRetry("115修名接口暂时异常，按文件ID安全续跑",delay_seconds=30) from exc

    ctx.contribute(JOB_HANDLERS,"p115-import:repair-names",RegisteredJobHandler(
        repair_handler,frozenset({1}),title="核验并还原115临时文件名"))

    async def restore_after_cleanup(plan,state,context,approved_paths):
        pipeline=flow.Flow(ctx,_pan,ops,store,context,plan,existing=_existing_item)
        pipeline.state=state
        renamed=await pipeline.restore_names(allowed_paths=approved_paths)
        await pipeline.verify_inventory()
        return renamed,pipeline.state

    async def cleanup_old_handler(context,data):
        try:
            return await old_cleanup.execute(ops,store,data,context,_existing_item,restore_after_cleanup)
        except (api.RiskControl,api.AuthExpired,api.ShareGone,api.ShareLocked,api.Conflict) as exc:
            raise JobBlocked(exc.message) from exc
        except (api.ApiError,OpsError) as exc:
            message=exc.message if isinstance(exc,api.ApiError) else "宿主清理接口暂时不可用"
            raise JobRetry(message+"；确认清单与检查点保留",delay_seconds=getattr(exc,"retry_after",None) or 30) from exc

    ctx.contribute(JOB_HANDLERS,old_cleanup.JOB,RegisteredJobHandler(
        cleanup_old_handler,frozenset({1}),title="清理明确确认的115旧文件"))

    router = APIRouter()

    @router.post("/cleanup-old",operation_id=f"plugins.{PLUGIN_ID}.cleanup-old",
                 summary="只预览洗版旧文件；确认具体路径与编号后才能清理（默认不删除）")
    async def cleanup_old_now(body: CleanupOldBody):
        try:
            if not body.yes:
                return await old_cleanup.preview(ops,store,body.job_id,_existing_item)
            return await old_cleanup.authorize(ops,store,jobs,body.job_id,body.file_ids,
                                                body.approval_token,_submit_lock)
        except JobBlocked as exc:
            raise HTTPException(409,detail=exc.message) from exc
        except OpsError as exc:
            raise HTTPException(exc.status if exc.status in (400,403,404,409) else 502,detail=exc.message) from exc

    # ------------------------------------------------------------------ 企业微信入口
    @router.get("/status", operation_id=f"plugins.{PLUGIN_ID}.status",
                summary="115 分享转存入库：登录与授权状态")
    async def status():
        pan = get_pan(ctx, store)
        web = await pan.web_login()
        openapi = await pan.open_login()
        name = ""
        if web:
            try:
                name = await pan.account_name()
            except api.ApiError:
                name = ""
        return {
            "plugin": PLUGIN_ID,
            "version": VERSION,
            "cloud_scoped_scan": True,
            "scan_polling": "adaptive-short-query",
            "old_file_cleanup": "explicit-preview-confirmation",
            "recognition_backend": "movieclaw_enrich + dl.resolve-target",
            "flow_backend": "MovieClaw 持久化任务 + 原生命名/批次季集 + 扫描/台账验收",
            "connection_pool": True,
            "rename_backend": "web-batch",
            "move_backend": "openapi-batch" if openapi else "web-batch",
            "batch_size": batch.BATCH_SIZE,
            "recognition_model_loaded": bool(_core_parser and _core_parser.ready),
            "recognition_model_directory": recognition.MODEL_DIRECTORY,
            "web_logged_in": bool(web),
            "web_login_app": (web or {}).get("app"),
            "web_login_at": (web or {}).get("at"),
            "account": name,
            "open_api_app_id": (openapi or {}).get("app_id") or (ctx.config.app_id or ""),
            "open_api_authorized": bool(openapi),
            "file_ops": "openapi" if openapi else ("web" if web else "none"),
            "dry_run": bool(ctx.config.dry_run),
            "offline_enabled": bool(ctx.config.offline_enabled),
            "offline_retry": bool(ctx.config.offline_retry),
            "wecom_entry": bool(ctx.config.wecom_entry),
            "wecom_user": ctx.config.wecom_user.strip(),
            "wecom_push_url_set": bool((ctx.config.wecom_push_url or "").strip()),
            "wecom_last_hit": await store.get("wecom_last_hit", default=None),
            "login_page": "/api/v1/plugins/p115-import/login",
            "login_page_note": "在已登录 MovieClaw 的浏览器里打开这个地址，选登录端就能出二维码",
        }

    @router.get("/login", response_class=HTMLResponse,
                operation_id=f"plugins.{PLUGIN_ID}.login-page",
                summary="115 扫码登录页（浏览器里打开）")
    async def login_page():
        pan = get_pan(ctx, store)
        return HTMLResponse(_login_page_html(ctx, bool(await pan.open_login())))

    @router.post("/auth/web/start", operation_id=f"plugins.{PLUGIN_ID}.auth.web.start",
                 summary="扫码登录 115（网页接口，转存要用）")
    async def web_start(body: LoginBody):
        return await _login_start(ctx, body)

    @router.get("/auth/web/status", operation_id=f"plugins.{PLUGIN_ID}.auth.web.status",
                summary="查询扫码登录结果")
    async def web_status(login_id: str, wait_seconds: int = 0):
        return await _login_status(ctx, login_id, wait_seconds)

    @router.post("/auth/reset", operation_id=f"plugins.{PLUGIN_ID}.auth.reset",
                 summary="清除已保存的 115 登录与 OpenAPI 授权")
    async def auth_reset():
        pan = get_pan(ctx, store)
        await pan.forget("web")
        await pan.forget("open")
        return {"message": "已清除 115 登录状态"}

    @router.post("/auth/open/start", operation_id=f"plugins.{PLUGIN_ID}.auth.open.start",
                 summary="用 AppID 授权 115 开放平台")
    async def open_start():
        return await _open_start(ctx)

    @router.get("/auth/open/status", operation_id=f"plugins.{PLUGIN_ID}.auth.open.status",
                summary="查询 OpenAPI 授权结果")
    async def open_status(login_id: str, wait_seconds: int = 0):
        return await _open_status(ctx, login_id, wait_seconds)

    @router.get("/check", operation_id=f"plugins.{PLUGIN_ID}.check",
                summary="自检：两条通道是否真能用（OpenAPI 取账号、网页接口列根目录）")
    async def check():
        pan = get_pan(ctx, store)
        out: dict[str, Any] = {"web": None, "openapi": None, "file_ops": None}
        try:
            cookies = await pan.require_web()
            listing = await pan._web_list("0")  # noqa: SLF001 -- 自检要看网页通道本身
            out["web"] = {"ok": True, "root_items": len(listing),
                          "cookies": sorted(cookies)}
        except api.AuthExpired as exc:
            out["web"] = {"ok": False, "error": exc.message}
        except api.ApiError as exc:
            out["web"] = {"ok": False, "error": exc.message}
        if await pan.open_login():
            try:
                info = await pan.open_json("GET", "/open/user/info", action="OpenAPI 自检")
                body = info.get("data") or {}
                listing = await pan._open_list("0")  # noqa: SLF001
                out["openapi"] = {
                    "ok": True,
                    "user_id": body.get("user_id"),
                    "user_name": body.get("user_name") or body.get("nickname"),
                    "root_items": len(listing),
                }
            except api.AuthExpired as exc:
                out["openapi"] = {"ok": False, "error": exc.message}
            except api.ApiError as exc:
                out["openapi"] = {"ok": False, "error": exc.message}
        else:
            out["openapi"] = {"ok": False, "error": "还没授权开放平台（可选）"}
        out["file_ops"] = "openapi" if (out["openapi"] or {}).get("ok") else (
            "web" if (out["web"] or {}).get("ok") else "none"
        )
        return out

    @router.post("/recognize", operation_id=f"plugins.{PLUGIN_ID}.recognize",
                 summary="只读识别原始分享目录/文件列表：复用本体解析与作品匹配，不转存、不改名、不删除")
    async def recognize_names(body: RecognizeBody):
        ops = await ctx.use(HOST_OPS).client(ctx)
        evidence, result = await _recognize_input(ctx, ops, body.share_title, body.paths, body)
        return dict(result, detected_kind=evidence["kind"], explicit_tmdb_ids=evidence["pins"])

    @router.post("/plan", operation_id=f"plugins.{PLUGIN_ID}.plan",
                 summary="预演：识别作品、比体积、算出要写进哪个目录")
    async def plan(body: PlanBody):
        return await preview_plan(ctx, body)

    @router.post("/apply", operation_id=f"plugins.{PLUGIN_ID}.apply",
                 summary="提交链接到可靠后台任务：识别→转存→本体扫描→库存核验（无 yes 仅预演）")
    async def apply_now(body: ApplyBody):
        if not body.yes:
            return {"status":"preview","plan":await preview_plan(ctx,body),
                    "message":"这是预演，没有执行转存；加 --yes 提交后台任务"}
        batches=links.split_inputs(body.link)
        submitted=[]
        for link in batches:
            submitted.append(await requests.submit(ctx,store,jobs,ops,body.model_copy(update={"link":link}),
                user=ctx.config.wecom_user.strip() if body.notify else "",lock=_submit_lock))
        return submitted[0] if len(submitted)==1 else {"status":"started","requests":submitted,
                  "message":f"已持久化接收 {len(submitted)} 批链接，各批独立处理"}

    @router.get("/board", operation_id=f"plugins.{PLUGIN_ID}.board",
                summary="入库看板：收件箱 / 正在处理 / 最近完成 / 需要处理（不含链接原文与提取码）")
    async def board_feed(limit: int = 60):
        # 按插件自己的任务类型分别取，避免被全局高频任务挤出视野
        per = max(10, min(200, int(limit or 60)))
        rows: list = []
        for job_type in (requests.JOB, JOB_TRANSFER, "p115-import:cleanup-staging", old_cleanup.JOB):
            found = await ops.call("jobs.list", {"job_type": job_type, "limit": per})
            items = found.get("items") if isinstance(found, dict) else found
            rows.extend(item for item in (items or []) if isinstance(item, dict))
        for job in rows:
            if job.get("job_type")==JOB_TRANSFER and job.get("status")=="succeeded":
                ref=board.ref_of(job)
                updated=await store.get("request-result:"+ref,default=None) if ref else None
                if updated and updated.get("inventory_verified"):
                    job["result"]=updated
        return board.build_feed(rows)

    @router.get("/request",operation_id=f"plugins.{PLUGIN_ID}.request",
                summary="查看一次链接请求的统一进度（自动跟进转存与库存核验任务）")
    async def request_status(job_id: str):
        try:
            state=await ops.call("jobs.show",{"job_id":job_id})
        except OpsError as exc:
            raise HTTPException(exc.status if exc.status in (400,403,404) else 502, detail=exc.message) from exc
        if not str(state.get("job_type") or "").startswith(PLUGIN_ID+":"):
            raise HTTPException(400,detail="这不是115导入任务")
        ref=(state.get("input_data") or {}).get("request_ref")
        result=state.get("result") or {}
        if not ref:
            ref=result.get("request_ref")
        if not ref and (state.get("input_data") or {}).get("plan_ref"):
            stored_plan=await store.get(state["input_data"]["plan_ref"],default={}) or {}
            ref=stored_plan.get("request_ref")
        if ref:
            checkpoint=await store.get("request-state:"+ref,default={}) or {}
            child_id=checkpoint.get("transfer_job_id")
            if child_id:
                state=await ops.call("jobs.show",{"job_id":child_id})
                result=(await store.get("request-result:"+ref,default={}) or state.get("result") or {}) if state.get("status")=="succeeded" else (state.get("result") or {})
            elif state.get("job_type")==JOB_TRANSFER and state.get("status")=="succeeded":
                result=await store.get("request-result:"+ref,default={}) or result
            elif not result:
                result=await store.get("request-result:"+ref,default={}) or {}
        return {"job_id":state["id"],"status":state["status"],"progress":state.get("progress"),
                "result":result,"error":state.get("error"),
                "retryable":state["status"] in ("failed","blocked","cancelled")}

    @router.post("/cancel", operation_id=f"plugins.{PLUGIN_ID}.cancel",
                 summary="取消115入库任务及已交接的转存子任务，保留文件与检查点")
    async def cancel_import(body: CancelBody):
        return await controls.cancel_request(ops, store, body.job_id, _submit_lock)

    @router.post("/resume", operation_id=f"plugins.{PLUGIN_ID}.resume",
                 summary="把卡住的任务接回：给了确认的作品编号就在原链接上重新提交，否则原地重试")
    async def resume(body: ResumeBody):
        try:
            state = await ops.call("jobs.show", {"job_id": body.job_id})
        except OpsError as exc:
            raise HTTPException(exc.status if exc.status in (400, 403, 404) else 502,
                                detail=exc.message) from exc
        if not str(state.get("job_type") or "").startswith(PLUGIN_ID + ":"):
            raise HTTPException(400, detail="这不是115导入任务")
        status = str(state.get("status") or "")
        if status not in ("failed", "blocked", "cancelled"):
            return {"status": "noop", "job_id": body.job_id, "job_created": False,
                    "message": "这条任务已经结束（或还在跑），不需要接回"}
        chosen = {key: value for key, value in (
            ("tmdb_id", body.tmdb_id), ("kind", body.kind),
            ("title", (body.title or "").strip() or None), ("year", body.year),
        ) if value}
        if not chosen:
            try:
                restarted = await ops.call("jobs.retry", {"job_id": body.job_id})
            except OpsError as exc:
                raise HTTPException(409, detail=exc.message) from exc
            actual = restarted.get("job") or restarted
            resumed = actual.get("id") or body.job_id
            return {"status": "started", "job_id": resumed, "job_created": False,
                    "message": "已把原任务重新排队，从原有检查点继续"}
        if str(state.get("job_type")) != requests.JOB:
            raise HTTPException(400, detail="这条任务已经过了识别阶段，直接重试即可；选作品只在链接受理任务上有用")
        ref = str((state.get("input_data") or {}).get("request_ref") or "")
        raw = await store.get("input:" + ref, default=None) if ref else None
        if not raw:
            raise HTTPException(404, detail="原链接资料已过保留期，请把链接重新粘贴到提交框")
        merged = {**raw, **chosen}
        body_in = PlanBody(**{key: value for key, value in merged.items() if key in PlanBody.model_fields})
        result = await requests.submit(ctx, store, jobs, ops, body_in,
                                       user=raw.get("user") or "",
                                       lock=_submit_lock)
        return {**result, "message": "已按确认的作品重新提交；原来那条任务可以在列表里忽略"}

    @router.post("/cleanup-staging",operation_id=f"plugins.{PLUGIN_ID}.cleanup-staging",
                 summary="检查/清理指定媒体库的空.p115-import父目录；有任何文件或批次子目录均保留")
    async def cleanup_staging_now(body: CleanupStagingBody):
        primary=await staging_root(body.library_id)
        checked=await flow.cleanup_empty_holder(_pan,primary["remote_dir_id"],
                                                expected_id=body.expected_dir_id,dry_run=True)
        if not body.yes or checked.get("status")!="preview":
            return checked
        if not body.expected_dir_id:
            raise HTTPException(400,detail="先预演确认目录编号，再带expected-dir-id与yes清理")
        ref=await jobs.create("p115-import:cleanup-staging",input={"library_id":body.library_id,
            "root_id":primary["remote_dir_id"],"account_id":primary["account_id"],
            "expected_dir_id":body.expected_dir_id},subject=".p115-import",
            dedupe_key="cleanup:"+str(primary["account_id"])+":"+str(primary["remote_dir_id"]),
            resources=[ResourceRef("p115-writer",str(primary["account_id"]),"target")])
        return {"status":"started","job_id":ref.id,"job_created":ref.created,
                "message":"已提交空父目录清理，执行前再次检查完整子项，不删除任何非空目录"}

    @router.post("/repair-names",operation_id=f"plugins.{PLUGIN_ID}.repair-names",
                 summary="根据成功任务的原始文件ID预演或还原洗版临时名，不删除任何文件")
    async def repair_names_now(body: RepairNamesBody):
        plan,rows=await repair_evidence(body.job_id)
        original=await ops.call("jobs.show",{"job_id":body.job_id})
        if not body.yes:
            return {"status":"preview","title":plan["title"],"files":rows,
                    "checked":len(rows),"needs_rename":sum(r["needs_rename"] for r in rows)}
        ref=await jobs.create("p115-import:repair-names",input={"job_id":body.job_id,
            "media_item_id":original["result"]["media_item_id"]},subject=plan["title"],
            dedupe_key="repair-names:"+body.job_id,
            resources=[ResourceRef("p115-writer",str(plan["cloud_account_id"]),"target")])
        return {"status":"started","job_id":ref.id,"created":ref.created}

    @router.post("/fix-dir", operation_id=f"plugins.{PLUGIN_ID}.fix-dir",
                 summary="把某个条目在网盘上的目录名改成当前命名模板的样子（带 tmdb 标记）")
    async def fix_dir(body: FixDirBody):
        ops = await ctx.use(HOST_OPS).client(ctx)
        pan = get_pan(ctx, store)

        libraries = await ops.call("library.list", {"scope": "all"})
        library = next((row for row in libraries or [] if row.get("id") == body.library_id), None)
        if library is None:
            raise HTTPException(status_code=404, detail=f"媒体库 {body.library_id} 不存在")
        folders = library.get("cloud_folders") or []
        primary = next((f for f in folders if f.get("is_primary")), None) or (
            folders[0] if folders else None)
        if primary is None:
            raise HTTPException(status_code=409, detail="这个媒体库没有挂在网盘上的目录")

        detail = await ops.call("library.items.get", {
            "library_id": body.library_id, "media_item_id": body.media_item_id,
        })
        rows = (detail or {}).get("files") or []
        if not rows:
            raise HTTPException(status_code=404, detail="这个条目在库里没有文件")

        root_path = (primary.get("display_path") or "").rstrip("/")
        prefix = root_path + "/"
        old_dir = ""
        for row in rows:
            remote = ((row.get("cloud") or {}).get("remote_path") or "").replace("\\", "/")
            if remote.startswith(prefix):
                old_dir = remote[len(prefix):].split("/", 1)[0]
                break
        if not old_dir:
            raise HTTPException(
                status_code=409,
                detail="这个条目的文件没有落在网盘库目录里（可能只在本地），这里不用改名",
            )

        scrape = await ops.call("scrape.show", {})
        templates = naming.Templates.from_settings((scrape or {}).get("setting") or {},library.get("scrape_overrides") or {})
        try:
            title = naming_title({"title":detail,"metadata":detail},
                library_languages(scrape or {},library.get("scrape_overrides") or {}),getattr(ctx,"locale","zh-Hans"),
                needs_title=bool(re.search(r"\{title(?::[^}]*)?\}",templates.entry_dir)))
        except JobBlocked as exc:
            raise HTTPException(409,detail=exc.message) from exc
        year = (detail or {}).get("year")
        tmdb_id = (detail or {}).get("tmdb_id")
        wanted = naming.entry_name(templates, title, year, tmdb_id)
        if not body.dry_run and wanted == old_dir:
            return {"status": "unchanged", "dir": old_dir,
                    "message": f"目录名已经是「{wanted}」，不用改"}
        if body.dry_run:
            return {"status": "preview", "old_dir": old_dir, "new_dir": wanted,
                    "message": f"「{old_dir}」→「{wanted}」（预演，没有改任何东西）"}

        # 网盘上按名字找到这个目录，改名（文件 id 不变，媒体库会按 id 认回原行）
        target = None
        for entry in await pan.list_dir(primary.get("remote_dir_id")):
            if entry.is_dir and entry.name == old_dir:
                target = entry
                break
        if target is None:
            raise HTTPException(status_code=404, detail=f"网盘上找不到目录「{old_dir}」")
        try:
            await pan.rename(target.remote_id, wanted)
        except api.Conflict as exc:
            raise HTTPException(status_code=409,
                                detail=f"网盘上已经有同名目录了：{exc.message}") from exc
        except api.ApiError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc

        # 让媒体库把台账里的展示路径跟上（按网盘文件 id 认回原来的行）
        scanned = False
        try:
            started = await ops.call("library.scan.start", {
                "library_id": body.library_id, "body": cloud_scan_body({"cloud_root_path":root_path,"entry_dir_name":wanted}),
            })
            job_id = (started or {}).get("job_id")
            if job_id:
                scanned, _ = await _wait_job(ops, job_id, deadline=300)
        except Exception as exc:  # noqa: BLE001 -- 改名已经成功，扫描失败只是台账慢一拍
            ctx.logger.warning("改名后触发扫描失败：%s", exc)

        return {
            "status": "done",
            "old_dir": old_dir,
            "new_dir": wanted,
            "scanned": scanned,
            "message": f"已把「{old_dir}」改名为「{wanted}」"
                       + ("，媒体库台账已同步" if scanned else "；扫描没跑成，几分钟后网盘同步也会跟上"),
        }

    @router.get("/wecom/ingest-url", operation_id=f"plugins.{PLUGIN_ID}.wecom.ingest-url",
                summary="取两段地址：入库接口（给 wecom-notify 转发用）与结果推送地址")
    async def ingest_url():
        ingest = await routes.sign(ctx, "/ingest", absolute=True)
        return {
            "ingest_url": ingest,
            "hint": "把这个地址填到 wecom-notify 插件设置里的「115 转存插件的入库地址」",
        }

    routes.mount(ctx, router)

    ingest_router = APIRouter()

    @ingest_router.post("/ingest", operation_id=f"plugins.{PLUGIN_ID}.ingest",
                        summary="入库接口（签名链接，供同机插件转发链接）")
    async def ingest(body: IngestBody):
        if not ctx.config.wecom_entry:
            raise HTTPException(status_code=409, detail="插件设置里「企业微信发链接直接入库」是关的")
        link = (body.link or "").strip()
        if not link:
            raise HTTPException(status_code=400, detail="link 不能为空")
        batches=links.split_inputs(body.link)
        if not batches or not all(api.parse_offline(value) or _looks_like_share(value) for value in batches):
            raise HTTPException(status_code=400, detail="只认 115 分享链接 / 电驴 / 磁力链接")
        submitted=[]
        for link in batches:
            submitted.append(await requests.submit(ctx,store,jobs,ops,body.model_copy(update={"link":link}),
                user=body.user or ctx.config.wecom_user,lock=_submit_lock))
        await store.set("wecom_last_hit",{"at":int(time.time()),"user":body.user,
                        "links":len(submitted),"result":"已持久化受理"})
        if len(submitted)==1:
            return dict(submitted[0],accepted=True)
        return {"accepted":True,"status":"started","requests":submitted}

    routes.mount(ctx, ingest_router, zone="public")
    ctx.logger.info(
        "%s 已加载（网页登录 %s，OpenAPI %s）",
        PLUGIN_ID,
        bool(await store.get("web_login", default=None)),
        bool(await store.get("open_login", default=None)),
    )
