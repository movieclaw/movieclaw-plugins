"""供管理员查看的有限发送记录；与可靠投递队列分开，不暴露请求或账号凭据。"""

import json

from .policy import iso_time


def error_reason(status):
    return {
        0: "网络连接失败，将自动重试",
        400: "授权请求未被接受，请检查账号连接",
        401: "账号授权已失效，请重新连接",
        403: "Trakt 拒绝访问，请检查账号权限",
        404: "Trakt 未找到对应影片，请检查外部编号",
        422: "Trakt 无法接受此播放记录，请检查影片或季集编号",
        429: "Trakt 请求频率受限，稍后自动重试",
    }.get(status, "Trakt 服务暂时不可用，将自动重试")


def describe(event, kind):
    media, unit = event.get("media") or {}, event.get("unit") or {}
    title = " ".join(str(media.get("title") or "").split())
    title = title.encode("utf-8")[:240].decode("utf-8", errors="ignore")
    return {
        "title": title,
        "kind": media.get("kind") if media.get("kind") in ("movie", "tv") else "other",
        "season": unit.get("season"),
        "episode": unit.get("episode"),
        "event": kind,
    }


def fallback(job):
    item = job.get("item") or {}
    media = item.get("movie", item.get("show", {}))
    ids, unit = media.get("ids", {}), item.get("episode", {})
    identifier = " / ".join(f"{k.upper()} {v}" for k, v in ids.items())[:100]
    return {
        "title": identifier or "未命名影片",
        "kind": "tv" if "show" in item else "movie",
        "season": unit.get("season"),
        "episode": unit.get("number"),
        "event": "completed"
        if job.get("path") == "/sync/history"
        else ("started" if job.get("path") == "/scrobble/start" else "stopped"),
    }


class ActivityLog:
    async def record_activity(self, member, event_id, **values):
        # 展示记录写入失败不能打断可靠投递。上限与队列独立，保留最近 80 条。
        try:
            rows = await self.get(member, "activity", [])
            previous = next((r for r in rows if r["id"] == event_id), {})
            row = {**previous, "id": event_id, **values}
            rows = [r for r in rows if r["id"] != event_id] + [row]
            rows = rows[-80:]
            while len(json.dumps(rows, ensure_ascii=False).encode()) > 48000:
                rows.pop(0)
            await self.set(member, "activity", rows)
        except Exception:
            self.log.warning("Trakt 发送详情暂未保存，投递队列不受影响")

    async def accounts(self):
        scopes = await self.store.scopes("account", entity="member")
        members = {0}
        for scope in scopes:
            value = scope.split(":", 1)[-1]
            if value.isdigit():
                members.add(int(value))
        return {"accounts": [await self.status(member) for member in sorted(members)]}

    async def complete_activity(self, member, job, outcome):
        try:
            rows = await self.get(member, "activity", [])
            previous = next((row for row in rows if row["id"] == job["id"]), {})
            metadata = fallback(job)
            metadata.update({key: previous[key] for key in metadata if key in previous})
            await self.record_activity(
                member,
                job["id"],
                **metadata,
                occurred_at=job["occurred_at"],
                state="skipped" if outcome == "expired" else "sent",
                completed_at=iso_time(self.clock()),
                reason={
                    "expired": "实时播放事件已过期，不补报正在观看",
                    "ignored_short_play": "已清除正在观看；短播放未记入已看历史",
                    "already_synced": "已确认 Trakt 中存在记录，未重复添加",
                }.get(outcome),
            )
        except Exception:
            self.log.warning("Trakt 发送详情暂未保存，投递队列不受影响")

    async def activity(self, member):
        ledger = await self.ledger(member)
        history = await self.get(member, "activity", [])
        by_id = {row["id"]: row for row in history}
        live = []
        for state, jobs in (("queued", ledger["queue"]), ("failed", ledger.get("failed", []))):
            for job in jobs:
                code = job.get("error_status")
                visible_state = state
                if state == "queued" and code is not None:
                    visible_state = "blocked" if code in (400, 401, 403) else "retrying"
                live.append(
                    {
                        **fallback(job),
                        **by_id.get(job["id"], {}),
                        "id": job["id"],
                        "state": visible_state,
                        "occurred_at": job["occurred_at"],
                        "error_status": code,
                        "reason": error_reason(code)
                        if code is not None
                        else ("影片匹配失败，请检查外部编号后重试" if state == "failed" else None),
                        "retry_at": iso_time(job["next_at"]) if state == "queued" else None,
                        "failures": job.get("attempt", 0),
                        "can_retry": state == "failed",
                    }
                )
        live_ids = {row["id"] for row in live}
        finished = [
            row
            for row in history
            if row["id"] not in live_ids and row.get("state") in ("sent", "skipped")
        ]
        rows = sorted(live + finished, key=lambda row: row.get("occurred_at", ""), reverse=True)
        return {"member_id": member, "records": rows, "retained": 80}
