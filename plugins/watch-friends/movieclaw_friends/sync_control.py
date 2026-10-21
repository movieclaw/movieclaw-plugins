"""本人后台任务控制：暂停调度、停止本轮及按原授权重试，不扩大任何同步范围。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .watch_alignment import source_pin


class ControlInput(BaseModel):
    action: Literal["pause", "stop", "resume", "retry"]
    run_id: str | None = Field(default=None, min_length=1, max_length=64)


def register_sync_control(app, service, user_for, kind):
    """暂停会结束本轮，恢复时重新核验；已提交意图与成功结果均保留。"""
    assert kind in {"auto-sync", "resume-sync"}
    settings_table = "auto_sync_settings" if kind == "auto-sync" else "resume_settings"
    runs_table = "auto_sync_runs" if kind == "auto-sync" else "resume_runs"
    store = service.store
    locks = {}

    async def validate(owner, config):
        user = store.one("SELECT * FROM users WHERE id=?", (owner,))
        identity = await service.archive.source.identity(store.decrypt(user["token"]))
        if any(identity.get(k) != user.get(k) for k in ("username", "role", "member_id")):
            raise HTTPException(409, "原本人账号已变化，请重新授权")
        if not config.get("sources"):
            raise HTTPException(409, "请先保存本人同步范围")
        source_for = service.source_for if kind == "auto-sync" else service.auto.source_for
        for selected in config["sources"]:
            try:
                source = source_for(selected["source_id"], owner)
            except HTTPException:
                raise HTTPException(409, "原同步连接已失效，请重新保存授权范围") from None
            if source_pin(source) != selected["pin"]:
                raise HTTPException(409, "连接或本人授权已变化，请重新保存授权范围")

    @app.post(
        f"/api/watch/{kind}/control",
        name=kind.replace("-", "_") + "_control",
        operation_id=kind.replace("-", "_") + "_control",
    )
    async def sync_control(payload: ControlInput, request: Request):
        owner = user_for(request)["id"]
        async with locks.setdefault(owner, asyncio.Lock()):
            if payload.run_id and not store.one(
                f"SELECT id FROM {runs_table} WHERE id=? AND user_id=?", (payload.run_id, owner)
            ):
                raise HTTPException(404, "没有这次本人的任务")
            setting = store.one(f"SELECT * FROM {settings_table} WHERE user_id=?", (owner,))
            if not setting:
                raise HTTPException(409, "请先保存同步设置")
            config = json.loads(setting["config"])
            if payload.action in {"resume", "retry"}:
                if payload.action == "retry" and not setting["enabled"]:
                    raise HTTPException(409, "自动同步已暂停，请先恢复同步")
                await validate(owner, config)
                with store.lock:
                    changed = store.db.execute(
                        f"UPDATE {settings_table} SET enabled=1,next_at=?,error=NULL "
                        "WHERE user_id=? AND revision=?",
                        (datetime.now(UTC).isoformat(), owner, setting["revision"]),
                    ).rowcount
                if changed != 1:
                    raise HTTPException(409, "同步设置已变化，请刷新后重试")
                service.kick(owner)
            else:
                current = store.one(
                    f"SELECT id FROM {runs_table} WHERE user_id=? AND status='running' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (owner,),
                )
                if payload.run_id and (not current or current["id"] != payload.run_id):
                    raise HTTPException(409, "这轮已结束，未停止其他新任务，请刷新状态")
                if payload.action == "pause":
                    store.execute(
                        f"UPDATE {settings_table} SET enabled=0,revision=revision+1,next_at=NULL "
                        "WHERE user_id=?",
                        (owner,),
                    )
                else:
                    # 提前安排下次检查，避免取消回调与调度器竞争，立即重复发起扫描。
                    next_at = datetime.now(UTC) + timedelta(minutes=config["interval_minutes"])
                    store.execute(
                        f"UPDATE {settings_table} SET next_at=? WHERE user_id=?",
                        (next_at.isoformat() if setting["enabled"] else None, owner),
                    )
                task = service.running.get(owner)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if current:
                    status = "paused" if payload.action == "pause" else "stopped"
                    store.execute(
                        f"UPDATE {runs_table} SET status=?,error=? WHERE id=? AND user_id=?",
                        (
                            status,
                            "自动同步已暂停，原授权与已完成结果保留。恢复后重新读取核验。"
                            if status == "paused"
                            else "本轮已停止，已完成结果保留；下次按原计划检查。",
                            current["id"],
                            owner,
                        ),
                    )
            return service.view(owner)
