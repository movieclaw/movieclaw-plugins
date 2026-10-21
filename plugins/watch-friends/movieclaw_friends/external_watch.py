"""个人 Emby/Plex 适配器：固定读取及单项已看端点、凭据留在请求头、禁止重定向。"""

import asyncio
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import httpx

from .peers import PeerError

MAX_RECORDS = 100_000


class ExternalHTTPError(PeerError):
    """只保留状态码和固定提示；外部响应正文可能含令牌或堆栈，不向页面透传。"""

    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def timestamp(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC).isoformat()
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (result.replace(tzinfo=UTC) if result.tzinfo is None else result).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def positive(value):
    try:
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            return None
        result = int(value)
        return result if 0 < result < 2**53 else None
    except (ValueError, TypeError, OverflowError):
        return None


def canonical(media):
    number = positive(media.get("tmdb_id"))
    if not number or media.get("kind") not in {"movie", "tv"}:
        return None
    if media["kind"] == "movie":
        return f"tmdb:movie:{number}"
    season, episode = media.get("season_number"), media.get("episode_number")
    if (
        type(season) is not int
        or type(episode) is not int
        or not 0 <= season <= 10000
        or not 0 <= episode <= 100000
    ):
        return None
    return f"tmdb:tv:{number}:s{season}:e{episode}"


class ExternalWatchClient:
    """每次请求固定解析后的地址，避免 DNS 重绑定；只读取当前令牌对应的用户状态。

    Plex 使用按令牌隔离的库条目状态，不使用管理员可读取全用户的播放流水接口。
    Emby 使用明确选择的账号，解析用户名后固定用户 ID；服务器 API Key 本身不代表用户。
    """

    def __init__(self, *, allow_private=False, transport=None, transport_factory=None):
        self.allow_private = allow_private
        self.transport = transport
        self.transport_factory = transport_factory

    def validate(self, value):
        try:
            if any(ord(char) < 33 for char in value.strip()):
                raise ValueError
            parsed = urlsplit(value.strip().rstrip("/"))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"(?:/[A-Za-z0-9_~-]+)*", parsed.path)
                or (not self.allow_private and parsed.scheme != "https")
            ):
                raise ValueError
            _ = parsed.port
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except (ValueError, AttributeError):
            raise PeerError(
                "服务器地址无效；默认需要 HTTPS，私网或 HTTP 请由管理员启用私网连接"
            ) from None

    async def get(self, base, path, token, platform, **params):
        return await self._request("GET", base, path, token, platform, params)

    async def mark_played(self, base, token, platform, account_id, external_id):
        """仅供已确认的对齐计划调用：只标记一个电影或单集，不发送播放进度。"""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", external_id):
            raise PeerError("外部条目编号无效")
        if platform == "emby":
            if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", account_id):
                raise PeerError("Emby 账号编号无效")
            return await self._request(
                "POST", base, f"/Users/{account_id}/PlayedItems/{external_id}", token, platform, {}
            )
        if platform != "plex" or not external_id.isdigit():
            raise PeerError("Plex 条目编号无效")
        return await self._request(
            "PUT",
            base,
            "/:/scrobble",
            token,
            platform,
            {"key": external_id, "identifier": "com.plexapp.plugins.library"},
        )

    async def active_items(self, source, base, token):
        if source["platform"] == "emby":
            result = await self.get(base, "/Sessions", token, "emby")
            rows = result.get("Items")
            if not isinstance(rows, list):
                raise PeerError("无法确认 Emby 活跃播放，续播回写已停止")
            return {
                str(r["NowPlayingItem"]["Id"])
                for r in rows
                if str(r.get("UserId")) == source["account_id"]
                and isinstance(r.get("NowPlayingItem"), dict)
            }
        result = await self.get(base, "/status/sessions", token, "plex")
        rows = result.get("MediaContainer", {}).get("Metadata", [])
        if not isinstance(rows, list):
            raise PeerError("无法确认 Plex 活跃播放，续播回写已停止")
        # 管理员令牌可能看到其他用户；保守保护同一条目的所有活跃会话。
        return {str(r["ratingKey"]) for r in rows if isinstance(r, dict) and r.get("ratingKey")}

    async def set_position(self, source, base, token, external_id, position, duration, current):
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", external_id)
            or not 0 < position < duration * 0.85
        ):
            raise PeerError("续播位置或条目编号无效")
        if source["platform"] == "emby":
            data = current.get("userdata", {})
            if (
                type(data.get("PlayCount")) is not int
                or data["PlayCount"] < 0
                or type(data.get("IsFavorite")) is not bool
                or data.get("Played") is not False
            ):
                raise PeerError("Emby 未返回完整的用户状态，已停止续播回写以保留原有次数和收藏")
            userdata = {
                k: v
                for k, v in current["userdata"].items()
                if k
                in {
                    "PlaybackPositionTicks",
                    "PlayCount",
                    "IsFavorite",
                    "LastPlayedDate",
                    "Played",
                    "Key",
                    "ItemId",
                    "Rating",
                    "UnplayedItemCount",
                    "PlayedPercentage",
                }
            }
            userdata["PlaybackPositionTicks"] = position * 10000
            return await self._request(
                "POST",
                base,
                f"/Users/{source['account_id']}/Items/{external_id}/UserData",
                token,
                "emby",
                {},
                body=userdata,
            )
        return await self._request(
            "GET",
            base,
            "/:/timeline",
            token,
            "plex",
            {
                "ratingKey": external_id,
                "key": "/library/metadata/" + external_id,
                "state": "stopped",
                "time": position,
                "duration": duration,
                "identifier": "com.plexapp.plugins.library",
                "X-Plex-Client-Identifier": "yingji-resume-sync",
            },
            raw_response=True,
        )

    async def _request(
        self, method, base, path, token, platform, params, *, body=None, raw_response=False
    ):
        base = self.validate(base)
        parsed = urlsplit(base)
        query = "?" + urlencode(params) if params else ""
        suffix = parsed.path + path + query
        headers = {
            "Accept": "application/json",
            "X-Emby-Token" if platform == "emby" else "X-Plex-Token": token,
        }
        extensions = {}
        target = f"{parsed.scheme}://{parsed.netloc}{suffix}"
        if self.transport is None:
            try:
                resolved = await asyncio.wait_for(
                    asyncio.to_thread(
                        socket.getaddrinfo,
                        parsed.hostname,
                        parsed.port or (443 if parsed.scheme == "https" else 80),
                        type=socket.SOCK_STREAM,
                    ),
                    5,
                )
                ips = [ipaddress.ip_address(row[4][0]) for row in resolved]
                ips = [getattr(ip, "ipv4_mapped", None) or ip for ip in ips]
                if not ips or any(
                    ip.is_link_local
                    or ip.is_multicast
                    or ip.is_unspecified
                    or (not self.allow_private and not ip.is_global)
                    for ip in ips
                ):
                    raise ValueError
            except (OSError, ValueError, TimeoutError):
                raise PeerError("服务器不可解析或指向未获准的网络；私网连接需管理员允许") from None
            address = f"[{ips[0]}]" if ips[0].version == 6 else str(ips[0])
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            target = f"{parsed.scheme}://{address}:{port}{suffix}"
            headers["Host"] = parsed.netloc
            extensions["sni_hostname"] = parsed.hostname
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport
                    or (self.transport_factory() if self.transport_factory else None),
                    timeout=20,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    method, target, headers=headers, extensions=extensions, json=body
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise ExternalHTTPError(
                        response.status_code, "外部账号认证失败，请检查令牌及账号访问权限"
                    )
                if response.status_code not in ({200} if method == "GET" else {200, 204}):
                    raise ExternalHTTPError(
                        response.status_code,
                        f"外部接口返回 HTTP {response.status_code}，请检查地址、平台和服务器版本",
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 8_000_000:
                        raise PeerError("外部服务器单页数据过大，已停止读取")
                # Plex 的 scrobble 成功可返回空正文或 HTML，只接受状态码，随后另读状态确认。
                if method != "GET" or raw_response:
                    return {}
                result = json.loads(content)
                if (
                    platform == "emby"
                    and (
                        path == "/Sessions"
                        or re.fullmatch(r"/Items/[A-Za-z0-9_-]+/Ancestors", path)
                    )
                    and isinstance(result, list)
                ):
                    result = {"Items": result}
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (httpx.HTTPError, ValueError):
            raise PeerError("暂时无法读取外部观影数据，请检查网络和账号后重试") from None

    async def read(
        self,
        platform,
        base,
        token,
        progress,
        *,
        account="",
        include_libraries=False,
        only_watched=True,
        excluded_libraries=(),
        included_libraries=None,
    ):
        excluded_libraries = {str(value) for value in excluded_libraries}
        included_libraries = (
            set(map(str, included_libraries)) if included_libraries is not None else None
        )
        if platform == "emby" and not only_watched:
            # 对齐直接按允许的库读取完整库存，避免先扫成人库、再重复全库核对来源。
            user = await self.emby_account(base, token, account)
            user_id = str(user["Id"])
            views = await self.get(base, f"/Users/{user_id}/Views", token, "emby")
            libraries = views.get("Items")
            if not isinstance(libraries, list) or len(libraries) > 200:
                raise PeerError("Emby 媒体库列表格式不兼容")
            records, names = {}, []
            for library in libraries:
                key = str(library.get("Id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
                    raise PeerError("Emby 媒体库编号无效")
                if key in excluded_libraries or (
                    included_libraries is not None and key not in included_libraries
                ):
                    continue
                names.append({"id": key, "name": str(library.get("Name") or "媒体库")[:120]})
                offset = len(records)
                part = await self._emby(
                    base,
                    token,
                    lambda count, base_count=offset: progress(base_count + count),
                    user_id,
                    only_watched=False,
                    parent_id=key,
                )
                for row in part["records"]:
                    identifier = row["external_id"]
                    if identifier not in records:
                        records[identifier] = row | {"library_ids": []}
                    if key not in records[identifier]["library_ids"]:
                        records[identifier]["library_ids"].append(key)
                if len(records) > MAX_RECORDS:
                    raise PeerError("允许库的库存超过单次十万条限制，请缩小范围")
            return {
                "account_id": user_id,
                "account_name": str(user.get("Name") or "Emby 用户"),
                "records": list(records.values()),
                "libraries": names,
            }
        result = await (
            self._emby(base, token, progress, account, only_watched=only_watched)
            if platform == "emby"
            else self._plex(
                base,
                token,
                progress,
                only_watched=only_watched,
                excluded_libraries=excluded_libraries,
                included_libraries=included_libraries,
            )
        )
        if include_libraries and platform == "emby":
            await self.emby_library_provenance(base, token, result, only_watched=only_watched)
        return result

    async def emby_account(self, base, token, account):
        """Emby 没有 Users/Me；用户名只做精确匹配，绝不选第一个用户或遍历其观影。"""
        account = account.strip()
        if not account or len(account) > 80 or any(ord(char) < 32 for char in account):
            raise PeerError("请先指定本人 Emby 用户名或用户 ID；API 密钥不绑定个人账号")
        is_id = re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", account
        )
        if is_id:
            user = await self.get(base, f"/Users/{account}", token, "emby")
        else:
            try:
                page = await self.get(
                    base,
                    "/Users/Query",
                    token,
                    "emby",
                    NameStartsWithOrGreater=account,
                    SortOrder="Ascending",
                    Limit=20,
                )
            except ExternalHTTPError as exc:
                if exc.status in {401, 403}:
                    raise PeerError(
                        "当前令牌无法按用户名查找，请填写本人 Emby 用户 ID，"
                        "或使用有查询权限的 API 密钥"
                    ) from None
                raise
            rows = page.get("Items")
            if not isinstance(rows, list):
                raise PeerError("Emby 用户查询格式不兼容，请直接填写本人用户 ID")
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("Name", "")).casefold() == account.casefold()
            ]
            if len(matches) != 1:
                raise PeerError("没有找到唯一匹配的 Emby 账号，请核对用户名，或直接填写本人用户 ID")
            user = matches[0]
        user_id = str(user.get("Id", ""))
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", user_id):
            raise PeerError("Emby 返回的账号 ID 无效，请核对所选账号")
        if is_id and user_id.replace("-", "").casefold() != account.replace("-", "").casefold():
            raise PeerError("Emby 返回账号与所选用户不一致，本次未读取观影数据")
        if isinstance(user.get("Policy"), dict) and user["Policy"].get("IsDisabled") is True:
            raise PeerError("所选 Emby 账号已停用，本次未读取观影数据")
        return user

    async def _emby(self, base, token, progress, account, *, only_watched=True, parent_id=None):
        user = await self.emby_account(base, token, account)
        user_id = str(user["Id"])
        result, start, shows, pages = [], 0, {}, set()
        while True:
            page = await self.get(
                base,
                f"/Users/{user_id}/Items",
                token,
                "emby",
                Recursive="true",
                **({"IsPlayed": "true"} if only_watched else {}),
                **({"ParentId": parent_id} if parent_id is not None else {}),
                IncludeItemTypes="Movie,Episode",
                Fields="ProviderIds,SeriesProviderIds,UserDataLastPlayedDate,UserDataPlaybackPositionTicks,UserDataPlayCount,MediaSources",
                EnableUserData="true",
                StartIndex=start,
                Limit=200,
            )
            rows = page.get("Items")
            if not isinstance(rows, list):
                raise PeerError("Emby 返回的已看列表格式不兼容")
            page_key = tuple(str(row.get("Id")) for row in rows if isinstance(row, dict))
            if rows and page_key in pages:
                raise PeerError("Emby 分页重复，本次未导入，请检查服务器版本")
            pages.add(page_key)
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("Type") not in {"Movie", "Episode"}
                    or (only_watched and (row.get("UserData") or {}).get("Played") is not True)
                ):
                    continue
                episode = row["Type"] == "Episode"
                ids = row.get("SeriesProviderIds") if episode else row.get("ProviderIds")
                if (
                    episode
                    and (not isinstance(ids, dict) or not positive(ids.get("Tmdb")))
                    and row.get("SeriesId")
                ):
                    series = str(row["SeriesId"])
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", series):
                        continue
                    if series not in shows:
                        shows[series] = await self.get(
                            base, f"/Users/{user_id}/Items/{series}", token, "emby"
                        )
                    ids = shows[series].get("ProviderIds")
                ids = ids if isinstance(ids, dict) else {}
                media = {
                    "title": str(
                        (row.get("SeriesName") if episode else row.get("Name")) or "未命名影片"
                    )[:300],
                    "kind": "tv" if episode else "movie",
                    "tmdb_id": positive(ids.get("Tmdb")),
                    "year": positive(row.get("ProductionYear")),
                    "poster_path": None,
                }
                if episode:
                    media.update(
                        season_number=row.get("ParentIndexNumber"),
                        episode_number=row.get("IndexNumber"),
                        episode_title=str(row.get("Name") or "")[:300],
                    )
                external_id = str(row.get("Id", ""))[:160]
                if external_id:
                    result.append(
                        {
                            "external_id": external_id,
                            "media": media,
                            "watched_at": timestamp(
                                (row.get("UserData") or {}).get("LastPlayedDate")
                            ),
                            "play_count": positive((row.get("UserData") or {}).get("PlayCount")),
                            "position_ms": int(
                                (row.get("UserData") or {}).get("PlaybackPositionTicks", 0)
                            )
                            // 10000,
                            "duration_ms": int(row.get("RunTimeTicks") or 0) // 10000,
                            "edition": str(row.get("EditionName") or "")[:120],
                            "version_count": len(row.get("MediaSources") or [None]),
                            **(
                                {"played": (row.get("UserData") or {}).get("Played")}
                                if not only_watched
                                else {}
                            ),
                        }
                    )
            start += len(rows)
            progress(start)
            if start > MAX_RECORDS:
                raise PeerError("已看数据超过单次十万条限制，请分批导出后导入")
            total = page.get("TotalRecordCount")
            if not rows and type(total) is int and start < total:
                raise PeerError("Emby 分页数据不完整，本次未导入，请重试")
            if not rows or (type(total) is int and start >= total):
                break
            if len(rows) < 200 and total is None:
                break
        return {
            "account_id": user_id,
            "account_name": str(user.get("Name") or "Emby 用户")[:80],
            "records": result,
        }

    async def emby_library_provenance(self, base, token, result, *, only_watched=True):
        user_id = result["account_id"]
        views = await self.get(base, f"/Users/{user_id}/Views", token, "emby")
        if not isinstance(views.get("Items"), list):
            raise PeerError("Emby 媒体库列表格式不兼容")
        libraries = [
            {"id": str(row.get("Id", "")), "name": str(row.get("Name", ""))[:120]}
            for row in views["Items"]
            if isinstance(row, dict)
            and row.get("Name")
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(row.get("Id", "")))
        ]
        if len(libraries) > 200:
            raise PeerError("Emby 媒体库数量超出限制")
        records = {row["external_id"]: row for row in result["records"]}
        for row in records.values():
            row["library_ids"] = []
        for library in libraries:
            start, seen = 0, set()
            while True:
                page = await self.get(
                    base,
                    f"/Users/{user_id}/Items",
                    token,
                    "emby",
                    ParentId=library["id"],
                    Recursive="true",
                    **({"IsPlayed": "true"} if only_watched else {}),
                    IncludeItemTypes="Movie,Episode",
                    EnableUserData="false",
                    StartIndex=start,
                    Limit=200,
                )
                rows = page.get("Items")
                if not isinstance(rows, list):
                    raise PeerError("Emby 媒体库来源查询失败")
                keys = tuple(str(row.get("Id")) for row in rows if isinstance(row, dict))
                if rows and keys in seen:
                    raise PeerError("Emby 媒体库分页重复")
                seen.add(keys)
                for key in keys:
                    if key in records and library["id"] not in records[key]["library_ids"]:
                        records[key]["library_ids"].append(library["id"])
                start += len(rows)
                total = page.get("TotalRecordCount")
                if start > MAX_RECORDS or (not rows and type(total) is int and start < total):
                    raise PeerError("Emby 媒体库来源核对不完整")
                if (
                    not rows
                    or (type(total) is int and start >= total)
                    or (total is None and len(rows) < 200)
                ):
                    break
        result["libraries"] = libraries

    async def _plex(
        self,
        base,
        token,
        progress,
        *,
        only_watched=True,
        excluded_libraries=(),
        included_libraries=None,
    ):
        sections = await self.get(base, "/library/sections", token, "plex")
        directories = sections.get("MediaContainer", {}).get("Directory")
        if not isinstance(directories, list):
            raise PeerError("无法读取 Plex 账号可访问的媒体库，请检查个人令牌")
        result, shows = [], {}

        def tmdb(row):
            for entry in row.get("Guid", []) if isinstance(row.get("Guid"), list) else []:
                match = re.fullmatch(
                    r"tmdb://([1-9][0-9]*)",
                    str(entry.get("id", "")) if isinstance(entry, dict) else "",
                )
                if match:
                    return positive(match[1])
            return None

        for library in directories:
            if not isinstance(library, dict):
                continue
            key = str(library.get("key", ""))
            if key in excluded_libraries or (
                included_libraries is not None and key not in included_libraries
            ):
                continue
            if library.get("type") not in {"movie", "show"} or not key.isdigit():
                continue
            start, pages = 0, set()
            while True:
                page = await self.get(
                    base,
                    f"/library/sections/{key}/all",
                    token,
                    "plex",
                    type=1 if library["type"] == "movie" else 4,
                    **({"unwatched": 0} if only_watched else {}),
                    includeGuids=1,
                    **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 200},
                )
                container = page.get("MediaContainer", {})
                rows = container.get("Metadata", [])
                if not isinstance(rows, list):
                    raise PeerError("Plex 返回的已看列表格式不兼容")
                page_key = tuple(str(row.get("ratingKey")) for row in rows if isinstance(row, dict))
                if rows and page_key in pages:
                    raise PeerError("Plex 分页重复，本次未导入，请检查服务器版本")
                pages.add(page_key)
                for row in rows:
                    if (
                        not isinstance(row, dict)
                        or (only_watched and not positive(row.get("viewCount")))
                        or row.get("type") not in {"movie", "episode"}
                    ):
                        continue
                    episode = row["type"] == "episode"
                    number = tmdb(row) if not episode else None
                    if episode:
                        parent = str(row.get("grandparentRatingKey", ""))
                        if parent.isdigit():
                            if parent not in shows:
                                detail = await self.get(
                                    base,
                                    f"/library/metadata/{parent}",
                                    token,
                                    "plex",
                                    includeGuids=1,
                                )
                                metadata = detail.get("MediaContainer", {}).get("Metadata", [])
                                shows[parent] = tmdb(metadata[0]) if metadata else None
                            number = shows[parent]
                    media = {
                        "title": str(
                            (row.get("grandparentTitle") if episode else row.get("title"))
                            or "未命名影片"
                        )[:300],
                        "kind": "tv" if episode else "movie",
                        "tmdb_id": number,
                        "year": positive(row.get("year")),
                        "poster_path": None,
                    }
                    if episode:
                        media.update(
                            season_number=row.get("parentIndex"),
                            episode_number=row.get("index"),
                            episode_title=str(row.get("title") or "")[:300],
                        )
                    external_id = str(row.get("ratingKey", ""))[:160]
                    if external_id:
                        result.append(
                            {
                                "external_id": external_id,
                                "library_ids": [key],
                                "media": media,
                                "watched_at": timestamp(row.get("lastViewedAt")),
                                "play_count": positive(row.get("viewCount")),
                                "position_ms": int(row.get("viewOffset") or 0),
                                "duration_ms": int(row.get("duration") or 0),
                                "edition": str(row.get("editionTitle") or "")[:120],
                                "version_count": len(row.get("Media") or [None]),
                                **(
                                    {"played": bool(positive(row.get("viewCount")))}
                                    if not only_watched
                                    else {}
                                ),
                            }
                        )
                start += len(rows)
                progress(len(result))
                if len(result) > MAX_RECORDS or start > MAX_RECORDS:
                    raise PeerError("已看数据超过单次十万条限制，请分批导入")
                total = container.get("totalSize")
                if not rows and type(total) is int and start < total:
                    raise PeerError("Plex 分页数据不完整，本次未导入，请重试")
                if (
                    not rows
                    or (type(total) is int and start >= total)
                    or (total is None and len(rows) < 200)
                ):
                    break
        return {
            "account_id": "token-user",
            "account_name": "Plex 当前令牌账号",
            "records": result,
            "libraries": [
                {
                    "id": str(row["key"]),
                    "name": str(row.get("title") or row.get("type") or "媒体库")[:120],
                }
                for row in directories
                if isinstance(row, dict)
                and str(row.get("key", "")).isdigit()
                and row.get("type") in {"movie", "show"}
            ],
        }
