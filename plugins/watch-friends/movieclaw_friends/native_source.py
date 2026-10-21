"""原生 inline 插件的只读观影数据源，仅调用 SDK 已登记的宿主操作。"""

import asyncio
from datetime import UTC, datetime

from .media import sanitize_media
from .upstream import UpstreamError, _ongoing, _record

HOST_PERMISSIONS = ("members.list", "playback.history", "search.library")


def _created_at(value) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except (AttributeError, TypeError, ValueError):
        raise UpstreamError(
            "无法确认 MovieClaw 成员的创建时间，请升级宿主或重新登录", 403
        ) from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def account_key(principal) -> str:
    """仅接收宿主 require_login 产出的身份，不接受客户端提供的用户编号或昵称。

    成员 ID 可能在删除后复用，加入创建时间，防止后来同号成员继承旧好友关系。
    插件、Agent 与分享访客不是人在宿主里的登录身份，不能借其管理权限入场。
    """
    if getattr(principal, "plugin", None) is not None:
        raise UpstreamError("请使用 MovieClaw 本人登录账号访问映迹", 403)
    kind = getattr(principal, "kind", None)
    if kind == "admin":
        return "admin:0"
    member_id = getattr(principal, "member_id", None)
    member = getattr(principal, "member", None)
    created = getattr(member, "created_at", None)
    if kind != "member" or type(member_id) is not int or member_id < 1 or created is None:
        raise UpstreamError("无法确认 MovieClaw 本人账号及成员生命周期，请重新登录", 403)
    _created_at(created)
    return f"member:{member_id}:{created.isoformat() if isinstance(created, datetime) else created}"


class NativeSource:
    """以插件获批身份调用正式 HOST_OPS，再按可信账号严格限定成员读取范围。

    ``ops`` 是 ``await ctx.use(HOST_OPS).client(ctx)``，只需批准
    ``members.list``、``playback.history`` 与 ``search.library``。这里没有网络服务、宿主令牌、数据库
    连接或宿主内部服务导入。account 字符串必须来自上面的 account_key，不能从
    浏览器请求字段生成；持久化该键可以在好友访问时复核原成员仍然存在。

    HOST_OPS 没有逐请求代表当前成员的能力，因此片单必须由浏览器以当前宿主
    登录身份读取并主动发布；不能用插件默认管理员视角伪称是普通成员的私有片单。
    海报只输出安全 TMDB 相对路径；只为已获准共享的条目查询轻量搜索候选，按
    本机条目与库编号精确核对后补充公开身份，不调用会触发浏览事件的完整详情。
    """

    browser_required = True

    def __init__(self, ops) -> None:
        self.ops = ops

    async def _call(self, operation: str, arguments: dict):
        try:
            # 正式 SDK 返回已剥离 ApiResponse 信封的 data；不构造任意 HTTP 路径。
            return await self.ops.call(operation, arguments)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status in (401, 403):
                raise UpstreamError(
                    "映迹尚未获准读取宿主成员与观影记录，请检查插件授权", status
                ) from None
            raise UpstreamError("无法通过宿主操作读取观影数据，请检查宿主版本和插件授权") from None

    async def identity(self, account: str) -> dict:
        if account == "admin:0":
            return {"username": account, "nickname": "管理员", "role": "admin", "member_id": 0}
        try:
            kind, raw_id, created = account.split(":", 2)
            member_id = int(raw_id)
        except (AttributeError, TypeError, ValueError):
            raise UpstreamError(
                "原生账号标识无效，请从 MovieClaw 登录页面重新进入插件", 401
            ) from None
        if kind != "member" or member_id < 1 or str(member_id) != raw_id:
            raise UpstreamError("原生账号标识无效，请从 MovieClaw 登录页面重新进入插件", 401)
        expected_created = _created_at(created)
        members = await self._call("members.list", {})
        if not isinstance(members, list):
            raise UpstreamError("宿主返回的成员列表格式不兼容")
        matches = [row for row in members if isinstance(row, dict) and row.get("id") == member_id]
        if len(matches) != 1 or matches[0].get("status") != "active":
            raise UpstreamError("原 MovieClaw 成员已删除或停用，已停止共享其观影数据", 401)
        member = matches[0]
        if _created_at(member.get("created_at")) != expected_created:
            raise UpstreamError("原 MovieClaw 成员身份已经变化，已停止共享旧账号数据", 401)
        return {
            "username": account,
            "nickname": member.get("nickname") or member.get("username") or "成员",
            "role": "member",
            "member_id": member_id,
        }

    async def _history(self, member_id: int) -> list:
        data = await self._call(
            "playback.history", {"member_id": member_id, "scope": "all", "limit": 50}
        )
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise UpstreamError("宿主返回的观影记录格式不兼容")
        entries = data["entries"]
        if any(
            not isinstance(row, dict) or not isinstance(row.get("media"), dict) for row in entries
        ):
            raise UpstreamError("宿主返回的观影记录格式不兼容")
        return entries[:50]

    async def _enrich_media(self, entries: list) -> bool:
        """局部按标题合并候选搜索；标题不是身份，必须核对条目与所属库两个编号。"""
        queries: dict[str, list[tuple[int, int, dict]]] = {}
        for row in entries:
            media = row["media"]
            item_id, library_id, title = (
                media.get("media_item_id"),
                media.get("library_id"),
                media.get("title"),
            )
            if (
                media.get("browsable") is False
                or type(item_id) is not int
                or item_id < 1
                or type(library_id) is not int
                or library_id < 1
                or not isinstance(title, str)
                or not title.strip()
            ):
                continue
            if (
                type(media.get("tmdb_id")) is int
                and media["tmdb_id"] > 0
                and media.get("kind") in {"movie", "tv"}
                and not getattr(self, "display_policy", None)
            ):
                continue
            query = title.strip()[:100]
            if query not in queries and len(queries) >= 50:
                continue
            queries.setdefault(query, []).append((item_id, library_id, media))
        if not queries:
            return False
        semaphore = asyncio.Semaphore(4)

        async def lookup(query: str, targets: list):
            async with semaphore:
                try:
                    found = await self._call("search.library", {"q": query, "limit": 100})
                except UpstreamError:
                    return
            hits = found.get("items") if isinstance(found, dict) else None
            if not isinstance(hits, list):
                return
            for item_id, library_id, media in targets:
                matches = [
                    hit
                    for hit in hits[:100]
                    if isinstance(hit, dict)
                    and isinstance(hit.get("item"), dict)
                    and type(hit["item"].get("media_item_id")) is int
                    and hit["item"]["media_item_id"] == item_id
                    and isinstance(hit.get("library_ids"), list)
                    and any(type(id) is int and id == library_id for id in hit["library_ids"])
                ]
                if len(matches) != 1:
                    continue
                media["_display_library_ids"] = [
                    value for value in matches[0]["library_ids"] if type(value) is int and value > 0
                ]
                public = sanitize_media(matches[0]["item"])
                if public.get("kind") in {"movie", "tv"}:
                    media["kind"] = public["kind"]
                    if type(public.get("tmdb_id")) is int and public["tmdb_id"] > 0:
                        media["tmdb_id"] = public["tmdb_id"]
                if public.get("poster_path"):
                    media["poster_path"] = public["poster_path"]

        await asyncio.gather(*(lookup(query, targets) for query, targets in queries.items()))
        return True

    async def recommendation_provenance(self, account: str, media: dict) -> list:
        """本人主动选择的作品按需核对库存，不以看过/导入过作为公开推荐前提。

        沿用已批准的只读搜索；必须完整分页并匹配 TMDB 类型与编号，不能把
        同名影片或前端传来的库编号当作来源。结果只存插件私有索引，不导入足迹。
        """
        identity = await self.identity(account)
        query = media.get("title", "").strip()[:100]
        if not query:
            raise UpstreamError("请先选择有名称的影片")
        cursor, seen, records = None, set(), []
        for _ in range(20):
            args = {"q": query, "limit": 100}
            if cursor:
                args["cursor"] = cursor
            result = await self._call("search.library", args)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise UpstreamError("主程序媒体库搜索格式不兼容，暂未核实影片来源")
            for hit in result["items"]:
                item = hit.get("item") if isinstance(hit, dict) else None
                if not isinstance(item, dict) or (
                    type(item.get("tmdb_id")) is not int
                    or item["tmdb_id"] != media.get("tmdb_id")
                    or item.get("kind") != media.get("kind")
                ):
                    continue
                number, libraries = item.get("media_item_id"), hit.get("library_ids")
                if (
                    type(number) is not int
                    or number <= 0
                    or not isinstance(libraries, list)
                    or not libraries
                    or any(type(lib) is not int or lib <= 0 for lib in libraries)
                ):
                    raise UpstreamError("作品所属媒体库未核实，请稍后重试")
                records.append(
                    {
                        "external_id": f"item:{number}",
                        "media": sanitize_media(item),
                        "library_ids": sorted(set(libraries)),
                    }
                )
            cursor = result.get("next_cursor")
            if not cursor:
                if identity["role"] == "member":
                    await self.identity(account)
                return records
            if not isinstance(cursor, str) or len(cursor) > 200 or cursor in seen:
                raise UpstreamError("媒体库搜索分页异常，暂未核实影片来源")
            seen.add(cursor)
        raise UpstreamError("媒体库搜索尚未核对完整，请使用更准确的影片名称")

    async def history(self, account: str) -> list:
        identity = await self.identity(account)
        entries = await self._history(identity["member_id"])
        if identity["role"] == "member":
            await self.identity(account)
        if await self._enrich_media(entries) and identity["role"] == "member":
            await self.identity(account)
        if getattr(self, "display_policy", None):
            self.display_policy.remember_native(account, entries)
        return [_record(row) for row in entries]

    async def archive(self, account: str) -> list:
        """分页读取本人的完整已完成播放记录，游标异常时整次失败，不静默截断。"""
        identity = await self.identity(account)
        records, seen, before = [], set(), None
        total = 0
        while True:
            arguments = {"member_id": identity["member_id"], "scope": "all", "limit": 200}
            if before is not None:
                arguments["before"] = before
            data = await self._call("playback.history", arguments)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise UpstreamError("宿主返回的完整观影档案格式不兼容")
            rows = data["entries"]
            total += len(rows)
            if total > 100000:
                raise UpstreamError("观影档案超过单次十万条限制，本次未导入")
            completed = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("media"), dict):
                    raise UpstreamError("宿主返回的观影档案条目格式不兼容")
                if row.get("completed") is True:
                    completed.append(row)
            # 每批最多 50 个标题查找，避免大型档案遗失后半段的公开作品身份。
            for index in range(0, len(completed), 50):
                await self._enrich_media(completed[index : index + 50])
            for row in completed:
                number = row.get("id")
                if type(number) is not int or number < 1:
                    raise UpstreamError("宿主记录缺少稳定身份，本次未导入")
                records.append(
                    {
                        "external_id": str(number),
                        "media": sanitize_media(row["media"]),
                        "watched_at": row.get("ended_at") or row.get("started_at"),
                        "library_ids": row["media"].get("_display_library_ids", [])
                        if getattr(self, "display_policy", None)
                        else [row["media"]["library_id"]]
                        if type(row["media"].get("library_id")) is int
                        else [],
                    }
                )
            cursor = data.get("next_cursor")
            if cursor is None:
                if data.get("has_more") is True:
                    raise UpstreamError("宿主观影分页缺少游标，本次未导入")
                break
            if (
                type(cursor) is not int
                or cursor < 1
                or cursor in seen
                or (before is not None and cursor >= before)
            ):
                raise UpstreamError("宿主观影分页重复或无效，本次未导入")
            seen.add(cursor)
            before = cursor
            if identity["role"] == "member":
                await self.identity(account)
        if identity["role"] == "member":
            await self.identity(account)
        return records

    async def snapshot(
        self, account: str, member_id: int, share_current: bool, share_history: bool
    ) -> dict:
        identity = await self.identity(account)
        if type(member_id) is not int or identity["member_id"] != member_id:
            raise UpstreamError("当前 MovieClaw 身份与观影数据归属不一致，已拒绝读取", 403)
        entries = await self._history(member_id) if share_current or share_history else []
        if (share_current or share_history) and identity["role"] == "member":
            # 两个宿主操作之间成员可能被删除并复用 ID；返回前再核对生命周期，
            # 避免把新成员的数据发布到旧账号的好友关系或公开预览中。
            await self.identity(account)
        observed = datetime.now(UTC)
        current = next((row for row in entries if _ongoing(row, observed)), None)
        history = (
            [row for row in entries if row.get("ended_at") is not None] if share_history else []
        )
        selected = ([current] if share_current and current is not None else []) + history
        if await self._enrich_media(selected) and identity["role"] == "member":
            await self.identity(account)
        if getattr(self, "display_policy", None):
            self.display_policy.remember_native(account, selected)
        return {
            "current": _record(current) if share_current and current is not None else None,
            "history": [_record(row) for row in history],
            "sharing": {"current": bool(share_current), "history": bool(share_history)},
            "observed_at": observed.isoformat(),
        }

    async def collections(self, account: str) -> list:
        raise UpstreamError(
            "原生模式请在当前登录的 MovieClaw 页面选择本人自建片单，再主动发布快照", 409
        )

    async def collection(self, account: str, id: int) -> dict:
        raise UpstreamError(
            "原生模式请在当前登录的 MovieClaw 页面选择本人自建片单，再主动发布快照", 409
        )
