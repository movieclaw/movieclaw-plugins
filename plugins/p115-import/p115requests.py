"""收件即持久化：网页/企微共用同一宿主任务；原链接只存插件加密数据。"""
import asyncio
import time

from fastapi import HTTPException
from movieclaw_sdk.jobs import JobBlocked, JobRetry, JobCancelled, ResourceRef
from movieclaw_sdk.ops import OpsError

import p115api as api
import p115flow as flow

JOB = "p115-import:request"


def safe_progress(state):
    return {k:state.get(k) for k in ("status","message","title","year","kind","tmdb_id","library_id",
        "library_name","media_item_id","candidates","recognition","moved","deleted","delete_failed",
        "inventory_verified","scan_job_id","pending_delete","scan_scope","metrics","not_ready","failed_downloads","subtitle_pending")
        if k in state}


async def submit(ctx, store, jobs, ops, body, *, user="", lock=None):
    async def enqueue():
        try:
            ref=flow.input_key(body.link,body.kind,body.title,body.year,body.tmdb_id)
        except ValueError as exc:
            raise HTTPException(400,detail=str(exc)) from exc
        cached=await store.get("request:"+ref,default=None)
        if cached and cached.get("job_id"):
            try:
                state=await ops.call("jobs.show",{"job_id":cached["job_id"]})
            except OpsError as exc:
                if exc.status!=404:
                    raise
                # 历史台账已过保留期时正常创建新一代，不被旧引用永久卡住。
                state={"status":"expired"}
            stored=await store.get("request-state:"+str(cached.get("request_ref")),default={}) or {}
            child_id=stored.get("transfer_job_id")
            if child_id:
                child_state=await ops.call("jobs.show",{"job_id":child_id})
                if child_state.get("status") in ("failed","blocked","cancelled"):
                    restarted=await ops.call("jobs.retry",{"job_id":child_id})
                    actual=restarted.get("job") or restarted
                    resumed_id=actual.get("id") or actual.get("job_id") or child_id
                    stored["transfer_job_id"]=resumed_id
                    await store.set("request-state:"+cached["request_ref"],stored,secret=True)
                    return {"status":"started","job_id":resumed_id,"request_job_id":cached["job_id"],
                            "job_created":False,"message":"已从原检查点续跑，只处理未完成的步骤"}
                if child_state.get("status") in ("queued","running","retry_wait","waiting","cancelling"):
                    return {"status":"started","job_id":child_id,"request_job_id":cached["job_id"],
                            "job_created":False,"message":"这批链接已在转存/核验，复用原任务，不重复提交"}
            if state.get("status") in ("failed","blocked","cancelled"):
                restarted=await ops.call("jobs.retry",{"job_id":cached["job_id"]})
                actual=restarted.get("job") or restarted
                resumed_id=actual.get("id") or actual.get("job_id") or cached["job_id"]
                await store.set("request:"+ref,{**cached,"job_id":resumed_id})
                return {"status":"started","job_id":resumed_id,"job_created":False,
                        "message":"已重新唤醒原请求，保留已完成的步骤"}
            if state.get("status") in ("queued","running","retry_wait","waiting","cancelling"):
                return {"status":"started","job_id":cached["job_id"],"job_created":False,
                        "message":"这批链接已在处理，复用同一任务，不重复转存"}
        # 覆盖配置引用前先换一代，历史任务重试不读到新请求/新凭据。
        generation=flow.digest([ref,time.time_ns()])
        await store.set("input:"+generation,{**body.model_dump(exclude={"yes"}),"user":user,"request_ref":generation},secret=True)
        job=await jobs.create(JOB,input={"request_ref":generation},subject="115 链接入库",
                              dedupe_key="input:"+ref,
                              resources=[ResourceRef("p115-input",ref,"target")])
        if not job.created:
            # 上次入队成功、索引写回前中断：以宿主已经存在的任务输入为准。
            existing=await ops.call("jobs.show",{"job_id":job.id})
            real_ref=(existing.get("input_data") or {}).get("request_ref")
            if real_ref:
                await store.delete("input:"+generation)
                generation=real_ref
        await store.set("request:"+ref,{"job_id":job.id,"request_ref":generation})
        return {"status":"started","job_id":job.id,"job_created":job.created,
                "message":"已收到链接并持久化；识别、转存、扫描与核验将在后台自动衔接"}
    if lock is None:
        return await enqueue()
    async with lock:
        return await enqueue()


async def notify_pause(ctx,store,ops,context,ref,message,push,*,retry=False):
    if not ref:
        return
    if retry:
        if not (await store.get("input:"+ref,default={}) or {}).get("user"):
            return
        state=await ops.call("jobs.show",{"job_id":context.job_id})
        if int(state.get("attempt") or 0)<int(state.get("max_attempts") or 3):
            return
    original=await store.get("input:"+ref,default={}) or {}
    saved=await store.get("request-state:"+ref,default={}) or {}
    if original.get("user") and saved.get("notified_error")!=message:
        await push(ctx,"【MovieClaw】处理暂停："+message+"；已完成步骤保留，可从原任务继续")
        saved["notified_error"]=message
        await store.set("request-state:"+ref,saved,secret=True)


class RequestRunner:
    def __init__(self,ctx,store,jobs,ops,*,plan_body,build_plan,push,execute=None,dispatch_lock=None):
        self.ctx,self.store,self.jobs,self.ops=ctx,store,jobs,ops
        self.plan_body,self.build_plan,self.push=plan_body,build_plan,push
        self.dispatch_lock=dispatch_lock or asyncio.Lock()

    async def handle(self,context,data):
        ref=data.get("request_ref")
        raw=await self.store.get("input:"+str(ref),default=None)
        if not raw:
            raise JobBlocked("请求资料不存在，未执行转存；请重新发链接")
        body=self.plan_body(**{k:v for k,v in raw.items() if k in self.plan_body.model_fields})
        saved=await self.store.get("request-state:"+ref,default={}) or {}
        started=time.monotonic()
        try:
            await context.raise_if_cancelled()
            if raw.get("user") and not saved.get("receipt_notified"):
                await self.push(self.ctx,"【MovieClaw】已收到链接，正在识别并检查库存；完成后会通知你，不用重复发送。")
                saved["receipt_notified"]=True
                await self.store.set("request-state:"+ref,saved,secret=True)
            if not saved.get("plan"):
                await context.update_progress(mode="waiting",phase="recognize",message="读取分享并交给 MovieClaw 识别、分库和检查库存")
                before=time.monotonic()
                plan=await self.build_plan(self.ctx,body)
                await context.raise_if_cancelled()
                plan["request_ref"]=ref
                plan["staging_dir_name"]=".p115-import/request-"+ref
                saved["plan_seconds"]=round(time.monotonic()-before,3)
                if plan.get("status") in ("ambiguous","not_found"):
                    result=safe_progress(plan)
                    result.update(request_ref=ref,job_id=context.job_id)
                    await self.store.set("request-result:"+ref,result)
                    if raw.get("user"):
                        choices="、".join(f"{c.get('title')}（{c.get('year') or '年份未知'}）" for c in plan.get("candidates",[])[:4])
                        await self.push(self.ctx,"【MovieClaw】需要确认作品：\n"+plan.get("message","无法可靠确定作品")
                                        +("\n候选："+choices if choices else ""))
                    raise JobBlocked(plan.get("message") or "需要确认作品",
                        details={"request_ref":ref,"candidates":plan.get("candidates") or []})
                if plan.get("status")!="ready" or self.ctx.config.dry_run:
                    result=safe_progress(plan)
                    result["status"]="dry_run" if self.ctx.config.dry_run else plan.get("status")
                    result.update(request_ref=ref,job_id=context.job_id)
                    await self.store.set("request-result:"+ref,result)
                    if raw.get("user"):
                        await self.push(self.ctx,"【MovieClaw】"+(plan.get("message") or "没有需要转存的文件"))
                    return result
                saved["plan"]=plan
                await self.store.set("request-state:"+ref,saved,secret=True)
            plan=saved["plan"]
            await context.update_progress(mode="waiting",phase="dispatch",message="已识别，排队执行网盘归位与本体库存核验")
            # 源请求不持有同作品锁；实际写操作在串行子任务执行，避免两个分享互相覆盖。
            async with self.dispatch_lock:
                await context.raise_if_cancelled()
                if not saved.get("transfer_job_id"):
                    private_key="transfer:"+ref
                    await self.store.set(private_key,plan,secret=True)
                    child=await self.jobs.create("p115-import:transfer",input={"plan_ref":private_key},
                        subject=plan.get("title") or "115 转存",
                        dedupe_key="transfer:"+ref,
                        resources=[ResourceRef("p115-writer",str(plan["cloud_account_id"]),"target"),
                                   ResourceRef("library",plan["library_id"],"context")])
                    saved["transfer_job_id"]=child.id
                    await self.store.set("request-state:"+ref,saved,secret=True)
                # 交接期间取消/停用也不能留下无人照管的转存子任务。
                try:
                    await context.raise_if_cancelled()
                except JobCancelled:
                    await self.ops.call("jobs.cancel", {"job_id": saved["transfer_job_id"]})
                    raise
            async with self.dispatch_lock:
                await context.raise_if_cancelled()
                state=await self.ops.call("jobs.show",{"job_id":saved["transfer_job_id"]})
                # 用户取消不属于可自动恢复的失败，即便以前发过暂停通知也不能复活。
                if state.get("status") in ("failed","blocked") and saved.get("notified_error"):
                    restarted=await self.ops.call("jobs.retry",{"job_id":saved["transfer_job_id"]})
                    actual=restarted.get("job") or restarted
                    saved["transfer_job_id"]=actual.get("id") or actual.get("job_id") or saved["transfer_job_id"]
                    saved.pop("notified_error",None)
                    await self.store.set("request-state:"+ref,saved,secret=True)
                    state=await self.ops.call("jobs.show",{"job_id":saved["transfer_job_id"]})
                if state.get("status") == "cancelled":
                    raise JobCancelled()
            if state.get("status")=="succeeded":
                result=state.get("result") or {}
                result.update(request_ref=ref,job_id=context.job_id,
                              metrics={**(result.get("metrics") or {}),"plan_seconds":saved.get("plan_seconds"),
                                       "request_attempt_seconds":round(time.monotonic()-started,3)})
                await self.store.set("request-result:"+ref,result)
                if raw.get("user") and not saved.get("notified"):
                    await self.push(self.ctx,"【MovieClaw】"+(result.get("message") or "入库完成")
                                    +(f"\n已核验，可在「{result.get('library_name') or '媒体库'}」播放" if result.get("inventory_verified") else ""))
                    saved["notified"]=True
                    await self.store.set("request-state:"+ref,saved,secret=True)
                return result
            if state.get("status") in ("failed","blocked","cancelled"):
                message=(state.get("error") or {}).get("message") or "转存任务未成功"
                if raw.get("user") and saved.get("notified_error")!=message:
                    await self.push(self.ctx,"【MovieClaw】处理暂停："+message)
                    saved["notified_error"]=message
                    await self.store.set("request-state:"+ref,saved,secret=True)
                raise JobBlocked(message,details={"transfer_job_id":saved["transfer_job_id"]})
            # 不创建监控子任务占满宿主4个worker：将请求任务结束为已交接，子任务完成时
            # 由其处理器统一通知/更新 request-result。任务页显示真正执行的子任务。
            result={"status":"dispatched","request_ref":ref,"job_id":context.job_id,
                    "transfer_job_id":saved["transfer_job_id"],"title":plan["title"],
                    "library_id":plan["library_id"],"message":"已完成识别并交给转存入库任务，结果以该任务核验为准"}
            await self.store.set("request-result:"+ref,result)
            return result
        except HTTPException as exc:
            await notify_pause(self.ctx,self.store,self.ops,context,ref,str(exc.detail),self.push,retry=exc.status_code>=500)
            if exc.status_code>=500:
                raise JobRetry(str(exc.detail),delay_seconds=30) from exc
            raise JobBlocked(str(exc.detail)) from exc
        except (api.AuthExpired,api.ShareGone,api.ShareLocked) as exc:
            await notify_pause(self.ctx,self.store,self.ops,context,ref,exc.message,self.push)
            raise JobBlocked(exc.message) from exc
        except api.RiskControl as exc:
            await notify_pause(self.ctx,self.store,self.ops,context,ref,exc.message,self.push)
            raise JobBlocked(exc.message) from exc
        except (api.ApiError,OpsError) as exc:
            message=exc.message if isinstance(exc,api.ApiError) else "宿主接口暂时不可用"
            await notify_pause(self.ctx,self.store,self.ops,context,ref,message,self.push,retry=True)
            raise JobRetry(message+"；未完成的步骤保留，将自动重试",delay_seconds=getattr(exc,"retry_after",None) or 30) from exc
