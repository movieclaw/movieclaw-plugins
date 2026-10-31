"""洗版旧文件清理：精确预览→短期确认票据→宿主任务，绝不由转存隐式触发。"""
import secrets
import time

from movieclaw_sdk.jobs import JobBlocked, ResourceRef

from p115flow import digest, matching_rows

JOB = "p115-import:cleanup-old"


def approval_ref(token):
    # 任务中心只能看到不可逆的数据引用，不能据此还原短期确认票据。
    return "delete-approval:"+digest(str(token or ""))


def flow_key(plan):
    return "flow:" + digest([plan.get("request_ref") or plan.get("staging_dir_name"),
                              plan.get("library_id"),plan.get("tmdb_id")])


def replacements(old, moved, rows):
    found, _ = matching_rows(moved, rows)
    return [m for m in moved if m["path"] in found
            and int(m.get("season") or 0)==int(old.get("season") or 0)
            and int(m.get("episode") or 0)==int(old.get("episode") or 0)
            and int(m.get("size") or 0)>int(old.get("size") or 0)
            and found[m["path"]].get("file_id")!=old.get("file_id")]


def same_old(old, current):
    return current and not current.get("missing") and all(
        current.get(k)==old.get(k) for k in ("file_id","remote_path","size","season","episode"))


async def evidence(ops, store, job_id, existing):
    job = await ops.call("jobs.show", {"job_id":job_id})
    if job.get("job_type")!="p115-import:transfer" or job.get("status")!="succeeded":
        raise JobBlocked("只能清理已成功核验入库的115转存任务所对应的旧文件")
    plan_ref=(job.get("input_data") or {}).get("plan_ref") or ""
    if not plan_ref.startswith("transfer:"):
        raise JobBlocked("原任务缺少可复核的持久化计划，未删除任何文件")
    plan=await store.get(plan_ref, default=None)
    if not plan:
        raise JobBlocked("原转存计划已不存在，未删除任何文件")
    state=await store.get(flow_key(plan), default={}) or {}
    result=state.get("result") or job.get("result") or {}
    if not result.get("inventory_verified"):
        raise JobBlocked("原任务没有逐文件入库核验结果，不能清理旧版")
    media_id, rows=await existing(ops,plan["library_id"],plan["tmdb_id"],plan["kind"])
    if not media_id or media_id!=result.get("media_item_id"):
        raise JobBlocked("作品的媒体库身份已变化，未删除任何文件")
    return plan,state,result,media_id,rows


async def preview(ops, store, job_id, existing):
    plan,state,result,media_id,rows=await evidence(ops,store,job_id,existing)
    moved=list((state.get("moved") or {}).values()) or result.get("moved") or []
    files=[]
    retained=[]
    seen=set()
    for old in plan.get("old_files") or []:
        if not old.get("file_id") or old["file_id"] in seen:
            continue
        seen.add(old["file_id"])
        current=next((r for r in rows if r.get("file_id")==old.get("file_id")),None)
        if current is None:
            continue
        new=replacements(old,moved,rows)
        if not current.get("cloud_only"):
            retained.append({"name":old.get("name"),"reason":"此文件含本地实体，插件不能完整预览其附属文件；请使用媒体库原生删除确认"})
            continue
        if not same_old(old,current) or not new:
            retained.append({"name":old.get("name"),"reason":"原文件已变化或同季集更大新文件不在位，保留"})
            continue
        args={"library_id":plan["library_id"],"media_item_id":media_id,"file_id":old["file_id"]}
        native=await ops.call("library.items.delete-file", {**args,"dry_run":True})
        if not native.get("dry_run") or native.get("errors") or native.get("rows_deleted")!=1:
            raise JobBlocked("宿主删除演练未得到完整路径清单，未授权清理")
        files.append({"old":old,"new":new,"removed_paths":[old["remote_path"],*(native.get("removed_paths") or [])],
                      "native_paths":native.get("removed_paths") or [],
                      "rows_deleted":native.get("rows_deleted"),"freed_bytes":native.get("freed_bytes")})
    if not files:
        return {"status":"nothing_to_do","title":plan["title"],"files":[],"retained":retained,
                "message":"没有通过安全复核的旧文件可清理；未删除任何内容"+("。"+retained[0]["reason"] if retained else "")}
    token=secrets.token_urlsafe(24)
    snapshot={"job_id":job_id,"plan":plan,"media_item_id":media_id,"files":files,
              "expires_at":int(time.time())+900}
    await store.set(approval_ref(token),snapshot,secret=True)
    return {"status":"preview","title":plan["title"],"library_name":plan.get("library_name"),
            "library_id":plan["library_id"],"media_item_id":media_id,"files":files,"retained":retained,
            "approval_token":token,"expires_at":snapshot["expires_at"],
            "message":"仅预览；须确认全部具体路径（含同名附属文件），再提交文件编号与确认票据。15分钟内有效"}


async def authorize(ops,store,jobs,job_id,file_ids,token,lock):
    async with lock:
        saved=await store.get(approval_ref(token),default=None)
        if not saved or saved.get("job_id")!=job_id:
            raise JobBlocked("缺少本次旧文件清单的确认票据，请先预览")
        expected=[f["old"]["file_id"] for f in saved["files"]]
        if not file_ids or len(file_ids)!=len(set(file_ids)) or sorted(file_ids)!=sorted(expected):
            raise JobBlocked("确认的文件清单与预览不一致，未提交删除")
        if saved.get("cleanup_job_id"):
            return {"status":"started","job_id":saved["cleanup_job_id"],"job_created":False,
                    "message":"这份确认已提交，复用同一清理任务"}
        if saved["expires_at"]<time.time():
            raise JobBlocked("确认票据已过期，请重新预览，不复用旧清单")
        ref=approval_ref(token)
        job=await jobs.create(JOB,input={"approval_ref":ref},subject=saved["plan"]["title"],
                              dedupe_key="cleanup-old:"+digest(token),
                              resources=[ResourceRef("p115-writer",str(saved["plan"]["cloud_account_id"]),"target"),
                                         ResourceRef("library",saved["plan"]["library_id"],"context")])
        saved["cleanup_job_id"]=job.id
        await store.set(ref,saved,secret=True)
        return {"status":"started","job_id":job.id,"job_created":job.created,
                "message":"已提交本次明确确认的旧文件清理；执行前仍会再次复核，新版本不动"}


async def execute(ops,store,data,context,existing,restore):
    key=data.get("approval_ref") or ""
    if not key.startswith("delete-approval:"):
        raise JobBlocked("清理任务缺少确认凭据，未删除")
    saved=await store.get(key,default=None)
    if not saved:
        raise JobBlocked("确认凭据已不存在，未删除")
    if saved.get("result"):
        return saved["result"]
    if saved["expires_at"]<time.time() and not saved.get("execution_started"):
        raise JobBlocked("任务排队时确认已过期，请重新预览清单")
    plan,state,result,media_id,rows=await evidence(ops,store,saved["job_id"],existing)
    # 不把随后变化的网盘库配置用于旧计划的删除。
    libraries=await ops.call("library.list",{"scope":"all"})
    library=next((l for l in libraries if l.get("id")==plan["library_id"]),None)
    folders=(library or {}).get("cloud_folders") or []
    if not any(int(f.get("account_id") or 0)==int(plan["cloud_account_id"])
               and str(f.get("remote_dir_id"))==str(plan["cloud_root_id"])
               and f.get("display_path")==plan["cloud_root_path"] for f in folders):
        raise JobBlocked("媒体库网盘位置已变化，未清理旧文件")
    await context.raise_if_cancelled()
    saved["execution_started"]=True
    saved.setdefault("deleted",[])
    await store.set(key,saved,secret=True)
    approved_new_paths=set()
    for item in saved["files"]:
        approved_new_paths.update(row["path"] for row in item["new"])
    for index,item in enumerate(saved["files"]):
        await context.raise_if_cancelled()
        old=item["old"]
        if old["file_id"] in saved["deleted"]:
            continue
        current_media,rows=await existing(ops,plan["library_id"],plan["tmdb_id"],plan["kind"])
        if current_media!=media_id:
            raise JobBlocked("清理前作品身份变化，未继续删除")
        current=next((r for r in rows if r.get("file_id")==old["file_id"]),None)
        if not replacements(old,item["new"],rows):
            raise JobBlocked("本次确认的新版本不在位或已变化，剩余旧文件保留")
        if current is not None:
            if not current.get("cloud_only") or not same_old(old,current):
                raise JobBlocked("删除前复核发现文件已变化或更大新版本缺失，剩余旧文件保留")
            args={"library_id":plan["library_id"],"media_item_id":media_id,"file_id":old["file_id"]}
            native=await ops.call("library.items.delete-file",{**args,"dry_run":True})
            if (not native.get("dry_run") or native.get("errors")
                or sorted(native.get("removed_paths") or [])!=sorted(item["native_paths"])
                or native.get("rows_deleted")!=item["rows_deleted"]):
                raise JobBlocked("删除范围与本次确认不同（可能含新增附属文件），未继续清理")
            await context.raise_if_cancelled()
            outcome=await ops.call("library.items.delete-file",args)
            if outcome.get("errors") or outcome.get("dry_run") or outcome.get("rows_deleted")!=1:
                raise JobBlocked("宿主未确认完整删除，剩余文件保留，请检查后重新预览")
            checked_media,checked_rows=await existing(ops,plan["library_id"],plan["tmdb_id"],plan["kind"])
            if checked_media!=media_id or any(r.get("file_id")==old["file_id"] for r in checked_rows):
                raise JobBlocked("删除后库存仍有旧文件或作品身份变化，未宣称清理完成")
        saved["deleted"].append(old["file_id"])
        state.setdefault("deleted",[])
        if old["file_id"] not in state["deleted"]:
            state["deleted"].append(old["file_id"])
        await store.set(key,saved,secret=True)
        await store.set(flow_key(plan),state,secret=True)
        await context.update_progress(mode="determinate",phase="cleanup",message="清理已明确确认且通过复核的旧文件",
                                      current=index+1,total=len(saved["files"]))
    # 清理票据只授权本次清单对应的视频规范名，不顺带修复其它历史冲突名。
    renamed, latest_state=await restore(plan,state,context,approved_new_paths)
    result.update(deleted=latest_state.get("deleted",[]),
                  pending_delete=[r for r in result.get("pending_delete",[]) if r.get("file_id") not in saved["deleted"]],
                  moved=list((latest_state.get("moved") or {}).values()),
                  name_restore_pending=latest_state.get("name_restore_pending",[]))
    result["message"]=f"《{plan['title']}》新版本已核验入库；已按单独确认清理 {len(saved['deleted'])} 个旧文件"
    latest_state["result"]=result
    await store.set(flow_key(plan),latest_state,secret=True)
    if plan.get("request_ref"):
        await store.set("request-result:"+plan["request_ref"],result)
    response={"status":"done","title":plan["title"],"library_id":plan["library_id"],"media_item_id":media_id,
              "deleted":saved["deleted"],"renamed":len(renamed),"message":result["message"]}
    saved["result"]=response
    await store.set(key,saved,secret=True)
    return response
