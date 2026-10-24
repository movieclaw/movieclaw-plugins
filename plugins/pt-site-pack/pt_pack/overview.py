"""站点账号快照与只读下载器任务总览。输出白名单字段，不返回路径或下载凭据。"""
import asyncio
import copy
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from movieclaw_sdk.ops import HOST_OPS, OpsError
from .management import redacted_error, check_write_origin


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def number(value, *, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return int(value) if integer else value


def text(value, limit=300):
    value = re.sub(r"https?://\S+", "[链接已隐藏]", str(value or ""))
    return redacted_error(value).replace("\n", " ").replace("\r", " ")[:limit]


class HREvidence:
    """只记已认证适配器自然搜索时观测到的三态证据，不另行抓取 PT 页面。"""
    def __init__(self, data):
        self.data, self.values, self.lock = data, {}, asyncio.Lock()

    async def start(self):
        self.values = await self.data.get("overview-hr-v1") or {}

    async def observe(self, sid, items):
        changed = False
        async with self.lock:
            for item in items:
                tid, flag = str(item.torrent_id), item.hit_and_run
                source=getattr(item,"hr_source",None)
                if len(tid) > 80 or type(flag) is not bool:
                    continue
                if flag is False and source not in {"site-api","detail-label","list-marker"}:continue
                key = sid + ":" + tid
                self.values.pop(key, None)
                self.values[key] = {"value": flag, "observed_at": utcnow()}
                if source in {"site-api","detail-label","list-marker"}:self.values[key]["source"]=source
                changed = True
            while len(self.values) > 256 or len(json.dumps(self.values).encode()) > 60000:
                self.values.pop(next(iter(self.values)))
            if changed:
                try:
                    await self.data.set("overview-hr-v1", self.values, secret=True)
                except Exception:
                    pass  # 证据存储失败不得影响正常搜索。

    def lookup(self, sid, page_url, website):
        try: p, target = urlparse(str(page_url or "")), urlparse(str(website or ""))
        except ValueError: return None
        if not p.hostname or not target.hostname or p.hostname.lower().removeprefix("www.") != target.hostname.lower().removeprefix("www."):
            return None
        args = parse_qs(p.query)
        tid = (args.get("torrentid") or args.get("id") or [None])[0]
        if tid is None:
            match = re.search(r"/(?:details?|torrents?|t)/(\d+)(?:/|$)", p.path)
            tid = match.group(1) if match else None
        value = self.values.get(sid + ":" + str(tid))
        if value and value.get("value") is False and value.get("source") not in {"site-api","detail-label","list-marker"}:return None
        if value:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(value["observed_at"])
                if 0 <= age.total_seconds() <= 7 * 86400:
                    return value
            except (ValueError, KeyError, TypeError):
                pass
        return None


def profile_view(account, proof=None):
    p = account.get("profile") or {}
    up, down = number(p.get("uploaded_bytes")), number(p.get("downloaded_bytes"))
    # 宿主旧解析器缺字段时用 0 填充，双零且没有分享率不能证明账号确实没有流量。
    uncertain = not p or (not up and not down and number(p.get("ratio")) is None)
    seeds, leeches = number(p.get("seeding_count"), integer=True), number(p.get("leeching_count"), integer=True)
    if not seeds and not leeches:
        seeds = leeches = None
    stamp = p.get("fetched_at")
    stale = bool(p)
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        stale = (datetime.now(timezone.utc) - dt).total_seconds() > 86400
    except (ValueError, TypeError, AttributeError):
        pass
    result = {"username": text(p.get("username"), 80), "user_class": text(p.get("user_class"), 80),
            "uploaded_bytes": None if uncertain else up, "downloaded_bytes": None if uncertain else down,
            "ratio": number(p.get("ratio")), "bonus": number(p.get("bonus")),
            "seeding_count": seeds, "leeching_count": leeches, "fetched_at": stamp,
            "stale": stale, "incomplete": uncertain or seeds is None or leeches is None}
    if proof:
        for field in ("uploaded_bytes", "downloaded_bytes", "seeding_count", "leeching_count"):
            if p.get(field) == 0 and field not in proof.get("fields", {}): result[field] = None
        for field, value in proof.get("fields", {}).items():
            if field in {"uploaded_bytes", "downloaded_bytes", "ratio", "bonus", "seeding_count", "leeching_count"} and number(value) is not None:
                result[field] = value
    sources = dict(proof.get("sources", {})) if proof else {}
    if result["ratio"] is None and result["downloaded_bytes"] and result["uploaded_bytes"] is not None:
        result["ratio"] = result["uploaded_bytes"] / result["downloaded_bytes"]
        sources["ratio"] = "calculated"
    result["field_sources"] = sources
    result["missing_fields"] = [k for k in ("uploaded_bytes", "downloaded_bytes", "ratio", "bonus", "seeding_count", "leeching_count") if result[k] is None]
    result["incomplete"] = bool(result["missing_fields"])
    from .anniversary import age_view
    age = age_view(p.get("join_date"))
    if age["age_days"] is None: age = age_view((proof or {}).get("join_date"))
    result.update(age)
    result["age_observed_at"] = (proof or {}).get("join_date_observed_at") if result["age_days"] is not None else None
    return result


def task_view(raw, index, sites, evidence):
    from .hr import target_url
    sid = raw.get("site_id")
    sid = sid if sid in sites else None
    site = sites.get(sid) or {}
    flag = raw.get("hit_and_run")
    reference=target_url(site,raw.get("page_url")) if sid else None
    proof = evidence.lookup(sid, reference or raw.get("page_url"), site.get("website_url") or site.get("base_url")) if sid else None
    if type(flag) is not bool:
        flag = proof["value"] if proof else None
    state = raw.get("state") if raw.get("state") in {"downloading","stalled","queued","paused","checking","completed","error","missing","unknown"} else "unknown"
    def speed(key):
        return 0 if state == "paused" else None if state in {"missing", "unknown", "error"} else number(raw.get(key))
    return {"key": "task-" + str(index), "site_id": sid, "site_key": site.get("key"), "name": text(raw.get("name")) or "未命名任务",
            "downloader_name": text(raw.get("downloader_name"), 80), "state": state,
            "progress": min(1, number(raw.get("progress"))) if number(raw.get("progress")) is not None else None,
            "size_bytes": number(raw.get("size_bytes")), "upspeed_bytes": speed("upspeed_bytes"),
            "dlspeed_bytes": speed("dlspeed_bytes"), "uploaded_bytes": number(raw.get("uploaded_bytes")),
            "hit_and_run": flag, "hr_observed_at": proof["observed_at"] if proof else None,
            "can_check_hr": False, "hr_reason": "尚未采集到明确考核标记" if sid else "任务缺少站点来源，无法核对考核"}


def totals(tasks, available=True):
    def total(key):
        known = [t[key] for t in tasks if t[key] is not None]
        return sum(known) if known else 0 if not tasks and available else None
    return {"task_count": len(tasks) if available else None,
            "uploading": sum((t["upspeed_bytes"] or 0) > 0 for t in tasks) if available else None,
            "downloading": sum(t["state"] == "downloading" for t in tasks) if available else None,
            "hr_yes": sum(t["hit_and_run"] is True for t in tasks),
            "hr_unknown": sum(t["hit_and_run"] is None for t in tasks),
            "upspeed_bytes": total("upspeed_bytes") if available else None,
            "dlspeed_bytes": total("dlspeed_bytes") if available else None}


class Overview:
    def __init__(self, ctx, snapshot, evidence, profiles=None, statistics=None):
        self.ctx, self.snapshot, self.evidence = ctx, snapshot, evidence
        self.client = None
        self.cache, self.expiry, self.lock = None, 0, asyncio.Lock()
        self.site_keys = {}
        self.profiles = profiles
        self.statistics = statistics
        self.task_keys,self.next_task_key,self.targets={},0,{}

    async def read(self):
        if self.cache is not None and self.expiry > time.monotonic(): return self.cache
        async with self.lock:
            if self.cache is not None and self.expiry > time.monotonic(): return self.cache
            if self.client is None: self.client = await self.ctx.use(HOST_OPS).client(self.ctx)
            results = await asyncio.gather(self.snapshot(), self.client.call("site.list", {}), self.client.call("dl.tasks", {}), return_exceptions=True)
            if isinstance(results[0], Exception) or isinstance(results[1], Exception):
                if self.cache is not None:
                    old = copy.deepcopy(self.cache); old["stale"] = True; old["profile_snapshot_stale"] = True; old["warning"] = "读取失败，保留上次快照；数据并非实时"; return old
                raise HTTPException(503, "暂时无法读取站点统计，请稍后重试")
            snapshot, accounts, tasks = results
            account_map = {a["site_id"]: a for a in accounts}
            sites = {}
            for raw in snapshot["sites"]:
                sid = raw["site_id"]
                sites[sid] = {k: raw[k] for k in ("site_id","name","source","source_label","configured","usable","verification","verification_label","icon_url","website_url","base_url")}
                sites[sid]["key"] = self.site_keys.setdefault(sid, secrets.token_hex(8))
                account = account_map.get(sid, {})
                proof = self.profiles.lookup(sid, account.get("profile") or {}) if self.profiles else None
                sites[sid]["profile"] = profile_view(account, proof)
            available = not isinstance(tasks, Exception)
            sources = tasks.get("sources", []) if available else []
            # 按下载器 + infohash 去重，不把同一任务重复计入总流量。
            unique = {}
            for i, raw in enumerate(tasks.get("items", []) if available else []):
                key = (raw.get("downloader_id"), raw.get("info_hash")) if raw.get("info_hash") else ("id",raw["id"]) if raw.get("id") else ("row", i)
                unique.setdefault(key, raw)
            from .hr import target_url
            rows,targets=[],{}
            for key,raw in unique.items():
                if key not in self.task_keys:
                    self.task_keys[key]=self.next_task_key;self.next_task_key+=1
                row=task_view(raw,self.task_keys[key],sites,self.evidence)
                site=sites.get(row["site_id"])
                target=target_url(site,raw.get("page_url")) if site else None
                if target:
                    row["can_check_hr"]=bool(site["usable"])
                    row["hr_reason"]="可读取此种子的站点详情核对" if site["usable"] else "站点尚未通过登录验证"
                    targets[row["key"]]={"site_id":site["site_id"],"url":target}
                elif site:row["hr_reason"]="任务缺少有效的站点详情地址"
                rows.append(row)
            if available:
                self.targets=targets
                self.task_keys={key:self.task_keys[key] for key in unique}
            if not available and self.cache:
                rows = copy.deepcopy(self.cache["tasks"])
                for task in rows:
                    if task["site_id"] not in sites: task["site_id"] = None
            active = sum(s.get("status") == "active" for s in sources)
            live = available and bool(active)
            from .statistics import collection_report
            partial = not available or any(s.get("status") != "active" for s in sources)
            tasks_stamp = utcnow() if available else self.cache.get("tasks_updated_at") if self.cache else None
            for sid, site in sites.items():
                own_tasks = [t for t in rows if t["site_id"] == sid]
                site["live"] = totals(own_tasks, live)
                site["collection"] = collection_report(site["profile"], account_map.get(sid, {}), site["live"], own_tasks,
                    tasks_available=live, partial=partial, record=self.statistics(sid) if self.statistics else None, tasks_observed_at=tasks_stamp)
            summary = totals(rows, live)
            for field in ("uploaded_bytes", "downloaded_bytes"):
                values = [s["profile"][field] for s in sites.values() if s["profile"][field] is not None]
                summary[field] = sum(values) if values else None
            summary.update(site_count=len(sites), configured=sum(s["configured"] for s in sites.values()),
                           profile_coverage=sum(s["profile"]["uploaded_bytes"] is not None for s in sites.values()),
                           stale_sites=sum(s["profile"]["stale"] for s in sites.values()),
                           unknown_tasks=sum(t["site_id"] is None for t in rows),
                           downloader_count=len(sources), connected_downloaders=active)
            partial = not available or any(s.get("status") != "active" for s in sources)
            self.cache = {"sites": list(sites.values()), "tasks": rows, "summary": summary,
                          "unassigned": totals([t for t in rows if t["site_id"] is None], live),
                          "updated_at": utcnow(), "tasks_updated_at": tasks_stamp,
                          "stale": partial, "tasks_available": live,
                          "warning": "下载器数据不完整；当前仅显示已取得的任务" if partial else "尚未连接下载器，实时数据暂不可用" if not active else None}
            self.expiry = time.monotonic() + 15
            return self.cache

    def project(self, data, privacy, hide_sites, hide_names):
        result = copy.deepcopy(data)
        aliases = {s["site_id"]: "站点 " + str(i + 1).zfill(2) for i, s in enumerate(result["sites"])}
        for s in result["sites"]:
            s.pop("base_url", None); s.pop("website_url", None)
            if privacy:
                s["profile"].pop("username", None); s["profile"].pop("user_class", None)
                s["profile"].pop("join_date", None)
            if hide_sites:
                sid = s["site_id"]; s.update(site_id=aliases[sid], name=aliases[sid], icon_url=None)
        for t in result["tasks"]:
            if privacy: t.pop("downloader_name", None)
            if hide_sites and t["site_id"]: t["site_id"] = aliases[t["site_id"]]
            if hide_names: t["name"] = "任务 " + t["key"].split("-")[-1].zfill(3)
        result.update(privacy=privacy, hide_sites=hide_sites, hide_names=hide_names)
        return result


def overview_router(ctx, snapshot, evidence, profiles=None, statistics=None):
    service, router = Overview(ctx, snapshot, evidence, profiles, statistics), APIRouter()
    collected, collecting = {}, set()
    hr_checked,hr_running={},set()

    @router.post("/overview/hr-check",operation_id="plugins.pt-site-pack.hr-check",summary="核对一个已归属任务的站点 H&R 标记，不下载种子或新增任务")
    async def hr_check(request:Request,task_key:str=Query(...,min_length=1,max_length=80)):
        from .hr import issue,TICKETS,REPORTS
        check_write_origin(request)
        data=await service.read()
        if not data["tasks_available"] or data["stale"]:raise HTTPException(409,"下载器快照不可用，请刷新后再核对")
        row=next((r for r in data["tasks"] if r["key"]==task_key),None)
        target=service.targets.get(task_key)
        if not row or not target or not row["can_check_hr"]:raise HTTPException(409,"此任务缺少可验证的站点或详情地址")
        sid=target["site_id"]
        if sid in hr_running or time.monotonic()-hr_checked.get(sid,float("-inf"))<60:raise HTTPException(429,"同站核对至少间隔 60 秒，请稍后重试")
        hr_running.add(sid);token,marker=issue(sid,target["url"])
        try:
            await asyncio.wait_for(service.client.call("search.torrents",{"keyword":marker,"sites":[sid],"no_history":True,"page":1}),60)
            report=REPORTS.get(token)
            if report is None:raise HTTPException(502,"站点未完成详情核对，请检查登录与连接状态")
            hr_checked[sid]=time.monotonic();service.expiry=0
            return {"hit_and_run":report["value"],"source":report["source"],"checked_at":utcnow(),
                "message":"站点明确标记有 H&R 考核" if report["value"] is True else "站点明确标记无 H&R 考核" if report["value"] is False else "详情未提供可确认的 H&R 标记，保留原有证据；缺少证据时仍为未知，不代表没有考核"}
        except asyncio.TimeoutError:raise HTTPException(504,"站点核对超时，保留原有证据") from None
        except OpsError as exc:raise HTTPException(exc.status,redacted_error(exc.message)) from None
        finally:
            hr_running.discard(sid);TICKETS.pop(token,None);REPORTS.pop(token,None)

    @router.post("/overview/collect", operation_id="plugins.pt-site-pack.overview-collect", summary="通过原生验证补采单站账号资料，不修改账号配置")
    async def collect(request: Request, site_key: str = Query(..., min_length=1, max_length=80)):
        check_write_origin(request)
        raw = await service.read()
        site = next((s for s in raw["sites"] if s["key"] == site_key), None)
        if not site or not site["configured"]: raise HTTPException(404, "请先配置此站点账号")
        sid = site["site_id"]
        if sid in collecting or time.monotonic() - collected.get(sid, float("-inf")) < 60:
            raise HTTPException(429, "此站点已发起采集，请至少等待 60 秒")
        collecting.add(sid)
        try:
            await service.client.call("site.verify", {"site_id": sid})
            collected[sid] = time.monotonic()
            service.expiry = 0
        except OpsError as exc:
            raise HTTPException(exc.status, redacted_error(exc.message)) from None
        finally:
            collecting.discard(sid)
        return {"accepted": True, "message": "已发起账号验证与资料采集，结果会随快照更新；未识别字段继续显示为未知。"}

    @router.get("/overview", operation_id="plugins.pt-site-pack.overview", summary="读取本站统计与下载器实时汇总")
    async def overview(privacy: bool = True, hide_sites: bool = False, hide_names: bool = False):
        data = service.project(await service.read(), privacy, hide_sites, hide_names)
        data.pop("tasks")
        return data

    @router.get("/overview/tasks", operation_id="plugins.pt-site-pack.overview-tasks", summary="分页读取站点任务明细，不返回路径或下载凭据")
    async def tasks(site_id: str | None = None, state: str = "all", q: str = Query("", max_length=100),
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50),
                    privacy: bool = True, hide_sites: bool = False, hide_names: bool = False):
        if state not in {"all","uploading","downloading","completed","paused","error","hr","hr-unknown"}: raise HTTPException(400, "不支持的任务筛选")
        raw = await service.read()
        site_id = next((s["site_id"] for s in raw["sites"] if s["key"] == site_id), site_id)
        # 查询使用真实 ID，投影后不允许用隐藏的种子名进行搜索，避免名字侧漏。
        rows = [t for t in raw["tasks"] if site_id is None or (t["site_id"] is None if site_id == "_unassigned" else t["site_id"] == site_id)]
        allowed = {t["key"] for t in rows}
        data = service.project(raw, privacy, hide_sites, hide_names)
        rows = [t for t in data["tasks"] if t["key"] in allowed]
        def match(t):
            return (not q or q.lower() in t["name"].lower()) and (state == "all" or
                    state == "uploading" and (t["upspeed_bytes"] or 0) > 0 or
                    state == "completed" and t["progress"] == 1 or
                    state == "hr" and t["hit_and_run"] is True or
                    state == "hr-unknown" and t["hit_and_run"] is None or
                    state == "error" and t["state"] in {"error","missing","unknown"} or t["state"] == state)
        rows = sorted((t for t in rows if match(t)), key=lambda t: (-(t["dlspeed_bytes"] or 0), -(t["upspeed_bytes"] or 0), t["key"]))
        pages = max(1, math.ceil(len(rows) / page_size)); page = min(page, pages)
        return {"items": rows[(page-1)*page_size:page*page_size], "total": len(rows), "page": page,
                "pages": pages, "page_size": page_size, "updated_at": data["tasks_updated_at"], "stale": data["stale"],
                "warning": data["warning"], "privacy": privacy, "hide_names": hide_names,
                "note": "仅显示已连接下载器中的任务；站点做种总数可能包含其他设备。H&R 标记不代表考核已完成。"}

    return router
