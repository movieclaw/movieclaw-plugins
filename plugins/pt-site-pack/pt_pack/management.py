"""Admin-only plugin page. Reads redacted public APIs, retries via native site.verify."""
import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Query
from starlette.responses import HTMLResponse, Response
from movieclaw_sdk.ops import HOST_OPS, OpsError

PREFIX = "/api/v1/plugins/pt-site-pack"
ASSETS = Path(__file__).parent / "assets"
ICON_TYPES = {".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
VALID_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}


def redacted_error(value):
    value = str(value or "")
    value = re.sub(r"(?i)((?:cookie|authorization|password|passkey|torrent_pass|authkey|downhash|api[_-]?key|token|access_key)\s*[:=]\s*)[^\s&<>]+", r"\1[已隐藏]", value)
    value = re.sub(r"(/download/\d+/)[A-Za-z0-9_-]+", r"\1[已隐藏]", value)
    return re.sub(r"[A-Za-z0-9_-]{32,}", "[已隐藏]", value)[:500]


def public_url(value):
    p = urlparse(str(value or ""))
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    return f"{p.scheme}://{p.netloc}"  # No credential-bearing paths/query strings.


def domain(value):
    return (urlparse(str(value or "")).hostname or "").lower().removeprefix("www.")


def icon_asset(site_id):
    """Only packaged raster images, never an arbitrary URL or filesystem path."""
    if not VALID_ID.fullmatch(site_id):
        return None
    for extension in ICON_TYPES:
        path = ASSETS / "icons" / (site_id + extension)
        if path.is_file():
            return path
    return None


def merge_sites(catalog, configured, stats, officials, pack_sites, registrations, custom=None):
    """Adapter availability, saved credentials, and verification are independent states."""
    current = {s["site_id"]: s for s in catalog}
    accounts = {s["site_id"]: s for s in configured}
    custom = custom or {}
    own = {s["site_id"]: s for s in pack_sites}
    own.update({sid: r["config"] for sid, r in custom.items()})
    regs = {s["site_id"]: s["state"] for s in registrations}
    official_ids = {s["site_id"] for s in officials}
    official_metadata = {s["site_id"]: s for s in officials}
    official_domains = {domain(s.get(k)) for s in officials for k in ("base_url", "web_base_url")} - {""}
    rows = []
    for sid in sorted(set(current) | set(accounts) | set(own)):
        entry = current.get(sid) or own.get(sid) or {}
        metadata = official_metadata.get(sid) or own.get(sid) or {}
        website = public_url(entry.get("web_base_url")) or public_url(metadata.get("web_base_url")) or public_url(entry.get("base_url"))
        # Some host catalogs still advertise the old tp/API origin. This is
        # display navigation only; do not change the native API/auth endpoint.
        if sid == "mteam" and website in ("https://tp.m-team.cc", "https://api.m-team.cc"):
            website = "https://kp.m-team.cc"
        account = accounts.get(sid)
        official = sid in official_ids or domain(entry.get("base_url")) in official_domains
        source = "custom" if sid in custom else "official" if official else "plugin" if sid in own else "other"
        present = sid in current
        status = account.get("status", "unknown") if account else "unconfigured"
        enabled = bool(account.get("enabled")) if account else False
        verification_labels = {"active": "上次验证通过", "failed": "验证失败", "verifying": "验证中", "pending": "尚未验证", "unconfigured": "未验证"}
        state = "unconfigured" if account is None else "verifying" if status == "verifying" else "failed" if status == "failed" else "paused" if not enabled else "active" if status == "active" else "pending"
        if not present:
            state = "unavailable"
        available = present and account is not None and enabled and status == "active" and bool(account.get("usable"))
        sync = stats.get(sid) or {}
        if account is None:
            retry_reason = "请先在资源站点或浏览器扩展中配置账号"
        elif not present:
            retry_reason = "适配尚未启用或不可用"
        elif status == "verifying":
            retry_reason = "验证正在进行，不重复发起"
        else:
            retry_reason = None
        rows.append({
            "site_id": sid, "name": entry.get("display_name") or ("冬樱 WinterSakura" if sid == "wintersakura" else sid),
            "source": source, "source_label": {"official": "官方支持", "plugin": "插件适配", "custom": "本站自定义", "other": "其他适配"}[source],
            "adapter_available": present, "adapter_label": "适配可用" if present else "等待恢复 / 尚未适配" if sid == "wintersakura" else "插件适配已关闭" if regs.get(sid) == "disabled" else "当前目录无适配",
            "configured": account is not None, "connection_label": "已接入（已配置账号）" if account else "未接入",
            "verification": status, "verification_label": verification_labels.get(status, "未知验证状态"),
            "state": state, "enabled": enabled, "usable": available,
            "last_error": redacted_error(account.get("last_error")) if account else "",
            "last_checked_at": account.get("last_checked_at") if account else None,
            "last_verified_at": account.get("last_verified_at") if account else None,
            "auth_type": account.get("auth_type") if account else None,
            "base_url": public_url(entry.get("base_url")), "retry_allowed": retry_reason is None, "retry_reason": retry_reason,
            "website_url": website,
            "icon_url": f"{PREFIX}/icons/{sid}" if icon_asset(sid) else None,
            "torrent_count": sync.get("torrent_count"), "last_sync_at": sync.get("last_sync_at"),
            "next_sync_at": sync.get("next_sync_at"), "sync_error": redacted_error(sync.get("last_error")),
            "sync_interval_seconds": sync.get("sync_interval_seconds"),
        })
    return rows


def check_write_origin(request):
    """Cookies alone must not authorize cross-origin writes. CLI Bearer calls remain valid."""
    origin = request.headers.get("origin")
    if origin:
        p = urlparse(origin)
        if p.scheme != request.url.scheme or p.netloc.lower() != request.headers.get("host", "").lower() or p.path not in ("", "/"):
            raise HTTPException(403, "只允许当前 MovieClaw 页面的同源操作")
    elif not request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(403, "缺少同源校验，请从站点管理页面重试")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不接受跨站请求")


def management_router(ctx, officials, pack_sites, registrations, workspace=None):
    from .diagnostics import issue_ticket, TICKETS, REPORTS
    router = APIRouter()
    client, client_lock = None, asyncio.Lock()
    retry_locks, last_retry = {}, {}
    probe_lock, last_probe = asyncio.Lock(), {}

    async def ops():
        nonlocal client
        if client is None:
            async with client_lock:
                if client is None:
                    client = await ctx.use(HOST_OPS).client(ctx)
        return client

    async def snapshot(include_catalog=False):
        access = await ops()
        try:
            catalog, accounts, stats_result = await asyncio.gather(
                access.call("site.catalog", {}), access.call("site.list", {}),
                access.call("site.stats", {}), return_exceptions=True)
            if isinstance(catalog, Exception):
                raise catalog
            if isinstance(accounts, Exception):
                raise accounts
            warning = "同步统计暂时无法读取；接入与验证状态仍为最新" if isinstance(stats_result, Exception) else None
            custom = workspace.state["custom"] if workspace else {}
            rows = merge_sites(catalog, accounts, {} if warning else stats_result, officials, pack_sites, registrations, custom)
            if workspace:
                selected = set(workspace.state["selected"])
                for row in rows:
                    record = custom.get(row["site_id"])
                    row["custom_enabled"] = record["enabled"] if record else None
                    row["custom_archived"] = bool(record and record.get("archived"))
                    row["has_history"] = bool(record and record["history"])
                    row["user_agent"] = workspace.state["user_agents"].get(row["site_id"], "")
                    row["added"] = row["configured"] or row["site_id"] in selected or bool(record and not record.get("archived"))
                if not include_catalog: rows = [r for r in rows if r["added"] and not r["custom_archived"]]
        except OpsError as exc:
            raise HTTPException(exc.status, redacted_error(exc.message)) from None
        counts = {"total": len(rows), "supported": sum(r["adapter_available"] for r in rows),
                  "configured": sum(r["configured"] for r in rows), "usable": sum(r["usable"] for r in rows),
                  "failed": sum(r["verification"] == "failed" for r in rows),
                  "official": sum(r["source"] == "official" for r in rows),
                  "plugin": sum(r["source"] == "plugin" for r in rows),
                  "custom": sum(r["source"] == "custom" for r in rows)}
        return {"sites": rows, "counts": counts, "warning": warning,
                "updated_at": datetime.now(timezone.utc).isoformat(), "poll_seconds": 10,
                "note": "刷新只读取 MovieClaw 状态，不向 PT 站发起登录或验证；验证通过不代表搜索、详情和下载全部验收。"}

    @router.get("/panel", operation_id="plugins.pt-site-pack.panel", summary="打开 PT 站点管理子页面", openapi_extra={"x-cli-hidden": True})
    async def panel():
        return HTMLResponse(ASSETS.joinpath("panel.html").read_text(), headers=SECURITY_HEADERS)

    @router.get("/panel.css", operation_id="plugins.pt-site-pack.panel-style", openapi_extra={"x-cli-hidden": True})
    async def panel_style():
        return Response(ASSETS.joinpath("panel.css").read_text(), media_type="text/css", headers=SECURITY_HEADERS)

    @router.get("/panel.js", operation_id="plugins.pt-site-pack.panel-script", openapi_extra={"x-cli-hidden": True})
    async def panel_script():
        return Response(ASSETS.joinpath("panel.js").read_text(), media_type="application/javascript", headers=SECURITY_HEADERS)

    @router.get("/overview.js", operation_id="plugins.pt-site-pack.overview-script", openapi_extra={"x-cli-hidden": True})
    async def overview_script():
        return Response(ASSETS.joinpath("overview.js").read_text(), media_type="application/javascript", headers=SECURITY_HEADERS)

    @router.get("/statistics.js", operation_id="plugins.pt-site-pack.statistics-script", openapi_extra={"x-cli-hidden": True})
    async def statistics_script():
        return Response(ASSETS.joinpath("statistics.js").read_text(), media_type="application/javascript", headers=SECURITY_HEADERS)

    @router.get("/insights.js", operation_id="plugins.pt-site-pack.insights-script", openapi_extra={"x-cli-hidden": True})
    async def insights_script():
        return Response(ASSETS.joinpath("insights.js").read_text(), media_type="application/javascript", headers=SECURITY_HEADERS)

    @router.get('/maintenance.js', operation_id='plugins.pt-site-pack.maintenance-script', openapi_extra={'x-cli-hidden': True})
    async def maintenance_script():
        return Response(ASSETS.joinpath('maintenance.js').read_text(), media_type='application/javascript', headers=SECURITY_HEADERS)

    if workspace:
        from .maintenance import Maintenance, maintenance_router
        async def maintenance_snapshot():
            result = await snapshot()
            accounts = {a['site_id']: a for a in await (await ops()).call('site.list', {})}
            for row in result['sites']:
                row['_account'] = accounts.get(row['site_id'], {})
            return result
        workspace.maintenance = Maintenance(ctx, workspace, maintenance_snapshot)
        router.routes.extend(maintenance_router(workspace.maintenance).routes)

    @router.get("/icons/{site_id}", operation_id="plugins.pt-site-pack.icon", summary="读取本地缓存的站点图标", openapi_extra={"x-cli-hidden": True})
    async def icon(site_id: str):
        path = icon_asset(site_id)
        if path is None:
            raise HTTPException(404, "此站点尚无本地图标，页面使用文字头像")
        return Response(path.read_bytes(), media_type=ICON_TYPES[path.suffix],
                        headers={**SECURITY_HEADERS, "Cache-Control": "private, max-age=86400"})

    @router.get("/sites", operation_id="plugins.pt-site-pack.sites", summary="汇总官方与插件站点的接入和验证状态")
    async def sites():
        return await snapshot()

    @router.get("/catalog", operation_id="plugins.pt-site-pack.catalog")
    async def catalog():
        return await snapshot(include_catalog=True)

    @router.get("/diagnostics", operation_id="plugins.pt-site-pack.diagnostics", summary="读取插件站点功能诊断结果（不重新请求站点）")
    async def diagnostics():
        return {"reports": list(REPORTS.values()), "note": "仅保存本次插件加载期间的脱敏结果；不保存种子文件，不创建下载任务"}

    @router.post("/sites/{site_id}/probe", operation_id="plugins.pt-site-pack.probe",
                 summary="测试插件站点搜索、详情和获取一个种子文件，不提交下载任务")
    async def probe(site_id: str, request: Request, keyword: str = Query(..., min_length=1, max_length=100), include_torrent: bool = False):
        check_write_origin(request)
        if not VALID_ID.fullmatch(site_id):
            raise HTTPException(400, "站点 ID 不正确")
        keyword = keyword.strip()
        if not keyword or keyword.startswith("__pt_pack_probe__:"):
            raise HTTPException(400, "请输入实际片名关键词")
        if probe_lock.locked():
            raise HTTPException(409, "已有站点正在诊断，请逐站操作")
        async with probe_lock:
            if time.monotonic() - last_probe.get(site_id, float("-inf")) < 60:
                raise HTTPException(429, "请至少等待 60 秒再诊断此站点")
            state = await snapshot()
            row = next((r for r in state["sites"] if r["site_id"] == site_id), None)
            if not row or row["source"] not in {"plugin", "custom"} or not row["usable"]:
                raise HTTPException(409, "仅能诊断已启用且验证通过的插件站点；官方适配不执行此诊断")
            token, marker = issue_ticket(site_id, keyword, include_torrent=include_torrent)
            REPORTS.pop(site_id, None)
            last_probe[site_id] = time.monotonic()
            try:
                result = await (await ops()).call("search.torrents", {
                    "keyword": marker, "sites": [site_id], "no_history": True, "page": 1})
                report = REPORTS.get(site_id)
                if not report:
                    errors = [redacted_error(s.get("error")) for s in result.get("sites", []) if s.get("error")]
                    raise HTTPException(409, "插件诊断未执行；可能站点被停用或适配实例尚未更新" + ("：" + "; ".join(errors) if errors else ""))
                return report
            except OpsError as exc:
                raise HTTPException(exc.status, redacted_error(exc.message)) from None
            finally:
                TICKETS.pop(token, None)

    @router.post("/sites/{site_id}/retry", operation_id="plugins.pt-site-pack.retry", summary="手动重新验证已配置站点")
    async def retry(site_id: str, request: Request):
        check_write_origin(request)
        if not VALID_ID.fullmatch(site_id):
            raise HTTPException(400, "站点 ID 不正确")
        lock = retry_locks.setdefault(site_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "已经在发起验证，请稍后刷新")
        async with lock:
            now = time.monotonic()
            if now - last_retry.get(site_id, float("-inf")) < 30:
                raise HTTPException(429, "请至少等待 30 秒再重新验证")
            state = await snapshot()
            row = next((r for r in state["sites"] if r["site_id"] == site_id), None)
            if row is None:
                raise HTTPException(404, "目录中没有此站点")
            if not row["retry_allowed"]:
                raise HTTPException(409, row["retry_reason"])
            access = await ops()
            try:
                result = await access.call("site.verify", {"site_id": site_id})
            except OpsError as exc:
                raise HTTPException(exc.status, redacted_error(exc.message)) from None
            last_retry[site_id] = time.monotonic()
            return {"site_id": site_id, "verification": result.get("status", "verifying"),
                    "message": "已发起原生站点验证；不是已验证成功，页面会自动刷新结果"}

    if workspace:
        from .overview import overview_router
        router.routes.extend(overview_router(ctx, snapshot, workspace.evidence, workspace.profiles, workspace.statistics_record).routes)
        from .statistics import statistics_router
        router.routes.extend(statistics_router(ctx, workspace).routes)
    return router
