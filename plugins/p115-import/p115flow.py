"""链接到库存的可靠编排：宿主持久任务 + 加密检查点 + 原生扫描/台账验收。

不把“115 移动成功”当成“已入库”；任何旧文件删除必须晚于逐文件库存核验。
"""

import asyncio
import hashlib
import json
import os
import posixpath
import time
from collections import defaultdict

from movieclaw_sdk.jobs import JobBlocked, JobRetry
from movieclaw_sdk.ops import OpsError

import p115api as api
import p115batch as batch
from p115compat import cloud_scan_body, poll_interval


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:24]


def input_key(text, kind=None, title=None, year=None, tmdb_id=None):
    """等价分享域名、磁力参数顺序不影响去重；不同离线批次绝不共用空 key。"""
    offline = api.parse_offline(text)
    if offline:
        source = sorted((i.kind, (i.info_hash or i.url).lower()) for i in offline)
    else:
        code, password = api.parse_share_link(text)
        source = ["share", code, password]
    return digest([source, kind, (title or "").strip(), year, tmdb_id])


def terminal_job(payload):
    """jobs.wait 的 job 在嵌套字段内；不能读顶层 status。"""
    return (payload or {}).get("job") or payload or {}


def matching_rows(moved, rows):
    found, missing = {}, []
    for item in moved:
        matches = [row for row in rows if not row.get("missing")
                   and row.get("remote_path") == item["path"]
                   and int(row.get("size") or 0) == int(item["size"])
                   and int(row.get("season") or 0) == int(item.get("season") or 0)
                   and int(row.get("episode") or 0) == int(item.get("episode") or 0)]
        if matches:
            found[item["path"]] = matches[0]
        else:
            missing.append(item["path"])
    return found, missing


def receipt_empty(receipt):
    data = (receipt or {}).get("data")
    # 未提供计数字段 != 明确说零。115 有时只返回数字或别的回执结构。
    return (isinstance(data, dict) and "recv_file_count" in data
            and "recv_folder_count" in data and int(data["recv_file_count"] or 0) == 0
            and int(data["recv_folder_count"] or 0) == 0)


def choose_source(item, entries, taken):
    """哈希优先，其次原名+体积；不在多季同名 01.mkv 中盲取第一份。"""
    candidates = [e for e in entries if not e.is_dir and e.remote_id not in taken]
    sha = (item.get("sha1") or "").upper()
    size = int(item.get("size") or 0)
    if sha:
        exact = [e for e in candidates if (e.sha1 or "").upper() == sha
                 and (not size or int(e.size) == size)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            # 内容完全相同才可互换；不同内容不会走这一支。
            return exact[0]
    name = posixpath.basename(item.get("share_path") or "")
    exact = [e for e in candidates if api_name(e.name) == api_name(name)
             and (not size or int(e.size) == size)
             and (not sha or not e.sha1 or e.sha1.upper()==sha)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise JobBlocked(f"多个同名同体积文件「{name}」缺少可核验哈希，已停止，避免跨季错配")
    return None


def api_name(name):
    import re
    stem, ext = os.path.splitext(name or "")
    return re.sub(r"\s*\(\d+\)$", "", stem) + ext


def offline_entry(item, tasks, files, taken, staging=None):
    """云下载任务回执里的落地文件 ID 比体积更可靠。

    115 把 ed2k/磁力下完后会重新封装，网盘里的实际体积与链接里声明的不一样（常见差
    几十到几百 MB），所以按“原名+声明体积”永远对不上。云下载任务列表里给的
    ``delete_file_id`` 就是这次下载落盘的那个文件，用它认领不会被重封装误导。
    返回 ``(entry, task)``；任务还没结束、或回执里没有文件 ID 时返回 ``(None, task)``。
    """
    task = tasks.get(str(item.get("info_hash") or "").lower())
    if not task:
        return None, None
    # 另一个批次里的同一条资源不能归到本批：只认落在本批暂存目录的任务。
    if staging and str(task.get("wp_path_id") or task.get("file_id") or "") != str(staging):
        return None, task
    if int(task.get("status") or 0) != 2:      # 2 = 下载成功
        return None, task
    fid = str(task.get("delete_file_id") or "")
    if fid:
        return next((e for e in files if not e.is_dir and e.remote_id == fid
                     and e.remote_id not in taken), None), task
    # 老回执没有文件 ID 时只认唯一同名文件，绝不凭体积在多个同名里乱挑。
    name = posixpath.basename(item.get("share_path") or "")
    same = [e for e in files if not e.is_dir and e.remote_id not in taken
            and api_name(e.name) == api_name(name)]
    return (same[0] if len(same) == 1 else None), task


def offline_failed(task):
    """115 明确报告这条云下载失败（而不是还在下）。"""
    if not task:
        return False
    return int(task.get("status") or 0) == -1 or bool(task.get("err_code"))


def selected_items(items, kind):
    """同一批同一季集只投一份最大文件，未知体积无法安全择优时保留歧义。"""
    groups = defaultdict(list)
    for row in items:
        if row.get("action") == "replace":
            unit = (row.get("season"), row.get("episode")) if kind == "tv" else (0, 0)
            groups[unit].append(row)
    chosen = []
    discarded = []
    for group in groups.values():
        if len(group) > 1 and any(not row.get("size") for row in group):
            raise ValueError("同一季集有多个体积未知版本，不能自动择优，请分开提供")
        winner = max(group, key=lambda row: int(row.get("size") or 0))
        chosen.append(winner)
        discarded.extend(row for row in group if row is not winner)
    return chosen, discarded


async def cancellable_sleep(seconds, context=None):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if context:
            await context.raise_if_cancelled()
        await asyncio.sleep(min(1, max(.001, until-time.monotonic())))


async def wait_scan(ops, job_id, context=None, deadline=600):
    end = time.monotonic() + deadline
    unchanged = 0
    previous = None
    while time.monotonic() < end:
        if context:
            await context.raise_if_cancelled()
        # 短查询不占前台长轮询；没变化逐步减频，有进展立即恢复频率。
        state = await ops.call("jobs.show", {"job_id": job_id})
        status = state.get("status")
        if status == "succeeded":
            return state
        if status in ("failed", "cancelled", "cancelled_by_user", "blocked", "dismissed", "needs_attention"):
            message = (state.get("error") or {}).get("message") or "扫描未成功"
            raise JobRetry("媒体库扫描未完成：" + message, delay_seconds=30)
        signature = (status, state.get("revision"), json.dumps(state.get("progress") or {}, sort_keys=True))
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        await cancellable_sleep(min(poll_interval(unchanged), max(.001, end-time.monotonic())), context)
    raise JobRetry("媒体库扫描仍在后台，文件已保留，稍后自动继续核验", delay_seconds=30)


class Flow:
    def __init__(self, ctx, pan, ops, store, context, plan, *, existing, push=None,
                 wait_seconds=120, scan_seconds=600, cloud_max_seconds=6*3600):
        self.ctx, self.pan, self.ops, self.store, self.context = ctx, pan, ops, store, context
        self.plan, self.existing, self.push = plan, existing, push
        self.wait_seconds, self.scan_seconds = wait_seconds, scan_seconds
        #: 云下载在任务里最长等多久（小时级），超过才交给重试，避免把重试次数当计时器。
        self.cloud_max_seconds = cloud_max_seconds
        self.key = "flow:" + digest([plan.get("request_ref") or plan.get("staging_dir_name"),
                                    plan.get("library_id"), plan.get("tmdb_id")])
        self.state = {}
        self.started = time.monotonic()
        self.metrics = defaultdict(float)
        self.phase = None
        self.last_reported_phase=None
        self.phase_at = self.started
        self.requests_at = getattr(pan,"request_count",0)

    def finish_phase(self):
        now=time.monotonic()
        if self.phase:
            self.metrics[self.phase]+=now-self.phase_at
        self.phase_at=now

    async def save(self):
        self.state["timings"] = dict(self.metrics)
        await self.store.set(self.key, self.state, secret=True)

    async def step(self, phase, message, current=None, total=None):
        self.finish_phase()
        self.phase=phase
        if self.context:
            await self.context.raise_if_cancelled()
            due=getattr(self.context,"progress_due",lambda **kw:True)
            if phase==self.last_reported_phase and current!=total and not due(min_interval=1.0):
                return
            self.last_reported_phase=phase
            await self.context.update_progress(mode="determinate" if total else "waiting",
                phase=phase, message=message, current=current, total=total,
                details={"library_id":self.plan["library_id"],"media_item_id":self.state.get("media_item_id")})
        else:
            self.ctx.logger.info("%s", message)

    async def sleep(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.context:
                await self.context.raise_if_cancelled()
            await asyncio.sleep(min(1, max(.001,until-time.monotonic())))

    async def walk(self, cid):
        out = []
        seen = set()
        async def visit(current):
            if current in seen:
                return
            seen.add(current)
            if len(seen) > 1000:
                raise JobBlocked("暂存目录层级异常，已停止")
            for entry in await self.pan.list_dir(current):
                if entry.is_dir:
                    await visit(entry.remote_id)
                else:
                    # 分享转存可能保留多层目录，批量后回查必须知道真实父目录。
                    entry.parent_id=str(current)
                    out.append(entry)
        await visit(cid)
        return out

    async def read_inventory(self):
        media_id, rows = await self.existing(self.ops,self.plan["library_id"],
                                            self.plan["tmdb_id"],self.plan["kind"])
        if media_id:
            self.state["media_item_id"] = media_id
        return rows

    async def verify_inventory(self):
        moved = list(self.state.get("moved",{}).values())
        rows = await self.read_inventory()
        found, missing = matching_rows(moved, rows)
        if not missing:
            return found, rows
        body = cloud_scan_body(self.plan)
        await self.step("index", "文件已归位，只扫描本次作品的网盘目录并核验库存")
        scan_id = self.state.get("scan_job_id")
        if scan_id:
            try:
                prior = await self.ops.call("jobs.show", {"job_id":scan_id})
            except OpsError as exc:
                if exc.status != 404:
                    raise
                prior = {"status":"expired"}
            if prior.get("status") in ("failed","cancelled","blocked","dismissed","needs_attention","expired"):
                scan_id = None
                self.state.pop("scan_job_id",None)
                await self.save()
        # 409 是正常排队，不立即花掉一次故障重试额度；有界等待并随时响应取消。
        end = time.monotonic() + self.scan_seconds
        unchanged = 0
        while not scan_id:
            try:
                ref = await self.ops.call("library.scan.start", {"library_id":self.plan["library_id"],"body":body})
            except OpsError as exc:
                if exc.status == 409:
                    rows = await self.read_inventory()
                    found, missing = matching_rows(moved, rows)
                    if not missing:
                        return found, rows
                    if time.monotonic() >= end:
                        raise JobRetry("目标媒体库持续繁忙，文件已保留，稍后自动核对库存", delay_seconds=60) from exc
                    await self.step("index", "目标媒体库忙，等待空闲后只扫描本次作品目录（不重复转存）")
                    await self.sleep(min(poll_interval(unchanged), max(.001,end-time.monotonic())))
                    unchanged += 1
                    continue
                if exc.status in (400,422):
                    raise JobBlocked("网盘范围扫描被拒绝，未退回整库扫描；请核对作品目录及宿主版本") from exc
                raise
            scan_id = ref.get("job_id")
            if not scan_id:
                raise JobRetry("媒体库未返回扫描任务，文件已保留",delay_seconds=20)
            self.state["scan_job_id"] = scan_id
            self.state["scan_scope"] = body["cloud_paths"]
            await self.save()
        await wait_scan(self.ops,scan_id,self.context,deadline=self.scan_seconds)
        rows = await self.read_inventory()
        found, missing = matching_rows(moved, rows)
        if missing:
            self.state.pop("scan_job_id",None)
            await self.save()
            raise JobRetry(f"已归位，但还有 {len(missing)} 个文件未被媒体库核验，稍后自动重查",delay_seconds=30)
        return found, rows

    async def run(self):
        self.state = await self.store.get(self.key,default={}) or {}
        if self.state.get("result"):
            return self.state["result"]
        plan=self.plan
        # 目录范围必须在任何网盘写入前可构造，残缺旧计划不允许退回整库。
        cloud_scan_body(plan)
        want=[i for i in plan["items"] if i["action"]=="replace"]
        if not want:
            return {"status":"nothing_to_do","message":plan.get("message") or "没有需要导入的文件"}
        self.state.setdefault("items",{})
        self.state.setdefault("moved",{})
        self.state.setdefault("deleted",[])
        self.state.setdefault("subtitle_pending",[])
        # 真正获得写锁后重新读取库存，不能执行排队前已经过时的比体积结果。
        live_rows=await self.read_inventory()
        for item in want:
            ident=str(item.get("share_id") or item.get("info_hash") or digest(item))
            cp=self.state["items"].get(ident) or {}
            if cp.get("remote_id"):
                continue
            unit=(int(item.get("season") or 0),int(item.get("episode") or 0))
            live=[r for r in live_rows if not r.get("missing") and
                  (int(r.get("season") or 0),int(r.get("episode") or 0))==unit]
            item["existing"]=live
            if cp.get("skipped") or (item.get("size") and live and max(int(r["size"]) for r in live)>=int(item["size"])):
                self.state["items"].setdefault(ident,{})["skipped"]=True
        want=[item for item in want if not self.state["items"].get(str(item.get("share_id") or item.get("info_hash") or digest(item)),{}).get("skipped")]
        if not want:
            result={"status":"nothing_to_do","title":plan["title"],"library_id":plan["library_id"],
                    "media_item_id":self.state.get("media_item_id"),"moved":[],"deleted":[],
                    "message":"执行前复核：库存已有同样大或更大版本，本次不重复转存"}
            self.state["result"]=result
            await self.save()
            return result
        await self.step("prepare","恢复检查点并准备目标目录")
        # 每个作品/季目录只解析一次；恢复时 ID 优先，减少反复列库根目录。
        staging=self.state.get("staging_id")
        if not staging:
            staging=await self.pan.ensure_dir(plan["cloud_root_id"],plan["staging_dir_name"].split("/"))
            self.state["staging_id"]=staging
            await self.save()
        target_dirs=self.state.setdefault("target_dirs",{})
        for item in want:
            parts=[plan["entry_dir_name"]]+([item["season_dir"]] if item.get("season_dir") else [])
            rel="/".join(parts)
            if rel not in target_dirs:
                target_dirs[rel]=await self.pan.ensure_dir(plan["cloud_root_id"],parts)
                await self.save()
        # 先确认已搬过的文件，处理“移动成功、保存检查点前崩溃”的窗口。
        listings={}
        for rel,cid in target_dirs.items():
            listings[cid]=await self.pan.list_dir(cid)
        todo=[]
        for index,item in enumerate(want):
            ident=str(item.get("share_id") or item.get("info_hash") or digest(item))
            cp=self.state["items"].setdefault(ident,{})
            rel="/".join([plan["entry_dir_name"]]+([item["season_dir"]] if item.get("season_dir") else []))
            cid=target_dirs[rel]
            rows=listings[cid]
            arrived=next((e for e in rows if cp.get("remote_id") and e.remote_id==cp["remote_id"]),None)
            if cp.get("moved") and not arrived:
                raise JobBlocked("之前已归位的文件现在不在目标目录，为免重复导入已暂停，请检查网盘是否被外部工具移动")
            if arrived:
                if (cp.get("actual_size") and int(arrived.size)!=cp["actual_size"]) or arrived.name not in (cp.get("target_name"),cp.get("restore_to")):
                    raise JobBlocked("已归位文件的名字或体积发生变化，已暂停以免误判")
                if cp.get("sha1") and arrived.sha1 and cp["sha1"].upper()!=arrived.sha1.upper():
                    raise JobBlocked("已归位文件哈希变化，未继续处理")
                cp["moved"]=True
                self.record_moved(ident,item,cp,arrived.size)
                continue
            same=[e for e in rows if not e.is_dir and e.name==item["file_name"]]
            if same and not cp.get("target_name"):
                current=same[0]
                # 没有稳定身份/哈希时不能把同体积当成我们这次搬的文件。
                expected_path=posixpath.join(plan["cloud_root_path"],rel,current.name)
                if not any(row.get("remote_path")==expected_path and not row.get("missing") for row in item.get("existing") or []):
                    raise JobBlocked(f"目标已有同名文件「{item['file_name']}」，台账尚未确认，已暂停避免覆盖")
                # 洗版先保留旧文件，新版本带唯一后缀共存；验收以后才允许处理旧版。
                base,ext=os.path.splitext(item["file_name"])
                cp["target_name"]=f"{base} - p115-{digest(ident)[:8]}{ext}"
                # 旧文件删完以后要改回这个名字（临时后缀只是为了不当场撞名）。
                cp["restore_to"]=item["file_name"]
                cp["previous_name"]=cp["target_name"]
            cp.setdefault("target_name",item["file_name"])
            cp["target_id"]=cid
            todo.append((ident,item,cp))
        await self.save()
        offline=any(i.get("source") in ("ed2k","magnet","http") for _,i,_ in todo)
        if todo and not self.state.get("submitted") and not self.state.get("submit_intent"):
            await self.step("submit","提交到 115（重试只处理尚未完成的文件）")
            # 先保存提交意图。网络断开时不知道服务器是否已接受，恢复只查暂存，不能盲重发。
            self.state["submit_intent"]=True
            await self.save()
            try:
                if offline:
                    await self.submit_offline(todo,staging)
                else:
                    ids=[]
                    for _,item,_ in todo:
                        ids.append(item["share_id"])
                        ids.extend(item.get("sub_ids") or [])
                    receipt=await self.pan.share_receive(plan["share_code"],plan["receive_code"],list(dict.fromkeys(ids)),staging)
                    if receipt_empty(receipt):
                        raise JobBlocked("115 明确回执为零文件，未执行任何旧文件清理；请检查容量、分享有效期或风控")
            except (JobBlocked,api.AuthExpired,api.ShareGone,api.ShareLocked,api.Conflict,api.RateLimited,api.RiskControl):
                # 明确拒绝/前置检查未通过可以在用户修复后重新提交；不适用于网络响应丢失。
                self.state.pop("submit_intent",None)
                await self.save()
                raise
            self.state["submitted"]=True
            await self.save()
        if todo:
            files,tasks=[],{}
            await self.wait_cloud(todo,staging)
            # 重新读一次暂存与云下载回执，拿最终落地结果归位。
            files=await self.walk(staging)
            tasks=await self.offline_index() if offline else {}
            taken={cp["remote_id"] for cp in self.state["items"].values() if cp.get("remote_id")}
            waiting=[]
            failed=[]
            prepared=[]
            for index,(ident,item,cp) in enumerate(todo):
                await self.step("organize",f"核对批量归位计划 {index+1}/{len(todo)}",index+1,len(todo))
                entry=next((e for e in files if e.remote_id==cp.get("remote_id")),None)
                task=None
                if entry is None and not cp.get("remote_id"):
                    if offline:
                        entry,task=offline_entry(item,tasks,files,taken,staging)
                        if offline_failed(task):
                            failed.append(posixpath.basename(item["share_path"]))
                            continue
                    else:
                        entry=choose_source(item,files,taken)
                if entry is None:
                    waiting.append(posixpath.basename(item["share_path"]))
                    continue
                taken.add(entry.remote_id)
                if int(entry.size)<=0:
                    raise JobBlocked(f"「{entry.name}」仍为空文件，暂存已保留，未清理任何旧文件")
                if not offline and item.get("size") and int(entry.size)!=int(item["size"]):
                    raise JobBlocked(f"「{entry.name}」体积与来源不符，已保留文件并停止归位")
                if item.get("sha1") and entry.sha1 and item["sha1"].upper()!=entry.sha1.upper():
                    raise JobBlocked("暂存文件的内容哈希与来源不符，已保留文件并停止归位")
                if cp.get("actual_size") and int(entry.size)!=int(cp["actual_size"]):
                    raise JobBlocked("检查点文件的实际体积已变化，未继续归位")
                if cp.get("remote_id") and entry.name not in (cp.get("source_name"),cp["target_name"],
                        posixpath.basename(item["share_path"])):
                    raise JobBlocked("检查点文件被外部改名，未继续批量归位")
                if cp.get("source_id") and str(entry.parent_id)!=str(cp["source_id"]):
                    raise JobBlocked("检查点文件的暂存位置已变化，未继续归位")
                if item.get("existing") and int(entry.size)<=max(int(x.get("size") or 0) for x in item["existing"]):
                    cp["skipped"]=True
                    cp["skip_reason"]="云下载实际体积不优于库存"
                    await self.save()
                    continue
                cp.update(remote_id=entry.remote_id,actual_size=int(entry.size),sha1=entry.sha1 or item.get("sha1") or "")
                cp.setdefault("source_id",str(entry.parent_id))
                cp.setdefault("source_name",entry.name)
                prepared.append((ident,item,cp))
            # 在任何批量请求之前一次保存整批逐文件ID/来源/实际体积/新旧名。
            await self.save()
            if prepared:
                await self.step("organize",f"批量改名并按目标季目录归位 {len(prepared)} 个视频")
                rows=[self.operation_row(cp) for _,_,cp in prepared]
                await batch.renamed(self.pan,rows,context=self.context,save=self.save)
                by_id={str(cp["remote_id"]):(ident,item,cp) for ident,item,cp in prepared}
                async def record_confirmed(done):
                    for row in done:
                        ident,item,cp=by_id[str(row["remote_id"])]
                        cp["moved"]=True
                        self.record_moved(ident,item,cp,cp["actual_size"])
                await batch.moved(self.pan,rows,context=self.context,save=self.save,confirmed=record_confirmed)
            # 视频已逐ID确认到位后才处理字幕；缺失或失败字幕始终保留。
            await self.subtitles_batch(want,staging)
            if failed and not self.state["moved"]:
                raise JobBlocked(f"115 已明确报告 {len(failed)} 个云下载失败；暂存目录保留，请换失败的那几集资源")
            if waiting:
                # 已到的先入账，没到的由宿主自动重试，不要求用户重新发送。
                if self.state["moved"]:
                    await self.verify_inventory()
                detail=""
                if offline:
                    detail=f"（115 云下载仍在进行：{len(waiting)} 个文件）"
                raise JobRetry(f"还有 {len(waiting)} 个文件未完成{detail}，已到文件与检查点保留，稍后自动继续",delay_seconds=45)
            if failed:
                raise JobBlocked(f"115 已明确报告 {len(failed)} 个云下载失败；已归位文件保留，请换失败的那几集资源")
        if not self.state["moved"]:
            result={"status":"nothing_to_do","title":plan["title"],"message":"云下载实际体积不优于库存，保留现有版本；暂存文件保留待确认","moved":[],"deleted":[]}
            self.state["result"]=result
            await self.save()
            return result
        # 字幕与视频同样可恢复：只处理本批已确认的视频，不清掉失败内容。
        await self.subtitles_batch(want,staging)
        await self.step("verify","核对媒体库中的作品、季集、路径和文件体积")
        found,rows=await self.verify_inventory()
        # 转存授权不包含删除：先核验新文件，旧文件另列清单、独立确认后再处理。
        pending_delete=[]
        delete_failed=[]
        for old in plan.get("old_files") or []:
            old_id=old.get("file_id")
            if not old_id or old_id in self.state["deleted"]:
                continue
            old_now=next((r for r in rows if r.get("file_id")==old_id),None)
            if old_now is None:
                continue
            verified=[m for m in self.state["moved"].values()
                      if m["season"]==old.get("season") and m["episode"]==old.get("episode")
                      and m["path"] in found and int(m["size"])>int(old.get("size") or 0)
                      and found[m["path"]].get("file_id")!=old_id]
            if not verified:
                delete_failed.append({"name":old.get("name"),"error":"同季集更大新版本尚未核验，旧文件保留"})
                continue
            pending_delete.append({**old,"reason":"新版本已核验，等待单独确认删除旧文件"})
        renamed=await self.restore_names()
        # 只清理已经空掉的本批目录；有未识别/未搬的字幕时保留，绝不删整个非空目录。
        await self.step("cleanup","清理本批空目录，并核对空的暂存父目录")
        cleanup=await self.cleanup_staging(staging)
        self.finish_phase()
        result={"status":"done","title":plan["title"],"request_ref":plan.get("request_ref"),"library_id":plan["library_id"],
            "library_name":plan.get("library_name"),"media_item_id":self.state.get("media_item_id"),
            "moved":list(self.state["moved"].values()),"deleted":self.state["deleted"],
            "delete_failed":delete_failed,"pending_delete":pending_delete,"subtitle_pending":self.state["subtitle_pending"],
            "skipped":plan.get("skipped") or [],"not_ready":[],"failed_downloads":[],
            "scan_job_id":self.state.get("scan_job_id"),"inventory_verified":True,
            "staging_cleanup":cleanup,"scan_scope":self.state.get("scan_scope",[]),
            "metrics":{"seconds":round(time.monotonic()-self.started,3),
                       "phase_seconds":{key:round(value,3) for key,value in self.metrics.items()},
                       "connection_pool_requests_delta":getattr(self.pan,"request_count",0)-self.requests_at},
            "renamed":renamed,"name_restore_pending":self.state.get("name_restore_pending",[]),
            "message":f"《{plan['title']}》已由 MovieClaw 核验入库 {len(self.state['moved'])} 个文件"
                      +(f"；{len(self.state['subtitle_pending'])} 个字幕暂未归位，已保留" if self.state['subtitle_pending'] else "")
                      +(f"；{len(pending_delete)} 个旧文件保留，待单独确认清理" if pending_delete else "")
                      +(f"；{len(delete_failed)} 个旧文件未通过替换核验，保留" if delete_failed else "")
                      +("；部分规范名被占用，已保留临时名" if self.state.get("name_restore_pending") else "")
                      +("；暂存空目录清理未完成，已记录" if cleanup.get("status")=="deferred" else "")}
        self.state["result"]=result
        await self.save()
        return result

    @staticmethod
    def operation_row(cp):
        return {"remote_id":str(cp["remote_id"]),"source_id":str(cp["source_id"]),
                "target_id":str(cp["target_id"]),"from_name":cp["source_name"],
                "name":cp["target_name"],"size":int(cp["actual_size"]),"sha1":cp.get("sha1") or ""}

    async def restore_names(self,allowed_paths=None):
        """规范名批量还原，目录只读一次预检；批后按ID回读，部分成功逐项存盘。"""
        renames=[]
        pending=[]
        rows=[]
        checkpoints={}
        listings={}
        for ident,cp in (self.state.get("items") or {}).items():
            moved=(self.state.get("moved") or {}).get(ident) or {}
            if allowed_paths is not None and moved.get("path") not in allowed_paths:
                continue
            if not cp.get("moved") or not cp.get("remote_id") or not cp.get("restore_to"):
                continue
            if self.context:
                await self.context.raise_if_cancelled()
            cid=str(cp["target_id"])
            if cid not in listings:
                listings[cid]=await self.pan.list_dir(cid)
            canonical=cp["restore_to"]
            entries=listings[cid]
            entry=next((e for e in entries if e.remote_id==cp["remote_id"] and not e.is_dir),None)
            if entry is None or int(entry.size)!=int(cp.get("actual_size") or 0):
                raise JobBlocked("洗版新文件的位置或体积已变化，未改名；请核对网盘")
            if any(e.name.casefold()==canonical.casefold() and e.remote_id!=entry.remote_id for e in entries):
                pending.append({"name":canonical,"reason":"规范名仍被其他文件占用，保留临时名，不覆盖"})
                continue
            if entry.name not in (cp["target_name"],canonical):
                raise JobBlocked("洗版新文件被外部改名，未继续修改")
            row={"remote_id":entry.remote_id,"source_id":cid,"from_name":cp["target_name"],
                 "name":canonical,"size":entry.size}
            rows.append(row)
            checkpoints[entry.remote_id]=(ident,cp)
        async def record_restored(done):
            for row in done:
                ident,cp=checkpoints[row["remote_id"]]
                cp["target_name"]=row["name"]
                cp.pop("restore_to",None)
                moved=self.state["moved"].get(ident)
                if moved:
                    moved["name"]=row["name"]
                    moved["path"]=posixpath.join(posixpath.dirname(moved["path"]),row["name"])
                renames.append({"from":cp.get("previous_name") or "","to":row["name"]})
                self.state.pop("scan_job_id",None)
        await batch.renamed(self.pan,rows,context=self.context,save=self.save,confirmed=record_restored)
        self.state["name_restore_pending"]=pending
        await self.save()
        if renames:
            await self.verify_inventory()
        return renames

    def record_moved(self,ident,item,cp,size):
        self.state["moved"][ident]={"name":cp["target_name"],"season":item.get("season",0),
            "episode":item.get("episode",0),"size":int(size),
            "path":posixpath.join(self.plan["cloud_root_path"],self.plan["entry_dir_name"],
                                 item.get("season_dir") or "",cp["target_name"])}

    async def subtitles_batch(self,items,staging):
        """本批字幕按视频ID检查点归属，批量改名与分目录移动；歧义、冲突均保留。"""
        relevant=[]
        for item in items:
            ident=str(item.get("share_id") or item.get("info_hash") or digest(item))
            cp=(self.state.get("items") or {}).get(ident) or {}
            if cp.get("moved") and any(not (cp.get("subtitles",{}).get(sub) or {}).get("moved") for sub in item.get("subs") or []):
                relevant.append((item,cp))
        if not relevant:
            return
        files=await self.walk(staging)
        taken={str(cp["remote_id"]) for cp in self.state["items"].values() if cp.get("remote_id")}
        listings={}
        operations=[]
        checkpoints={}
        def pending(sub):
            if sub not in self.state["subtitle_pending"]:
                self.state["subtitle_pending"].append(sub)
        for item,cp in relevant:
            subs=cp.setdefault("subtitles",{})
            for sub in item.get("subs") or []:
                subcp=subs.setdefault(sub,{})
                if subcp.get("moved"):
                    continue
                pending(sub)
                cid=str(cp["target_id"])
                if cid not in listings:
                    listings[cid]=await self.pan.list_dir(cid)
                destination=listings[cid]
                old_name=posixpath.basename(sub)
                if subcp.get("remote_id"):
                    arrived=next((e for e in destination if e.remote_id==subcp["remote_id"]),None)
                    if arrived:
                        expected=subcp.get("target_name") or arrived.name
                        if arrived.is_dir or arrived.name!=expected or (subcp.get("actual_size") and arrived.size!=subcp["actual_size"]):
                            raise JobBlocked("字幕检查点文件被外部修改，未继续处理")
                        subcp["moved"]=True
                        self.state["subtitle_pending"].remove(sub)
                        continue
                # 使用视频的原暂存父目录；同名不唯一就保留歧义，不跨季盲取。
                video_source=str(cp.get("source_id") or "")
                candidates=[e for e in files if not e.is_dir and str(e.remote_id) not in taken
                    and (e.remote_id==subcp["remote_id"] if subcp.get("remote_id") else
                         e.name==old_name and (not video_source or str(e.parent_id)==video_source))]
                if len(candidates)!=1:
                    continue
                entry=candidates[0]
                old_stem=os.path.splitext(posixpath.basename(item["share_path"]))[0]
                suffix=old_name[len(old_stem):] if old_name.startswith(old_stem) else os.path.splitext(old_name)[1]
                new_name=os.path.splitext(cp["target_name"])[0]+suffix
                if any(e.name.casefold()==new_name.casefold() and e.remote_id!=entry.remote_id for e in destination):
                    continue
                if int(entry.size)<=0:
                    continue
                if subcp.get("source_id") and str(entry.parent_id)!=str(subcp["source_id"]):
                    raise JobBlocked("字幕检查点的暂存位置变化，未继续处理")
                if subcp.get("actual_size") and entry.size!=subcp["actual_size"]:
                    raise JobBlocked("字幕检查点体积变化，未继续改名")
                if subcp.get("source_name") and entry.name not in (subcp["source_name"],subcp.get("target_name")):
                    raise JobBlocked("字幕被外部改成计划外的名字，未继续改名")
                subcp.update(remote_id=entry.remote_id,target_id=cid,target_name=new_name,actual_size=entry.size)
                subcp.setdefault("source_id",str(entry.parent_id))
                subcp.setdefault("source_name",entry.name)
                taken.add(str(entry.remote_id))
                operations.append(self.operation_row(subcp))
                checkpoints[str(entry.remote_id)]=(sub,subcp)
        await self.save()
        async def record_subtitles(done):
            for row in done:
                sub,subcp=checkpoints[str(row["remote_id"])]
                subcp["moved"]=True
                if sub in self.state["subtitle_pending"]:
                    self.state["subtitle_pending"].remove(sub)
        if operations:
            try:
                await batch.renamed(self.pan,operations,context=self.context,save=self.save)
                await batch.moved(self.pan,operations,context=self.context,save=self.save,confirmed=record_subtitles)
            except (api.AuthExpired,api.RiskControl,api.RateLimited):
                raise
            except (api.ApiError,JobRetry):
                # 字幕故障不误删或伪装已归位，本轮结尾仍会尝试认回/补做。
                pass
        await self.save()

    async def wait_cloud(self,todo,staging):
        """等 115 把文件放到暂存目录。

        云下载（电dk/磁力）不是“几十秒就能完成”的事，百 GB 级别通常要几个小时；而宿主
        一个任务总共只能重试 3 次。旧实现把“还没下完”当成失败往外抛重试，十几分钟就把
        重试额度耗完，任务停在一个看着像“等文件”、其实再也不会醒来的状态。
        所以这里改成在任务里等：按命中的文件数报真实进度，并定期把心跳与进度写回，
        只有 115 明确报错或者等待到硬上限才交给重试/阻塞。
        """
        offline=any(i.get("source") in ("ed2k","magnet","http") for _,i,_ in todo)
        total=len([1 for _,_,cp in todo if not cp.get("remote_id")])
        deadline=time.monotonic()+max(self.wait_seconds,self.cloud_max_seconds if offline else self.wait_seconds)
        previous = None
        unchanged = 0
        while True:
            files=await self.walk(staging)
            tasks=await self.offline_index() if offline else {}
            ready=0
            failed=[]
            for _,item,cp in todo:
                if cp.get("remote_id"):
                    ready+=1
                    continue
                if offline:
                    entry,task=offline_entry(item,tasks,files,set(),staging)
                    if offline_failed(task):
                        failed.append(posixpath.basename(item["share_path"]))
                        continue
                    if entry is not None:
                        ready+=1
                elif choose_source(item,files,set()) is not None:
                    ready+=1
            if failed and ready==0 and len(failed)==total:
                raise JobBlocked(f"115 已明确报告 {len(failed)} 个云下载全部失败；暂存目录保留，请换资源")
            await self.step("cloud",f"等待 115 完成云下载 {ready}/{total} 个文件",ready,total)
            if ready>=total or failed or time.monotonic()>=deadline:
                return
            signature = (ready, tuple(failed))
            unchanged = unchanged + 1 if signature == previous else 0
            previous = signature
            await self.sleep(min(poll_interval(unchanged, cloud=True), max(.001,deadline-time.monotonic())))

    async def submit_offline(self,todo,staging):
        tasks=await self.offline_index()
        urls=[]
        for _,item,_ in todo:
            sha=(item.get("info_hash") or "").lower()
            existing=tasks.get(sha)
            if existing:
                if offline_failed(existing):
                    raise JobBlocked("115 已明确报告该云下载失败；不是本地处理超时，请换资源")
                folder=str(existing.get("wp_path_id") or existing.get("file_id") or "")
                if folder!=str(staging):
                    raise JobBlocked("相同云下载任务已在另一目录，未重复提交；需确认原任务落点后再继续")
            else:
                urls.append(item["share_id"])
        if urls:
            await self.pan.offline_add(urls,staging)

    async def offline_index(self):
        """把全部云下载任务按 info_hash 建索引（小写）。分页取全，不只第一页。"""
        tasks={}
        for page in range(1,21):
            payload=await self.pan.offline_tasks(page)
            rows=payload.get("tasks") or []
            for task in rows:
                sha=str(task.get("info_hash") or "").lower()
                if sha:
                    tasks[sha]=task
            pages=int(payload.get("page_count") or payload.get("page_num") or 1)
            if page>=pages or not rows:
                break
        return tasks

    async def cleanup_staging(self,staging):
        """仅处理本批目录；父目录必须重新确认完全为空，绝不清理其它批次。"""
        parts=[p for p in (self.plan.get("staging_dir_name") or "").split("/") if p]
        root=str(self.plan.get("cloud_root_id") or "")
        if len(parts)!=2 or parts[0]!=".p115-import" or any(p in (".","..") for p in parts) or str(staging)==root:
            return {"status":"deferred","reason":"暂存路径不符合安全边界，未执行清理"}
        try:
            batch_removed=await self.clean_empty(staging)
            if not batch_removed:
                return {"status":"retained","batch_removed":False,"holder_removed":False,
                        "reason":"本批仍有未处理内容，保留暂存目录"}
            return await cleanup_empty_holder(self.pan,root,context=self.context)
        except api.ApiError as exc:
            self.ctx.logger.warning("暂存空目录清理未完成：%s",exc.message)
            return {"status":"deferred","reason":"网盘未完成清理，文件未受影响"}

    async def clean_empty(self,cid):
        if self.context:
            await self.context.raise_if_cancelled()
        children=await self.pan.list_dir(cid)
        for entry in children:
            if entry.is_dir:
                await self.clean_empty(entry.remote_id)
        # 用完整子项（包括字幕/隐藏文件）判空，不能用“子目录清单”当空目录。
        if await self.pan.list_dir(cid):
            return False
        if self.context:
            await self.context.raise_if_cancelled()
        await self.pan.delete([cid])
        return True


async def cleanup_empty_holder(pan,root,*,context=None,expected_id=None,dry_run=False):
    """仅删除库根直下且完整清单为空的 .p115-import；不递归触碰其它批次。"""
    root=str(root)
    entries=await pan.list_dir(root)
    matches=[entry for entry in entries if entry.is_dir and entry.name==".p115-import"]
    if not matches:
        return {"status":"absent","batch_removed":True,"holder_removed":False}
    if len(matches)!=1:
        return {"status":"deferred","reason":"存在多个同名暂存父目录，未执行删除"}
    holder=matches[0]
    if holder.remote_id==root or (expected_id and holder.remote_id!=str(expected_id)):
        return {"status":"deferred","reason":"目录身份与确认时不同，未执行删除"}
    children=await pan.list_dir(holder.remote_id)
    result={"holder_id":holder.remote_id,"batch_removed":True,"holder_removed":False,
            "entry_count":len(children),"file_count":sum(not e.is_dir for e in children),
            "directory_count":sum(e.is_dir for e in children)}
    if children:
        return dict(result,status="retained",reason="父目录还有其它批次或文件，已保留")
    if dry_run:
        return dict(result,status="preview",eligible=True)
    if context:
        await context.raise_if_cancelled()
    # 删除前重新列全部子项；即使另一个批次只有空目录也必须保留。
    if await pan.list_dir(holder.remote_id):
        return dict(result,status="retained",reason="删除前发现新内容，父目录保留")
    await pan.delete([holder.remote_id])
    remaining=await pan.list_dir(root)
    if any(e.remote_id==holder.remote_id for e in remaining):
        return dict(result,status="deferred",reason="网盘回执成功但目录仍可见，未宣称清理完成")
    return dict(result,status="removed",holder_removed=True)
