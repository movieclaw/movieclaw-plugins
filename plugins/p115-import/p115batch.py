"""批量网络操作、逐文件验收。回执不等于成功；只补做仍未确认的文件。

批次50是本插件的保守分块，不声称是115接口的服务端上限。所有批次顺序执行，
保持现有账号限速。状态保存和归位记账由调用方传入，不触碰宿主数据库。
"""
from collections import defaultdict

from movieclaw_sdk.jobs import JobBlocked, JobRetry

import p115api as api

BATCH_SIZE = 50


def chunks(rows, size=BATCH_SIZE):
    for offset in range(0, len(rows), size):
        yield rows[offset:offset + size]


def validate_rows(rows):
    ids = set()
    targets = set()
    for row in rows:
        fid = str(row.get("remote_id") or "")
        source = str(row.get("source_id") or "")
        name = str(row.get("name") or "")
        if not fid or not source or fid in ids or not name.strip() or name in (".", "..") or "/" in name or "\\" in name:
            raise JobBlocked("批次文件编号、来源或目标名字异常，未执行批量操作")
        ids.add(fid)
        # 改名时先在来源目录里发生，同一目录的目标名必须唯一。
        key = (source, name.casefold())
        if key in targets:
            raise JobBlocked("同一来源目录存在重复的目标名字，未批量改名，避免覆盖")
        targets.add(key)
        if int(row.get("size") or 0) <= 0:
            raise JobBlocked("批次含空文件或缺少实际体积，未执行批量操作")


def checked_entry(row, entries, names):
    matches = [entry for entry in entries if str(entry.remote_id) == str(row["remote_id"])]
    if len(matches) != 1:
        raise JobRetry("批次文件尚未在预期目录按编号确认，检查点已保留，稍后复核", delay_seconds=30)
    entry = matches[0]
    if entry.is_dir or int(entry.size) != int(row["size"]):
        raise JobBlocked("批次文件类型或体积发生变化，未继续修改")
    if row.get("sha1") and entry.sha1 and row["sha1"].upper() != entry.sha1.upper():
        raise JobBlocked("批次文件哈希发生变化，未继续修改")
    if entry.name not in names:
        raise JobBlocked("批次文件被改成了计划外的名字，未继续修改")
    return entry


def ensure_free(row, entries):
    if any(entry.name.casefold() == row["name"].casefold() and str(entry.remote_id) != str(row["remote_id"])
           for entry in entries):
        raise JobBlocked("批次目标名字已被其他文件或目录占用，未覆盖")


async def cancelled(context):
    if context:
        await context.raise_if_cancelled()


async def directory(pan, cid, context):
    await cancelled(context)
    return await pan.list_dir(cid)


async def renamed(pan, rows, *, context=None, save=None, confirmed=None):
    """网页批量异名改名，按来源目录复核。不猜测或重发结果不明的请求。

    发生限流/登录/风控时不额外请求；其它网络回执丢失可先读目录认回实际成功项。
    成功项回调/检查点先写，再抛出未完成错误。已符合目标名的项不重复改名。
    """
    rows = list(rows)
    validate_rows(rows)
    listings = {}
    todo = []
    already = []
    for row in rows:
        source = str(row["source_id"])
        if source not in listings:
            listings[source] = await directory(pan, source, context)
        entry = checked_entry(row, listings[source], (row["from_name"], row["name"]))
        ensure_free(row, listings[source])
        (already if entry.name == row["name"] else todo).append(row)
    if confirmed and already:
        await confirmed(already)
    if save and already:
        await save()
    verified = list(already)
    for index,batch in enumerate(chunks(todo)):
        await cancelled(context)
        if index:
            refreshed={}
            for row in batch:
                source=str(row["source_id"])
                if source not in refreshed:
                    refreshed[source]=await directory(pan,source,context)
                checked_entry(row,refreshed[source],(row["from_name"],row["name"]))
                ensure_free(row,refreshed[source])
            listings.update(refreshed)
        # 全批ID/实际体积/原名必须先存盘，写入中断才可逐ID认回。
        if save:
            await save()
        error = None
        try:
            await pan.rename_many([(row["remote_id"], row["name"]) for row in batch])
        except (api.AuthExpired, api.RiskControl, api.RateLimited):
            raise
        except api.ApiError as exc:
            error = exc
        observed = {}
        done = []
        incomplete = False
        verification_error = None
        for row in batch:
            source = str(row["source_id"])
            if source not in observed:
                observed[source] = await directory(pan, source, context)
            try:
                entry = checked_entry(row, observed[source], (row["from_name"], row["name"]))
                ensure_free(row, observed[source])
            except (JobBlocked,JobRetry) as exc:
                verification_error=verification_error or exc
                continue
            if entry.name == row["name"]:
                done.append(row)
            else:
                incomplete = True
        if confirmed and done:
            await confirmed(done)
        if save:
            await save()
        verified.extend(done)
        if verification_error:
            raise verification_error
        if incomplete:
            if error:
                raise error
            raise JobRetry("115批量改名只确认了部分文件，未移动未确认项；稍后只补做剩余项", delay_seconds=30)
        # 下一批预检看本批完成后的真实名字，防止互换、大小写冲突或覆盖。
        listings.update(observed)
        remaining = [row for row in todo if row not in verified]
        for row in remaining:
            if str(row["source_id"]) in observed:
                ensure_free(row, observed[str(row["source_id"])])
    return verified


async def moved(pan, rows, *, context=None, save=None, confirmed=None):
    """同目标目录分组批量移动，前后逐ID/体积/名字复核；不把整批回执当成功。"""
    rows = list(rows)
    validate_rows(rows)
    groups = defaultdict(list)
    targets = set()
    for row in rows:
        target = str(row.get("target_id") or "")
        key = (target, row["name"].casefold())
        if not target or key in targets:
            raise JobBlocked("批次目标目录缺失或有重复目标名字，未移动")
        targets.add(key)
        groups[target].append(row)
    verified = []
    for target, group in groups.items():
        for batch in chunks(group):
            destination = await directory(pan, target, context)
            sources = {}
            todo = []
            done = []
            for row in batch:
                ensure_free(row, destination)
                if any(str(entry.remote_id) == str(row["remote_id"]) for entry in destination):
                    checked_entry(row, destination, (row["name"],))
                    done.append(row)
                    continue
                source = str(row["source_id"])
                if source not in sources:
                    sources[source] = await directory(pan, source, context)
                checked_entry(row, sources[source], (row["name"],))
                todo.append(row)
            if confirmed and done:
                await confirmed(done)
            if save:
                await save()
            verified.extend(done)
            if not todo:
                continue
            await cancelled(context)
            error = None
            try:
                await pan.move([row["remote_id"] for row in todo], target)
            except (api.AuthExpired, api.RiskControl, api.RateLimited):
                raise
            except api.ApiError as exc:
                error = exc
            observed = await directory(pan, target, context)
            done = []
            verification_error=None
            for row in todo:
                matches = [entry for entry in observed if str(entry.remote_id) == str(row["remote_id"])]
                if matches:
                    try:
                        checked_entry(row, observed, (row["name"],))
                        ensure_free(row, observed)
                    except (JobBlocked,JobRetry) as exc:
                        verification_error=verification_error or exc
                        continue
                    done.append(row)
            if confirmed and done:
                await confirmed(done)
            if save:
                await save()
            verified.extend(done)
            if verification_error:
                raise verification_error
            if len(done) != len(todo):
                if error:
                    raise error
                raise JobRetry("115批量移动只确认了部分文件，未宣称整批归位；稍后按编号只补做剩余项", delay_seconds=30)
    return verified
