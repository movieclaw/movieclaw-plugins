"""仅取消本插件的受理/转存链，不撤销文件，也不取消共享扫描。"""
from fastapi import HTTPException

from movieclaw_sdk.ops import OpsError

import p115board as board

CANCELLABLE_TYPES = (board.REQUEST, board.TRANSFER, board.CLEANUP, "p115-import:cleanup-old", "p115-import:repair-names")


async def cancel_request(ops, store, job_id, lock):
    # 与受理→转存交接共用锁：不能出现按钮取消后又偷偷创建子任务。
    async with lock:
        try:
            state = await ops.call("jobs.show", {"job_id": job_id})
            if state.get("job_type") not in CANCELLABLE_TYPES:
                raise HTTPException(400, detail="这不是可取消的115入库任务")
            ref = board.ref_of(state)
            ids = [job_id]
            if state.get("job_type") == board.REQUEST and ref:
                saved = await store.get("request-state:" + ref, default={}) or {}
                child = saved.get("transfer_job_id") or (state.get("result") or {}).get("transfer_job_id")
                if child:
                    child_state = await ops.call("jobs.show", {"job_id": child})
                    if child_state.get("job_type") != board.TRANSFER or board.ref_of(child_state) != ref:
                        raise HTTPException(409, detail="关联任务不匹配，未取消任何任务")
                    ids.append(child)
            outcomes = []
            for target in ids:
                response = await ops.call("jobs.cancel", {"job_id": target})
                job = response.get("job") or {}
                outcomes.append({"job_id": target, "status": job.get("status"),
                                 "cancelled": bool(response.get("cancelled"))})
        except OpsError as exc:
            raise HTTPException(exc.status if exc.status in (400, 403, 404, 409) else 502,
                                detail=exc.message) from exc
    pending = any(row["status"] == "cancelling" for row in outcomes)
    changed = any(row["cancelled"] for row in outcomes)
    return {"status": "cancelling" if pending else "cancelled" if changed else "noop",
            "jobs": outcomes,
            "message": ("已请求取消，正在等待当前操作安全结束" if pending else
                        "任务已取消" if changed else "任务已经结束，无需取消")
                       + "；已转存文件和检查点保留，已提交的115云下载及共享扫描不会撤回"}
