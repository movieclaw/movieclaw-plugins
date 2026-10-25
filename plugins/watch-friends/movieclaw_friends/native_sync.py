"""MovieClaw 本人设备授权与后台已看同步；不借用插件管理员身份写成员数据。"""

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

from .config import origin_url
from .media import sanitize_media
from .peers import PeerError
from .watch_alignment import source_pin, sync_key


class NativeSync:
    """通过正式设备配对取得独立凭据；固定同源 API，凭据只存加密私有表。"""

    def __init__(self, app, archive, settings, user_for, transport_factory=None, transport=None):
        self.app, self.archive, self.store, self.settings = app, archive, archive.store, settings
        self.user_for, self.transport_factory, self.transport = (
            user_for,
            transport_factory,
            transport,
        )
        self.pending, self.locks = {}, {}
        self._read_clients = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS native_sync_grants(
                user_id TEXT PRIMARY KEY,username TEXT NOT NULL,role TEXT NOT NULL,
                account TEXT NOT NULL,secret TEXT NOT NULL,origin TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1);
        """)

    @property
    def api_origin(self):
        # 本机 API 与公开联邦地址分开；只使用管理员配置，绝不从请求头猜测目标。
        return origin_url(self.settings.upstream_url or self.settings.public_url)

    def client(self):
        return httpx.AsyncClient(
            transport=self.transport
            or (self.transport_factory() if self.transport_factory else None),
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )

    @asynccontextmanager
    async def read_session(self):
        # 同一轮读请求复用连接，避免为几千个查询反复建连；不跨账号转发请求头。
        task = asyncio.current_task()
        async with self.client() as client:
            self._read_clients[task] = client
            try:
                yield
            finally:
                self._read_clients.pop(task, None)

    async def request(self, method, path, token="", body=None):
        target = self.api_origin
        if target == "http://movieclaw.invalid":
            raise HTTPException(
                409,
                "尚未配置 MovieClaw 接口地址。请管理员设置主程序外部访问地址，"
                "或在映迹插件配置中填写本机接口地址，再重新启用插件。",
            )

        async def send(client):
            response = await client.request(
                method,
                target + "/api/v1" + path,
                headers={"Authorization": "Bearer " + token} if token else {},
                json=body,
            )
            if response.status_code == 202:
                return None
            if response.status_code == 429:
                raise HTTPException(429, "MovieClaw 请求过快，请稍后重试")
            if 300 <= response.status_code < 400:
                raise PeerError(
                    "MovieClaw 本人接口被重定向；请使用最终访问地址，并检查反向代理是否跳转到登录页"
                )
            if response.status_code == 404:
                raise PeerError(
                    "MovieClaw 本人接口不存在（HTTP 404）；请检查主程序版本，以及接口地址是否指向 MovieClaw"
                )
            if response.status_code not in {200, 201}:
                raise PeerError(
                    f"MovieClaw 接口返回 HTTP {response.status_code}；请检查本人授权及反向代理访问规则"
                )
            if len(response.content) > 8000000:
                raise PeerError("MovieClaw 返回数据过大，请缩小同步范围")
            try:
                result = response.json()
            except ValueError:
                raise PeerError(
                    "MovieClaw 本人接口未返回有效 JSON；请检查接口地址及反向代理，避免返回登录网页"
                ) from None
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or "data" not in result
            ):
                raise PeerError("MovieClaw 本人接口响应格式不兼容；请检查主程序版本和反向代理配置")
            return result["data"]

        try:
            client = self._read_clients.get(asyncio.current_task())
            if client is not None:
                return await send(client)
            async with self.client() as client:
                return await send(client)
        except httpx.TimeoutException:
            raise PeerError(
                "MovieClaw 本人接口请求超时；请检查服务器能否连回自身地址，或配置映迹本机接口地址"
            ) from None
        except httpx.ProxyError:
            raise PeerError("MovieClaw 本人接口代理连接失败；请检查映迹的网络代理规则") from None
        except httpx.ConnectError as exc:
            # 仅判断固定错误标志，不把异常原文、地址、证书内容或凭据返回用户。
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "certificate verify failed" in str(exc).lower():
                raise PeerError(
                    "MovieClaw HTTPS 证书校验失败；请检查证书域名、有效期及证书链，"
                    "或由管理员配置受信任的本机接口地址"
                ) from None
            raise PeerError(
                "MovieClaw 无法连接自身接口；请检查域名解析和端口，或配置映迹本机接口地址"
            ) from None
        except httpx.HTTPError:
            raise PeerError("MovieClaw 本人接口连接中断，请重试并检查服务器网络出口") from None
        except ValueError:
            raise PeerError("MovieClaw 本人接口客户端配置无效；请检查本机接口地址及网络代理设置") from None

    def source_for(self, owner):
        row = self.store.one("SELECT * FROM native_sync_grants WHERE user_id=?", (owner,))
        if not row:
            raise HTTPException(409, "请先在后台同步设置中配对本人 MovieClaw 账号")
        if self.store.decrypt(row["origin"]) != self.api_origin:
            raise HTTPException(409, "宿主地址已变化，请重新配对本人设备")
        return {
            "id": "movieclaw",
            "platform": "movieclaw",
            "origin": row["origin"],
            "secret": row["secret"],
            "account_id": row["username"],
            "account_name": row["username"],
            "grant_revision": row["revision"],
            "owner": owner,
        }

    async def verify(self, owner):
        source = self.source_for(owner)
        grant = self.store.one("SELECT * FROM native_sync_grants WHERE user_id=?", (owner,))
        user = self.store.one("SELECT * FROM users WHERE id=?", (owner,))
        identity = await self.archive.source.identity(self.store.decrypt(user["token"]))
        if identity["username"] != grant["account"] or identity["role"] != grant["role"]:
            raise PeerError("原 MovieClaw 账号已失效，请重新配对")
        current = await self.request("GET", "/auth/me", self.store.decrypt(source["secret"]))
        if current.get("role") != grant["role"] or current.get("username") != grant["username"]:
            raise PeerError("后台设备凭据不再属于本人账号")
        if source_pin(self.source_for(owner)) != source_pin(source):
            raise PeerError("本人设备授权已变化")
        return source

    async def movie_states(self, library_id, token):
        """按正式库筛选分组读取电影状态；三组必须互斥，旧服忽略筛选时回退查询。"""
        result = {}
        try:
            for mode in ("played", "watching", "unwatched"):
                offset = 0
                while True:
                    items = await self.request(
                        "GET",
                        f"/libraries/{library_id}/items?limit=200&offset={offset}&w={mode}",
                        token,
                    )
                    if not isinstance(items, list):
                        return None
                    for item in items:
                        number = item.get("media_item_id")
                        if (
                            type(number) is not int
                            or number in result
                            or item.get("kind") != "movie"
                        ):
                            return None
                        result[number] = (
                            None
                            if mode == "watching"
                            else {"played": mode == "played", "position_ms": 0, "duration_ms": None}
                        )
                    if len(result) > 100000:
                        return None
                    if len(items) < 200:
                        break
                    offset += len(items)
        except PeerError:
            # 不支持筛选、分页异常或服务版本不兼容时，不从半份分组推测已看。
            return None
        return result

    async def read(self, owner, selected, progress):
        async with self.read_session():
            return await self._read(owner, selected, progress)

    async def _read(self, owner, selected, progress):
        source = await self.verify(owner)
        token = self.store.decrypt(source["secret"])
        libraries = await self.request("GET", "/libraries?scope=visible", token)
        if not isinstance(libraries, list) or not set(selected) <= set(
            str(r["id"]) for r in libraries
        ):
            raise PeerError("MovieClaw 所选媒体库已不可见")
        records = {}
        for library in libraries:
            if str(library["id"]) not in selected or library.get("kind") not in {"movie", "tv"}:
                continue
            movie_states = (
                await self.movie_states(library["id"], token)
                if library.get("kind") == "movie"
                else None
            )
            offset, seen = 0, set()
            while True:
                items = await self.request(
                    "GET", f"/libraries/{library['id']}/items?limit=200&offset={offset}", token
                )
                if not isinstance(items, list) or any(r["media_item_id"] in seen for r in items):
                    raise PeerError("MovieClaw 库存分页不完整")
                for item in items:
                    number = item["media_item_id"]
                    seen.add(number)
                    media = sanitize_media(item)
                    units = [None] if media.get("kind") == "movie" else item.get("seasons", [])
                    for season in units:
                        if season is None:
                            entries = [{"owned": True, "episode_number": None}]
                        else:
                            page = await self.request(
                                "GET",
                                f"/playback/items/{number}/episodes?season_number={season}",
                                token,
                            )
                            if page.get("season_number") != season or not isinstance(
                                page.get("episodes"), list
                            ):
                                raise PeerError("MovieClaw 季集状态无法核实")
                            entries = page["episodes"]
                        for episode in entries:
                            if episode.get("owned") is not True:
                                continue
                            target = {"media_item_id": number}
                            unit = dict(media)
                            if season is not None:
                                target.update(
                                    season_number=season,
                                    episode_number=episode.get("episode_number"),
                                )
                                unit.update(
                                    season_number=season,
                                    episode_number=episode.get("episode_number"),
                                )
                            if not sync_key(unit):
                                continue
                            identifier = (
                                str(number)
                                if season is None
                                else f"{number}:{season}:{episode['episode_number']}"
                            )
                            if identifier in records:
                                records[identifier]["library_ids"].append(str(library["id"]))
                                continue
                            # 季集接口已经按当前成员批量返回已看与位置。未播放或已看
                            # 单集无需再查 /resume；只有正在续看的单集补读真实片长。
                            batch = (
                                season is not None
                                and type(episode.get("played")) is bool
                                and type(episode.get("position_ms")) is int
                                and episode["position_ms"] >= 0
                            )
                            movie_state = (
                                movie_states.get(number)
                                if season is None and movie_states is not None
                                else None
                            )
                            if movie_state is not None:
                                state = movie_state
                            elif batch and (episode["played"] or episode["position_ms"] == 0):
                                state = {
                                    "played": episode["played"],
                                    "position_ms": episode["position_ms"],
                                    "duration_ms": None,
                                }
                            else:
                                # 旧服务器缺少批量状态字段时仍按单元查询，不猜测未看。
                                state = await self.request(
                                    "GET", "/playback/resume?" + urlencode(target), token
                                )
                            if type(state.get("played")) is not bool:
                                raise PeerError("MovieClaw 已看状态无法核实")
                            records[identifier] = {
                                "external_id": identifier,
                                "target": target,
                                "media": unit,
                                "library_ids": [str(library["id"])],
                                **state,
                            }
                            if len(records) == 1 or len(records) % 100 == 0:
                                progress(len(records))
                            if len(records) > 100000:
                                raise PeerError("MovieClaw 库存超过十万单元，请缩小范围")
                if len(items) < 200:
                    break
                offset += len(items)
        progress(len(records))
        return {"account_id": source["account_id"], "records": list(records.values())}

    async def inspect(self, owner, row, *, include_resume=False):
        source = await self.verify(owner)
        token = self.store.decrypt(source["secret"])
        media, target = row["media"], row["target"]
        ref = f"tmdb:{media['kind']}:{media['tmdb_id']}"
        found = await self.request("GET", "/discover/titles/" + ref, token)
        links = found.get("library_links", [])
        ids = {r["media_item_id"] for r in links}
        if (
            found.get("title", {}).get("title_ref") != ref
            or found.get("title", {}).get("media_type") != media["kind"]
            or ids != {target["media_item_id"]}
        ):
            raise PeerError("MovieClaw 作品身份或匹配唯一性已变化")
        if media["kind"] == "tv":
            episodes = await self.request(
                "GET",
                f"/playback/items/{target['media_item_id']}/episodes?season_number={target['season_number']}",
                token,
            )
            if (
                len(
                    [
                        e
                        for e in episodes.get("episodes", [])
                        if e.get("owned") is True
                        and e.get("episode_number") == target["episode_number"]
                    ]
                )
                != 1
            ):
                raise PeerError("MovieClaw 单集片源已变化")
        versions = 1
        if include_resume:
            if media["kind"] == "tv":
                selected = [
                    e
                    for e in episodes["episodes"]
                    if e.get("episode_number") == target["episode_number"]
                    and e.get("owned") is True
                ]
                versions = len(selected[0].get("file_ids", []))
            else:
                versions = found.get("title", {}).get("library_status", {}).get("file_count", 0)
        state = await self.request("GET", "/playback/resume?" + urlencode(target), token)
        if include_resume:
            state["version_count"] = versions
        return {**state, "library_ids": set(str(r["library_id"]) for r in links)}

    async def mark(self, owner, row):
        source = await self.verify(owner)
        return await self.request(
            "POST",
            "/playback/marks",
            self.store.decrypt(source["secret"]),
            {**row["target"], "played": True, "device_id": "yingji-watched-sync"},
        )

    async def active_items(self, owner):
        from datetime import UTC, datetime

        from .upstream import _ongoing

        user = self.store.one("SELECT * FROM users WHERE id=?", (owner,))
        rows = await self.archive.source.history(self.store.decrypt(user["token"]))
        result = set()
        for row in rows:
            if _ongoing(row, datetime.now(UTC)):
                media = row["media"]
                number = str(media.get("media_item_id"))
                if media.get("kind") == "tv":
                    number += f":{media.get('season_number')}:{media.get('episode_number')}"
                result.add(number)
        return result

    async def position(self, owner, row, position):
        source = await self.verify(owner)
        return await self.request(
            "POST",
            "/playback/progress",
            self.store.decrypt(source["secret"]),
            {
                **row["target"],
                "event": "stop",
                "position_ms": position,
                "device_id": "yingji-resume-sync",
            },
        )

    def register(self):
        app = self.app

        @app.get("/api/watch/native-sync")
        async def native_sync_status(request: Request):
            owner = self.user_for(request)["id"]
            row = self.store.one(
                "SELECT username,origin FROM native_sync_grants WHERE user_id=?",
                (owner,),
            )
            bound = bool(row) and self.store.decrypt(row["origin"]) == self.api_origin
            return {
                "bound": bound,
                "account_name": row["username"] if bound else None,
                "available": self.settings.native,
            }

        @app.post("/api/watch/native-sync/pair")
        async def native_sync_pair(request: Request):
            user = self.user_for(request)
            expected = getattr(request.state, "friends_login_name", None)
            if not self.settings.native or not expected:
                raise HTTPException(409, "请从原生插件本人账号发起设备配对")
            old = self.pending.get(user["id"])
            if old and old["expires"] > time.time() and old["origin"] == self.api_origin:
                return old["view"]
            result = await self.request(
                "POST",
                "/auth/device/authorize",
                body={
                    "client_type": "cli",
                    "client_name": "映迹 · 本人观影同步",
                    "installation_id": "yingji:" + user["id"],
                    "client_version": "0.2.1",
                },
            )
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("user_code"), str)
                or not 1 <= len(result["user_code"]) <= 128
                or not isinstance(result.get("device_code"), str)
                or not 1 <= len(result["device_code"]) <= 2048
                or type(result.get("interval")) is not int
                or not 0 < result["interval"] <= 300
                or type(result.get("expires_in")) is not int
                or result["expires_in"] <= 0
            ):
                raise PeerError("MovieClaw 配对接口响应格式不兼容；请检查主程序版本和反向代理")
            view = {
                "user_code": result["user_code"],
                # 从当前浏览器已登录的同源页面打开，内网 API 地址不泄露给客户端。
                "verification_url": "/activate?" + urlencode({"code": result["user_code"]}),
                "interval": max(5, result["interval"]),
            }
            self.pending[user["id"]] = {
                "code": result["device_code"],
                "origin": self.api_origin,
                "expires": time.time() + min(result["expires_in"], 900),
                "next": 0,
                "expected": expected,
                "role": user["role"],
                "view": view,
            }
            return view

        @app.post("/api/watch/native-sync/pair/check")
        async def native_sync_pair_check(request: Request):
            user = self.user_for(request)
            async with self.locks.setdefault(user["id"], asyncio.Lock()):
                pending = self.pending.get(user["id"])
                if (
                    not pending
                    or pending["expires"] <= time.time()
                    or pending["origin"] != self.api_origin
                ):
                    raise HTTPException(409, "配对已过期，请重新发起")
                if pending["next"] > time.time():
                    return {"bound": False, "waiting": True}
                pending["next"] = time.time() + pending["view"]["interval"]
                result = await self.request(
                    "POST", "/auth/device/token", body={"device_code": pending["code"]}
                )
                if result is None:
                    return {"bound": False, "waiting": True}
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("token"), str)
                    or not result["token"]
                ):
                    raise PeerError("MovieClaw 配对令牌响应格式不兼容；请重新发起配对")
                self.pending.pop(user["id"], None)
                identity = await self.request("GET", "/auth/me", result["token"])
                if not isinstance(identity, dict):
                    raise PeerError("MovieClaw 本人身份响应格式不兼容；请重新发起配对")
                if (
                    identity.get("username") != pending["expected"]
                    or identity.get("role") != pending["role"]
                ):
                    # 错账号批准的令牌立即自撤销，不保留其他人的授权。
                    await self.request("DELETE", "/auth/devices/current", result["token"])
                    raise HTTPException(403, "批准账号与当前本人账号不一致，请用本人账号重新配对")
                self.store.execute(
                    (
                        "INSERT INTO "
                        "native_sync_grants(user_id,username,role,account,secret,origin) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                        "username=excluded.username,role=excluded.role,account=excluded.account,secret=excluded.secret,origin=excluded.origin,revision=native_sync_grants.revision+1"
                    ),
                    (
                        user["id"],
                        identity["username"],
                        identity["role"],
                        user["username"],
                        self.store.encrypt(result["token"]),
                        self.store.encrypt(self.api_origin),
                    ),
                )
                return {"bound": True, "account_name": identity["username"]}

        @app.delete("/api/watch/native-sync")
        async def native_sync_revoke(request: Request):
            owner = self.user_for(request)["id"]
            self.pending.pop(owner, None)
            row = self.store.one(
                "SELECT secret,origin FROM native_sync_grants WHERE user_id=?", (owner,)
            )
            self.store.execute("DELETE FROM native_sync_grants WHERE user_id=?", (owner,))
            # 地址变更后只撤销本地保管，绝不把旧设备凭据发送到新目标。
            if row and self.store.decrypt(row["origin"]) == self.api_origin:
                with suppress(PeerError):
                    await self.request(
                        "DELETE",
                        "/auth/devices/current",
                        self.store.decrypt(row["secret"]),
                    )
            return {"bound": False}
