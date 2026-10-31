"""看板数据：把宿主任务台账折成 收件箱 / 正在处理 / 最近完成 / 需要处理。

宿主 ``jobs.list`` 的每条任务已带 progress / result / error；这里只做归类与合并
（纯函数，方便测试）：同一批链接的「受理」与「转存」两条任务按 request_ref 合成
一条时间线；原文链接与提取码不进入任何返回值。
"""
from __future__ import annotations

from datetime import datetime

REQUEST = "p115-import:request"
TRANSFER = "p115-import:transfer"
CLEANUP = "p115-import:cleanup-staging"
CLEANUP_OLD = "p115-import:cleanup-old"

ACTIVE = ("queued", "running", "retry_wait", "cancelling", "waiting")
PROBLEM = ("failed", "blocked", "cancelled")

#: 阶段标识 → 展示文字；认不出的原样返回
PHASES = {
    "recognize": "读取分享、识别与库存检查",
    "dispatch": "已交接，排队转存",
    "queued": "排队中",
    "prepare": "准备暂存目录",
    "submit": "提交网盘转存",
    "cloud": "等待网盘放行文件",
    "organize": "按命名模板归位",
    "verify": "库存核验",
    "index": "媒体库登记",
    "cleanup": "清理暂存目录",
    "completed": "已完成",
    "failed": "未完成",
    "blocked": "需要处理",
}


def ref_of(job):
    """一条任务对应的请求编号：受理任务看输入，转存任务从 plan_ref 里取。"""
    result = job.get("result") or {}
    if job.get("job_type") == TRANSFER:
        plan_ref = str((job.get("input_data") or {}).get("plan_ref") or "")
        if plan_ref.startswith("transfer:"):
            return plan_ref.split(":", 1)[1]
    return str((job.get("input_data") or {}).get("request_ref")
               or result.get("request_ref") or "")


def _candidates(error):
    """候选只在能直接用来确认时带出：没有编号或类型认不出的都丢掉。"""
    rows = []
    for item in ((error or {}).get("details") or {}).get("candidates") or []:
        if not isinstance(item, dict):
            continue
        try:
            tmdb_id = int(item.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        kind = item.get("kind")
        if tmdb_id <= 0 or kind not in ("movie", "tv"):
            continue
        if not str(item.get("title") or "").strip():
            continue
        rows.append({"tmdb_id": tmdb_id, "kind": kind,
                     "title": str(item["title"]).strip(), "year": item.get("year")})
    return rows[:6]


def row_of(job):
    """一条任务 → 看板行。候选只在出错的任务上带出，不含任何链接原文。"""
    job_type = job.get("job_type") or ""
    result = job.get("result") or {}
    progress = job.get("progress") or {}
    error = job.get("error") or {}
    title = str(result.get("title") or "").strip()
    subject = str(job.get("subject") or "").strip()
    if not title and subject and subject not in ("115 链接入库", ".p115-import"):
        title = subject
    if not title and job_type == CLEANUP_OLD:
        title = "清理已确认的旧文件"
    if not title and job_type == CLEANUP:
        title = "空暂存目录清理"
    if not title and job_type == TRANSFER:
        title = "115 转存"
    if not title and job_type == REQUEST:
        title = "115 链接入库"
    phase = str(progress.get("phase") or "")
    status = job.get("status")
    return {
        "job_id": job.get("id"),
        "job_type": job_type,
        "ref": ref_of(job),
        "title": title,
        "status": status,
        "source": str(result.get("source") or ""),
        "phase": phase,
        "phase_label": PHASES.get(phase, phase),
        "message": str(progress.get("message") or result.get("message")
                       or error.get("message") or ""),
        "percent": progress.get("percent"),
        "current": progress.get("current"),
        "total": progress.get("total"),
        "library_name": str(result.get("library_name") or ""),
        "media_item_id": result.get("media_item_id"),
        "library_id": result.get("library_id"),
        "inventory_verified": bool(result.get("inventory_verified")),
        "pending_delete_count":len(result.get("pending_delete") or []),
        "scan_scope":result.get("scan_scope") or [],
        "transfer_job_id": str(result.get("transfer_job_id") or ""),
        "child": str(result.get("transfer_job_id") or ""),
        "attempt": job.get("attempt"),
        "max_attempts": job.get("max_attempts"),
        "dismissed": bool(job.get("dismissed_at")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
        "error": str(error.get("message") or "") if status in PROBLEM else "",
        "candidates": _candidates(error) if status in PROBLEM else [],
    }


def _stamp(row):
    for key in ("finished_at", "updated_at", "created_at"):
        value = row.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except ValueError:
                continue
    return 0.0


def _latest(rows):
    """同一批链接多次尝试（重试会新建任务）时只留最新的一份。"""
    newest: dict[str, dict] = {}
    for row in rows:
        key = row["ref"] or row["job_id"]
        current = newest.get(key)
        if current is None or _stamp(row) >= _stamp(current):
            newest[key] = row
    return [row for row in newest.values()]


def _attention(rows, active_refs):
    groups: dict[str, list[dict]] = {}
    for row in rows:
        # 只催「失败」与「需要处理」；取消是用户自己的选择，不回头打扰
        if row["status"] not in ("failed", "blocked") or row["dismissed"]:
            continue
        if row["ref"] and row["ref"] in active_refs:
            continue  # 同一批已有新任务在跑：旧失败不再催
        groups.setdefault(row["ref"] or row["job_id"], []).append(row)
    out = []
    for group in groups.values():
        group.sort(key=_stamp, reverse=True)
        head = dict(group[0])
        if not head["candidates"]:
            for row in group[1:]:
                if row["candidates"]:
                    head["candidates"] = row["candidates"]
                    break
        out.append(head)
    out.sort(key=_stamp, reverse=True)
    return out


def build_feed(jobs, *, recent_limit=20, attention_limit=20):
    rows = [row_of(job) for job in jobs if isinstance(job, dict) and job.get("id")]
    request_rows = [row for row in rows if row["job_type"] == REQUEST]
    transfer_rows = [row for row in rows if row["job_type"] == TRANSFER]
    cleanup_rows = [row for row in rows if row["job_type"] in (CLEANUP,CLEANUP_OLD)]
    active_refs = {row["ref"] for row in rows if row["status"] in ACTIVE and row["ref"]}

    transfers = _latest(transfer_rows)
    with_child = {row["ref"] for row in transfer_rows}

    inbox = [row for row in request_rows if row["status"] in ACTIVE]
    inbox.sort(key=_stamp, reverse=True)
    live = [row for row in transfers + cleanup_rows if row["status"] in ACTIVE]
    live.sort(key=_stamp, reverse=True)

    recent, seen = [], set()
    pool = transfers + cleanup_rows + [row for row in request_rows if row["ref"] not in with_child]
    for row in sorted(pool, key=_stamp, reverse=True):
        if row["status"] not in ("succeeded", "cancelled"):
            continue
        key = row["ref"] or row["job_id"]
        if key in seen:
            continue
        seen.add(key)
        recent.append(row)

    cancelled_refs = {row["ref"] for row in _latest(rows) if row["status"] == "cancelled" and row["ref"]}
    attention = _attention(rows, active_refs | cancelled_refs)
    return {
        "inbox": inbox[:20],
        "live": live[:20],
        "recent": recent[:recent_limit],
        "attention": attention[:attention_limit],
        "counts": {"inbox": len(inbox), "live": len(live),
                   "recent": len(recent), "attention": len(attention)},
    }
