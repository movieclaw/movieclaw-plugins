"""映迹 Web 与点对点协议：身份从宿主只读验证，社交授权由插件自己管理。"""

import base64
import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_serializer

from .auto_sync import register_auto_sync
from .community import register_community
from .config import Settings, origin_url
from .demo import DEMO_USERS, is_demo
from .demo import details as demo_details
from .demo import profile as demo_profile
from .demo import seed as seed_demo
from .demo import snapshot as demo_snapshot
from .display_policy import DisplayPolicy, register_display_policy
from .favorites import favorite_items, favorites_digest, register_favorites
from .feed import register_feed
from .lists import register_lists
from .media import ArtworkImage, safe_poster_path
from .network import register_network
from .peers import PeerClient, PeerError
from .series_footprints import register_series_footprints
from .social_federation import register_social_federation
from .store import Store, digest
from .task_center import register_task_center
from .timeline import merge_history
from .upstream import MovieClawClient, UpstreamError
from .version import __version__
from .watch_alignment import register_alignment
from .watch_archive import register_watch_archive, safe_avatar

COOKIE = "movieclaw_friends_session"
NOTICE = (
    "确认后将允许双方插件按各自的共享设置访问数据。跨服务器连接时，"
    "本方服务器的网络地址会提供给对方服务器，对方可能记录该地址及获准共享的数据。"
    "页面隐藏地址不能阻止网络层获取地址。撤销后停止后续访问，"
    "但无法保证对方删除已经保存或导出的副本。"
)


def now() -> str:
    return datetime.now(UTC).isoformat()


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class Profile(BaseModel):
    nickname: str = Field(min_length=1, max_length=60)
    discoverable: bool = False
    share_current: bool = False
    share_history: bool = False
    public_recent: bool = True
    share_favorites: bool = False
    share_posts: bool = False


class Publish(Profile):
    public_consent: bool = False
    preview_token: str = Field(default="", max_length=14000)


class FriendRequest(BaseModel):
    user_id: str | None = Field(default=None, max_length=64)
    invite_code: str | None = Field(default=None, max_length=4096)
    address_consent: bool = False


class Consent(BaseModel):
    address_consent: bool = False


class DemoPreference(BaseModel):
    """只修改当前账号的插件演示偏好，不接受账号编号或宿主设置。"""

    enabled: bool = Field(strict=True)


class IncomingRequest(BaseModel):
    invite_token: str = Field(min_length=32, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    origin: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=32, max_length=128)
    endpoints: dict[str, str] = Field(default_factory=dict, max_length=4)


class Media(BaseModel):
    """跨服数据白名单；对端即使发送宿主路径或凭据也不会入库、回传页面。"""

    media_item_id: int | None = None
    tmdb_id: int | None = None
    kind: str | None = Field(default=None, max_length=32)
    title: str = Field(default="未命名影片", max_length=300)
    year: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = Field(default=None, max_length=300)
    poster_path: str | None = Field(default=None, max_length=256)
    demo_id: str | None = Field(default=None, pattern=r"^[a-z-]{1,40}$")
    cover_path: str | None = Field(default=None, max_length=256)
    cover_type: Literal["backdrop", "poster"] | None = None
    cover_images: list[ArtworkImage] = Field(default_factory=list, max_length=9)

    @model_serializer(mode="wrap")
    def optional_cover(self, handler):
        # 仅明确选择的推荐封面进入共享资料；老记录不增加空字段，保持旧快照兼容。
        result = handler(self)
        if not self.cover_path or not self.cover_type:
            result.pop("cover_path", None)
            result.pop("cover_type", None)
        if not self.cover_images:
            result.pop("cover_images", None)
        else:
            result["cover_path"] = self.cover_images[0].path
            result["cover_type"] = self.cover_images[0].type
        return result

    @field_validator("poster_path", "cover_path", mode="before")
    @classmethod
    def safe_poster(cls, value):
        # 对端与浏览器都只能提供 TMDB 文件名，不能通过图片泄露服务器地址或凭据。
        return safe_poster_path(value)


class Watch(BaseModel):
    id: int | None = None
    media: Media
    started_at: str | None = Field(default=None, max_length=64)
    ended_at: str | None = Field(default=None, max_length=64)
    watched_ms: int | None = None
    start_position_ms: int | None = None
    end_position_ms: int | None = None
    duration_ms: int | None = None
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    completed: bool = False
    expires_at: datetime | None = None
    playback_rate: float = Field(default=1, gt=0, le=100)


class Collection(BaseModel):
    id: int
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=500)
    items: list[Media] = Field(default_factory=list, max_length=500)
    cover: Media | None = None

    @model_serializer(mode="wrap")
    def optional_cover(self, handler):
        result = handler(self)
        if self.cover is None:
            result.pop("cover", None)
        return result


class Sharing(BaseModel):
    current: bool = False
    history: bool = False


class Presence(BaseModel):
    """在线展示只传布尔状态和最近在线时间，不接受设备、地址等额外字段。"""

    online: bool = False
    last_seen: datetime | None = None


class DemoRecommendation(BaseModel):
    """演示推荐的最小展示字段，不接收作者凭据、宿主条目或互动数据库信息。"""

    id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=2000)
    media: Media
    created_at: datetime


class SharedData(BaseModel):
    current: Watch | None = None
    history: list[Watch] = Field(default_factory=list, max_length=50)
    collections: list[Collection] = Field(default_factory=list, max_length=50)
    sharing: Sharing = Field(default_factory=Sharing)
    observed_at: datetime
    presence: Presence | None = None
    favorites: list[Media] = Field(default_factory=list, max_length=500)
    avatar_id: Literal["xiaoying", "achuan", "xiaolu", "xingzi"] | None = None
    bio: str = Field(default="", max_length=300)
    avatar_data: str | None = Field(default=None, max_length=90000)
    recommendations: list[DemoRecommendation] = Field(default_factory=list, max_length=8)


def create_app(
    settings: Settings,
    *,
    upstream=None,
    peer_transport=None,
    peer_transport_factory=None,
    task_factory=None,
    external_watch=None,
) -> FastAPI:
    """组装一个完全独立的插件实例，测试可注入只读宿主与对端网络。

    本函数不会导入宿主模型、执行其迁移或写入其数据目录。每次输出好友数据
    都重新检查当前关系与共享开关，避免撤回授权时仍有在途请求发布旧数据。
    """

    store = Store(settings.data_dir)
    policy = DisplayPolicy(store)
    store.display_policy = policy
    if settings.demo_enabled:
        seed_demo(store)
    source = upstream or MovieClawClient(settings.upstream_url, settings.admin_token)
    peers = PeerClient(
        settings.allow_private_peers,
        transport=peer_transport,
        transport_factory=peer_transport_factory,
    )
    if hasattr(source, "__dict__"):
        source.display_policy = policy
    public_url = origin_url(settings.public_url)

    @asynccontextmanager
    async def lifespan(app):
        app.state.auto_sync.start()
        app.state.resume_sync.start()
        app.state.network.start()
        yield
        await app.state.network.close()
        await app.state.auto_sync.close()
        await app.state.resume_sync.close()
        await app.state.watch_archive.close()
        store.close()

    app = FastAPI(
        title="MovieClaw · 映迹",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.settings = settings
    app.state.upstream = source

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(PeerError)
    async def peer_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValidationError)
    async def invalid_snapshot(request, exc):
        return JSONResponse(
            {"detail": "读取的观影数据格式不兼容或超出插件容量限制"}, status_code=502
        )

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.url.path.startswith("/api/"):
                origin = request.headers.get("origin")
                if request.headers.get("x-friends-request") != "1" or (
                    origin and origin != public_url
                ):
                    return JSONResponse({"detail": "请从插件页面提交操作"}, status_code=403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                limit = (
                    1_000_000
                    if request.url.path == "/federation/directory"
                    else 100000
                    if request.url.path == "/api/watch/avatar"
                    else 24000
                )
                if len(body) > limit:
                    return JSONResponse({"detail": "请求内容过大"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    def user_for(request: Request) -> dict:
        if settings.native:
            user = getattr(request.state, "friends_user", None)
            if not user:
                raise HTTPException(401, "请先登录 MovieClaw")
            return user
        token = request.cookies.get(COOKIE, "")
        user = store.one(
            "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id "
            "WHERE token_hash=? AND expires_at>?",
            (digest(token), time.time()),
        )
        if not user:
            raise HTTPException(401, "请先使用 MovieClaw 令牌登录插件")
        return user

    async def verified_user(request: Request) -> dict:
        """读取他人数据前重验宿主身份，已注销的凭据不能靠旧插件会话继续访问。"""
        user = user_for(request)
        if settings.native:
            return user
        identity = await source.identity(store.decrypt(user["token"]))
        if (identity["username"], identity["role"], identity["member_id"]) != (
            user["username"],
            user["role"],
            user["member_id"],
        ):
            raise HTTPException(401, "MovieClaw 账号身份已经变化，请重新登录")
        return user

    register_lists(app, store, user_for, Media, Collection)
    register_favorites(app, store, user_for, Media)
    community = register_community(app, store, user_for, server_avatars=settings.native)
    app.state.community = community
    register_watch_archive(
        app, store, user_for, source, settings, task_factory=task_factory, external=external_watch
    )
    register_display_policy(app, store, user_for, app.state.watch_archive, policy)
    register_series_footprints(app, store, user_for, app.state.watch_archive)
    register_alignment(app, store, user_for, app.state.watch_archive, Media)
    register_task_center(app, store, user_for, app.state.watch_archive, app.state.alignment)
    from .native_sync import NativeSync

    app.state.native_sync = NativeSync(
        app,
        app.state.watch_archive,
        settings,
        user_for,
        transport_factory=peer_transport_factory,
        transport=peer_transport,
    )
    app.state.native_sync.register()
    register_auto_sync(app, store, user_for, app.state.watch_archive)
    from .resume_sync import ResumeSync

    app.state.resume_sync = ResumeSync(app.state.auto_sync, user_for)
    app.state.resume_sync.register(app)
    feed = register_feed(app, store, user_for, Media, community=community, source=source)
    register_network(app, store, settings, peers, community, user_for, source, task_factory)

    def invitation(user: dict) -> str:
        payload = json.dumps(
            {
                "origin": public_url,
                "invite": user["invite_token"],
                "endpoints": settings.federation_endpoints,
            }
        )
        return "mcf1." + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    def own_profile(user: dict) -> dict:
        person = community.public_profile(user["id"], user["id"])
        return {key: user[key] for key in ("id", "username", "nickname")} | {
            "discoverable": bool(user["discoverable"]),
            "share_current": bool(user["share_current"]),
            "share_history": bool(user["share_history"]),
            "public_recent": bool(user["public_recent"]),
            "share_favorites": bool(user["share_favorites"]),
            "share_posts": bool(user["share_posts"]),
            "role": user["role"],
            "avatar_id": person["avatar_id"],
            **({"avatar_url": person["avatar_url"]} if person.get("avatar_url") else {}),
            "bio": person["bio"],
            "invite_code": invitation(user),
            "is_demo": is_demo(user),
        }

    def person_fields(user: dict, viewer_id: str) -> dict:
        person = (
            demo_profile(user["id"])
            if is_demo(user)
            else community.public_profile(user["id"], viewer_id)
        )
        return {
            "avatar_id": person.get("avatar_id", "initials"),
            "bio": person.get("bio", ""),
            **({"avatar_url": person["avatar_url"]} if person.get("avatar_url") else {}),
        }

    def allowed_relation(row: dict, viewer_id: str) -> bool:
        other = (
            (row["target_id"] if row["user_id"] == viewer_id else row["user_id"])
            if row["direction"] == "local"
            else community.remote_actor(row["id"])
        )
        return community.can_interact(viewer_id, other)

    def blocked_peer(user_id: str, origin: str, peer_user_id: str) -> bool:
        # 已屏蔽的远端用户不能通过撤销旧关系、换一条新关系编号绕过屏蔽。
        return any(
            not community.can_interact(user_id, community.remote_actor(row["id"]))
            for row in store.all(
                "SELECT id FROM relationships WHERE user_id=? AND peer_origin=? AND peer_user_id=?",
                (user_id, origin, peer_user_id),
            )
        )

    def demo_enabled_for(user_id: str) -> bool:
        """读取最新本人偏好；管理员总开关关闭时，任何账号都不能重新打开演示。"""
        user = store.one("SELECT demo_enabled FROM users WHERE id=?", (user_id,))
        return bool(settings.demo_enabled and user and user["demo_enabled"])

    def demo_response(user_id: str) -> dict:
        user = store.one("SELECT demo_enabled FROM users WHERE id=?", (user_id,))
        preference = bool(user["demo_enabled"])
        result = {
            "enabled": bool(settings.demo_enabled and preference),
            "available": settings.demo_enabled,
            "preference": preference,
            "user": None,
            "data": None,
            "users": [],
            "details": {},
        }
        if not result["enabled"]:
            return result
        observed = datetime.now(UTC)
        users = [
            {
                "user": demo_profile(u["id"]),
                "data": SharedData.model_validate(
                    demo_snapshot(u["id"], observed=observed)
                ).model_dump(mode="json")
                | {"is_demo": True},
            }
            for u in DEMO_USERS
        ]
        return result | users[0] | {"users": users, "details": demo_details()}

    def relation_has_demo(row: dict, viewer_id: str) -> bool:
        if row["direction"] != "local":
            return False
        other_id = row["target_id"] if row["user_id"] == viewer_id else row["user_id"]
        target = store.one("SELECT role FROM users WHERE id=?", (other_id,))
        return bool(target and is_demo(target))

    def relation_for(relation_id: str, user_id: str, *, allow_blocked: bool = False) -> dict:
        row = store.one("SELECT * FROM relationships WHERE id=?", (relation_id,))
        if not row or user_id not in {row["user_id"], row["target_id"]}:
            raise HTTPException(404, "好友关系不存在")
        if relation_has_demo(row, user_id) and not demo_enabled_for(user_id):
            raise HTTPException(404, "好友关系不存在")
        if not allow_blocked and not allowed_relation(row, user_id):
            raise HTTPException(404, "好友关系当前不可访问")
        return row

    def peer_relation(request: Request) -> dict:
        header = request.headers.get("authorization", "")
        token = request.headers.get("x-watch-friends-token", "") or (
            header[7:] if header.startswith("Bearer ") else ""
        )
        row = store.one("SELECT * FROM relationships WHERE token_hash=?", (digest(token),))
        if not row:
            raise HTTPException(401, "好友连接凭据无效")
        return row

    def display_relation(row: dict, viewer_id: str) -> dict:
        if row["direction"] == "local":
            target_id = row["target_id"] if row["user_id"] == viewer_id else row["user_id"]
            target = store.one("SELECT nickname,role FROM users WHERE id=?", (target_id,))
            name = demo_profile(target_id)["nickname"] if is_demo(target) else target["nickname"]
            remote = False
            virtual = is_demo(target)
        else:
            name, remote = row["peer_name"], True
            target_id = None
            virtual = False
        person = (
            demo_profile(target_id)
            if virtual
            else community.public_profile(
                target_id if not remote else community.remote_actor(row["id"]), viewer_id
            )
        )
        return {
            "id": row["id"],
            "user_id": target_id,
            "nickname": name,
            "is_remote": remote,
            "is_demo": virtual,
            "status": row["status"],
            "avatar_id": person.get("avatar_id", "initials"),
            "bio": person.get("bio", ""),
            "profile_id": target_id if not remote else community.remote_actor(row["id"]),
            **({"avatar_url": person["avatar_url"]} if person.get("avatar_url") else {}),
        }

    async def peer_request(row: dict, path: str, *, data=None):
        endpoints = (
            json.loads(store.decrypt(row["peer_endpoints"])) if row.get("peer_endpoints") else {}
        )
        return await peers.request(
            row["peer_origin"],
            path,
            token=store.decrypt(row["token"]),
            data=data,
            endpoint=endpoints.get(path),
            require_signed=settings.native,
        )

    async def refresh_remote(row: dict) -> dict:
        if row["direction"] == "local" or row["status"] not in {"pending", "accepted"}:
            return row
        result = await peer_request(row, "/federation/state")
        if settings.native and result.get("endpoints"):
            # 已建立的旧好友升级后可交换新增社交能力，不必删除关系重新添加。
            peers.validate_endpoints(row["peer_origin"], result["endpoints"])
            store.execute(
                "UPDATE relationships SET peer_endpoints=? WHERE id=?",
                (store.encrypt(json.dumps(result["endpoints"])), row["id"]),
            )
        status = result.get("status")
        if status in {"rejected", "revoked"}:
            store.execute(
                "UPDATE relationships SET status=?, cache=NULL WHERE id=? "
                "AND status IN ('pending','accepted')",
                (status, row["id"]),
            )
        elif row["direction"] == "outgoing" and status == "accepted":
            store.execute(
                "UPDATE relationships SET status='accepted' WHERE id=? AND status='pending'",
                (row["id"],),
            )
        return store.one("SELECT * FROM relationships WHERE id=?", (row["id"],))

    async def shared_snapshot(user_id: str) -> dict:
        user = store.one("SELECT * FROM users WHERE id=?", (user_id,))
        if is_demo(user):
            if not settings.demo_enabled:
                raise HTTPException(404, "虚拟演示用户已关闭")
            result = SharedData.model_validate(demo_snapshot(user_id)).model_dump(mode="json")
            return result | {"is_demo": True}
        snapshot = await source.snapshot(
            store.decrypt(user["token"]),
            user["member_id"],
            bool(user["share_current"]),
            bool(user["share_history"]),
        )
        if user.get("share_imported_history") and user["share_history"]:
            snapshot["history"] = merge_history(
                snapshot.get("history", []), app.state.watch_archive.shareable_history(user_id)
            )[:50]
        # 在读取宿主期间用户可能关闭开关；再次取最新授权，旧请求不能重新发布。
        latest = store.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not latest["share_current"]:
            snapshot["current"] = None
        if not latest["share_history"]:
            snapshot["history"] = []
        snapshot["sharing"] = {
            "current": bool(latest["share_current"]),
            "history": bool(latest["share_history"]),
        }
        snapshot["collections"] = [
            json.loads(row["data"])
            for row in store.all(
                "SELECT data FROM collections WHERE user_id=? ORDER BY source_id", (user_id,)
            )
        ]
        person = community.public_profile(user_id, user_id)
        snapshot["avatar_id"] = person["avatar_id"] if person["avatar_id"] != "initials" else None
        snapshot["bio"] = person["bio"]
        thumbnail = store.one(
            "SELECT avatar_data FROM account_profiles WHERE user_id=?", (user_id,)
        )
        if thumbnail and safe_avatar(thumbnail["avatar_data"]):
            snapshot["avatar_data"] = thumbnail["avatar_data"]
        if snapshot.get("current"):
            snapshot["current"]["expires_at"] = (
                datetime.now(UTC) + timedelta(seconds=90)
            ).isoformat()
        return SharedData.model_validate(policy.snapshot(user_id, snapshot)).model_dump(mode="json")

    register_social_federation(
        app,
        store,
        verified_user,
        relation_for,
        peer_relation,
        refresh_remote,
        peer_request,
        feed,
        community=community,
    )

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "plugin": "movieclaw-watch-friends",
            "protocol": 1,
            "version": __version__,
            "demo_enabled": settings.demo_enabled,
        }

    @app.get("/api/info")
    async def info():
        return {
            "name": "映迹",
            "protocol": 1,
            "version": __version__,
            "privacy_notice": NOTICE,
            "demo_enabled": settings.demo_enabled,
        }

    @app.get("/api/demo")
    async def demo_preview(request: Request):
        """演示只读且不创建好友关系；从未读过本人的真实观影资料也可体验完整界面。"""
        user = await verified_user(request)
        return demo_response(user["id"])

    @app.patch("/api/demo/preferences")
    async def demo_preferences(data: DemoPreference, request: Request):
        # 关闭本人演示无需等待宿主网络；更新一列，不影响真实好友、片单或共享版本。
        user = user_for(request)
        store.execute("UPDATE users SET demo_enabled=? WHERE id=?", (data.enabled, user["id"]))
        return demo_response(user["id"])

    @app.post("/api/login")
    async def login(data: Login):
        identity = await source.identity(data.token)
        user = store.one("SELECT * FROM users WHERE username=?", (identity["username"],))
        if user:
            if user["role"] != identity["role"] or (
                user["member_id"] is not None and user["member_id"] != identity["member_id"]
            ):
                raise HTTPException(
                    409, "同名 MovieClaw 成员的身份已变化，请联系插件管理员重置旧绑定"
                )
            # 一个账号只保留一份宿主读取凭据；换凭据时旧设备必须重新登录，
            # 否则旧插件 Cookie 会借用新设备的有效凭据绕过原凭据撤销。
            if digest(store.decrypt(user["token"])) != digest(data.token):
                store.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            store.execute(
                "UPDATE users SET token=?,member_id=?,role=? WHERE id=?",
                (store.encrypt(data.token), identity["member_id"], identity["role"], user["id"]),
            )
        else:
            user_id = str(uuid.uuid4())
            store.execute(
                "INSERT INTO users(id,username,nickname,member_id,role,token,invite_token) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    identity["username"],
                    identity["nickname"],
                    identity["member_id"],
                    identity["role"],
                    store.encrypt(data.token),
                    store.secret(),
                ),
            )
        user = store.one("SELECT * FROM users WHERE username=?", (identity["username"],))
        store.plugin_joined_at(user["id"])
        token = store.secret()
        store.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        store.execute(
            "INSERT INTO sessions VALUES(?,?,?)", (digest(token), user["id"], time.time() + 43200)
        )
        response = JSONResponse({"user": own_profile(user)})
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
            max_age=43200,
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        store.execute(
            "DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get(COOKIE, "")),)
        )
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": own_profile(await verified_user(request)), "privacy_notice": NOTICE}

    @app.post("/api/profile/preview")
    async def preview_profile(data: Profile, request: Request):
        user = user_for(request)
        nickname = data.nickname.strip()
        if not nickname:
            raise HTTPException(422, "请填写显示姓名")
        data.nickname = nickname
        public_recent = data.discoverable and data.public_recent
        public_favorites = (
            favorite_items(store, user["id"]) if data.discoverable and data.share_favorites else []
        )
        # 只公开姓名或喜欢的电影时，不要求读取观影历史，也不借管理员身份扩展授权。
        snapshot = (
            await source.snapshot(
                store.decrypt(user["token"]),
                user["member_id"],
                data.share_current,
                data.share_history or public_recent,
            )
            if data.share_current or data.share_history or public_recent
            else {
                "current": None,
                "history": [],
                "sharing": {"current": False, "history": False},
                "observed_at": now(),
            }
        )
        imported = (
            app.state.watch_archive.shareable_history(user["id"])
            if data.share_history or public_recent
            else []
        )
        if imported:
            snapshot["history"] = merge_history(snapshot.get("history", []), imported)[:50]
        snapshot = policy.snapshot(user["id"], snapshot)
        public_favorites = policy.filter(user["id"], public_favorites)
        public_history = (
            [Watch.model_validate(item).model_dump(mode="json") for item in snapshot["history"][:5]]
            if public_recent
            else []
        )
        snapshot["history"] = snapshot["history"] if data.share_history else []
        snapshot["sharing"] = {"current": data.share_current, "history": data.share_history}
        snapshot["collections"] = [
            json.loads(row["data"])
            for row in store.all("SELECT data FROM collections WHERE user_id=?", (user["id"],))
        ]
        latest = store.one("SELECT privacy_revision FROM users WHERE id=?", (user["id"],))
        if latest["privacy_revision"] != user["privacy_revision"]:
            raise HTTPException(409, "设置或喜欢的电影已经变化，请重新预览")
        preview_token = store.encrypt(
            json.dumps(
                {
                    "user_id": user["id"],
                    "profile": data.model_dump(),
                    "history": public_history,
                    "favorites_digest": favorites_digest(public_favorites),
                    "archive_included": bool(imported),
                    "revision": user["privacy_revision"],
                },
                ensure_ascii=False,
            )
        )
        return {
            "public_profile": {
                "nickname": nickname,
                "history": public_history,
                "favorites": public_favorites,
            }
            if data.discoverable
            else None,
            "preview_token": preview_token,
            "friend_data": SharedData.model_validate(
                policy.snapshot(user["id"], snapshot)
            ).model_dump(mode="json"),
            "social_sharing": data.share_posts,
            "archive_included": bool(imported),
        }

    @app.patch("/api/profile")
    async def update_profile(data: Publish, request: Request):
        user = user_for(request)
        data.nickname = data.nickname.strip()
        if not data.nickname:
            raise HTTPException(422, "请填写显示姓名")
        history = []
        favorites = []
        preview = None
        if data.discoverable or data.preview_token:
            if not data.public_consent:
                raise HTTPException(400, "请先预览并确认要公开的姓名、观影记录或喜欢的电影")
            try:
                preview = json.loads(store.cipher.decrypt(data.preview_token.encode(), ttl=300))
            except (InvalidToken, ValueError) as exc:
                raise HTTPException(400, "公开预览已失效，请重新预览后确认") from exc
            profile = Profile(**data.model_dump()).model_dump()
            if (
                preview.get("user_id") != user["id"]
                or preview.get("profile") != profile
                or preview.get("revision") != user["privacy_revision"]
            ):
                raise HTTPException(400, "设置已经变化，请重新预览公开内容")
            history = preview["history"] if data.discoverable else []
            favorites = (
                policy.filter(user["id"], favorite_items(store, user["id"]))
                if data.discoverable and data.share_favorites
                else []
            )
            if preview.get("favorites_digest") != favorites_digest(favorites):
                raise HTTPException(400, "喜欢的电影已经变化，请重新预览公开内容")
        with store.lock:
            latest = store.one("SELECT privacy_revision FROM users WHERE id=?", (user["id"],))
            if latest["privacy_revision"] != user["privacy_revision"]:
                raise HTTPException(409, "设置或喜欢的电影已经变化，请重新确认")
            store.execute(
                "UPDATE users SET nickname=?,discoverable=?,share_current=?,share_history=?,"
                "public_recent=?,share_favorites=?,share_posts=?,public_history=?,public_favorites=?,"
                "public_updated_at=?,share_imported_history=?,"
                "privacy_revision=privacy_revision+1 WHERE id=?",
                (
                    data.nickname,
                    data.discoverable,
                    data.share_current,
                    data.share_history,
                    data.public_recent,
                    data.share_favorites,
                    data.share_posts,
                    json.dumps(history if data.public_recent else [], ensure_ascii=False),
                    json.dumps(favorites, ensure_ascii=False),
                    now() if data.discoverable else None,
                    bool(preview.get("archive_included"))
                    if preview is not None
                    else bool(user.get("share_imported_history")),
                    user["id"],
                ),
            )
            store.execute(
                "UPDATE relationships SET cache=NULL WHERE user_id=? OR target_id=?",
                (user["id"], user["id"]),
            )
        return {"user": own_profile(store.one("SELECT * FROM users WHERE id=?", (user["id"],)))}

    @app.get("/api/directory")
    async def directory(request: Request):
        viewer = await verified_user(request)
        network = app.state.network
        if network.enabled():
            await network.verify_publishers()
        network_cards = (
            {e["payload"]["actor"]: e["payload"]["card"] for e in network.local_envelopes()}
            if network.enabled()
            else {}
        )
        users = store.all(
            "SELECT id,nickname,role,discoverable,public_history,public_recent,share_favorites,"
            "public_favorites,public_updated_at FROM users "
            "WHERE id<>? AND (discoverable=1 OR id IN "
            "(SELECT user_id FROM network_preferences WHERE snapshot IS NOT NULL)) "
            "ORDER BY nickname",
            (viewer["id"],),
        )
        return {
            "users": [
                {
                    "id": u["id"],
                    "nickname": demo_profile(u["id"])["nickname"] if is_demo(u) else u["nickname"],
                    "history": demo_snapshot(u["id"])["history"]
                    if is_demo(u)
                    else policy.filter(u["id"], json.loads(u["public_history"]))
                    if u["public_recent"]
                    else [],
                    "favorites": demo_snapshot(u["id"]).get("favorites", [])
                    if is_demo(u)
                    else policy.filter(u["id"], json.loads(u["public_favorites"]))
                    if u["share_favorites"]
                    else [],
                    **person_fields(u, viewer["id"]),
                    "updated_at": u["public_updated_at"],
                    "is_demo": is_demo(u),
                    **(
                        {**network_cards[u["id"]], "network_public": True}
                        if u["id"] in network_cards
                        else {}
                    ),
                }
                for u in users
                if (u["id"] in network_cards or u["discoverable"])
                and (not is_demo(u) or demo_enabled_for(viewer["id"]))
                and community.can_interact(viewer["id"], u["id"])
            ]
        }

    @app.get("/api/preview")
    async def preview(request: Request):
        return await shared_snapshot(user_for(request)["id"])

    @app.get("/api/collections")
    async def collections(request: Request):
        user = user_for(request)
        saved = store.all(
            "SELECT * FROM collections WHERE user_id=? AND source_id>0", (user["id"],)
        )
        source_error = None
        try:
            rows = [] if settings.native else await source.collections(store.decrypt(user["token"]))
        except UpstreamError:
            if not saved:
                raise
            rows = []
            source_error = "暂时无法读取源片单，仍可停止分享已保存的快照"
        shared = {r["source_id"] for r in saved}
        result = [dict(row, shared=row["id"] in shared, source_available=True) for row in rows]
        visible = {row["id"] for row in rows}
        # 原片单删除、隐藏或宿主暂时不可用时，仍保留撤销既有快照的入口。
        result.extend(
            {
                "id": row["source_id"],
                "name": row["name"],
                "item_count": len(json.loads(row["data"])["items"]),
                "shared": True,
                "source_available": False,
            }
            for row in saved
            if row["source_id"] not in visible
        )
        return {
            "collections": result,
            "source_error": source_error,
            "browser_required": settings.native,
        }

    @app.post("/api/collections/{collection_id}/share")
    async def share_collection(collection_id: int, request: Request):
        user = user_for(request)
        if collection_id <= 0:
            raise HTTPException(400, "宿主片单编号必须为正数，插件自建片单请使用对应的分享入口")
        count = store.one(
            "SELECT COUNT(*) AS total FROM collections WHERE user_id=?", (user["id"],)
        )
        if count["total"] >= 50 and not store.one(
            "SELECT source_id FROM collections WHERE user_id=? AND source_id=?",
            (user["id"], collection_id),
        ):
            raise HTTPException(400, "第一版最多同时分享五十个片单，请先取消部分分享")
        if settings.native:
            # 片单由浏览器使用宿主当前登录身份读取后主动发布。服务端只接收本人的
            # 白名单快照，不接受成员ID，也不借插件管理员权限读取成员私有片单。
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(422, "请提交本人主动分享的片单快照")
            item = Collection.model_validate(payload.get("collection")).model_dump(mode="json")
            if item["id"] != collection_id:
                raise HTTPException(422, "片单标识与请求不一致")
        else:
            item = Collection.model_validate(
                await source.collection(store.decrypt(user["token"]), collection_id)
            ).model_dump(mode="json")
        latest = store.one("SELECT privacy_revision FROM users WHERE id=?", (user["id"],))
        if latest["privacy_revision"] != user["privacy_revision"]:
            raise HTTPException(409, "共享设置已经变化，请重新确认片单分享")
        store.execute(
            "INSERT INTO collections VALUES(?,?,?,?) ON CONFLICT(user_id,source_id) "
            "DO UPDATE SET name=excluded.name,data=excluded.data",
            (user["id"], collection_id, item["name"], json.dumps(item, ensure_ascii=False)),
        )
        return {"ok": True}

    @app.delete("/api/collections/{collection_id}/share")
    async def unshare_collection(collection_id: int, request: Request):
        user = user_for(request)
        store.execute(
            "DELETE FROM collections WHERE user_id=? AND source_id=?", (user["id"], collection_id)
        )
        store.execute(
            "UPDATE users SET privacy_revision=privacy_revision+1 WHERE id=?", (user["id"],)
        )
        store.execute(
            "UPDATE relationships SET cache=NULL WHERE user_id=? OR target_id=?",
            (user["id"], user["id"]),
        )
        return {"ok": True}

    @app.post("/api/requests")
    async def send_request(data: FriendRequest, request: Request):
        return await create_friend_request(data, request, peers)

    async def create_friend_request(data, request, friend_peers):
        user = await verified_user(request)
        if not data.address_consent:
            raise HTTPException(400, "请先阅读服务器地址及数据共享提示并明确同意")
        if bool(data.user_id) == bool(data.invite_code):
            raise HTTPException(422, "请选择一位广场用户，或填写一个好友邀请码")
        target = None
        origin, invite = None, None
        endpoints = {}
        if data.invite_code:
            try:
                prefix, encoded = data.invite_code.strip().split(".", 1)
                if prefix != "mcf1":
                    raise ValueError
                decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                origin = origin_url(decoded["origin"])
                invite = decoded["invite"]
                endpoints = decoded.get("endpoints", {})
                if not isinstance(endpoints, dict):
                    raise ValueError
                if not isinstance(invite, str) or not 32 <= len(invite) <= 128:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as exc:
                raise HTTPException(400, "邀请码格式无效") from exc
            if origin == public_url:
                target = store.one("SELECT * FROM users WHERE invite_token=?", (invite,))
                if not target:
                    raise HTTPException(404, "邀请码已经失效")
            else:
                origin = friend_peers.validate(origin)
                if settings.native and not settings.federation_endpoints:
                    raise HTTPException(409, "请由管理员先在 MovieClaw 设置中配置外部访问地址")
                if settings.native:
                    friend_peers.validate_endpoints(origin, endpoints)
        else:
            target = store.one("SELECT * FROM users WHERE id=?", (data.user_id,))
            if target and not target["discoverable"]:
                await app.state.network.verify_publishers()
                if not app.state.network.local_card(target["id"]):
                    target = None
            if not target:
                raise HTTPException(404, "该用户没有开放广场展示，请使用对方提供的邀请码")
        relation_id = str(uuid.uuid4())
        status = "pending"
        if target:
            if is_demo(target) and not demo_enabled_for(user["id"]):
                raise HTTPException(404, "虚拟演示用户已关闭")
            if target["id"] == user["id"]:
                raise HTTPException(400, "不能添加自己为好友")
            if not community.can_interact(user["id"], target["id"]):
                raise HTTPException(404, "当前无法向该用户申请好友")
            existing = store.one(
                "SELECT id FROM relationships WHERE direction='local' "
                "AND ((user_id=? AND target_id=?) OR (user_id=? AND target_id=?)) "
                "AND status IN ('pending','accepted')",
                (user["id"], target["id"], target["id"], user["id"]),
            )
            if existing:
                raise HTTPException(409, "已经是好友，或已有待处理的申请")
            status = "accepted" if is_demo(target) else "pending"
            store.execute(
                "INSERT INTO relationships(id,user_id,target_id,direction,status,created_at) "
                "VALUES(?,?,?,'local',?,?)",
                (relation_id, user["id"], target["id"], status, time.time()),
            )
            if not is_demo(target):
                community.notify(
                    target["id"],
                    user["id"],
                    "friend_request",
                    relation_id,
                    key=f"friend_request:{relation_id}",
                )
        else:
            token = store.secret()
            store.execute(
                "INSERT INTO relationships(id,user_id,peer_origin,peer_name,direction,status,"
                "token,token_hash,created_at) VALUES(?,?,?,?,'outgoing','pending',?,?,?)",
                (
                    relation_id,
                    user["id"],
                    origin,
                    "等待对方确认",
                    store.encrypt(token),
                    digest(token),
                    time.time(),
                ),
            )
            if endpoints:
                store.execute(
                    "UPDATE relationships SET peer_endpoints=? WHERE id=?",
                    (store.encrypt(json.dumps(endpoints)), relation_id),
                )
            try:
                result = await friend_peers.request(
                    origin,
                    "/federation/requests",
                    endpoint=endpoints.get("/federation/requests"),
                    require_signed=settings.native,
                    data={
                        "invite_token": invite,
                        "request_id": relation_id,
                        "origin": public_url,
                        "user_id": user["id"],
                        "token": token,
                        "endpoints": {
                            path: url
                            for path, url in settings.federation_endpoints.items()
                            if path != "/federation/social" or "/federation/social" in endpoints
                        },
                    },
                )
                peer_id, name = result["user_id"], result["nickname"]
                if not isinstance(peer_id, str) or not isinstance(name, str) or len(name) > 60:
                    raise ValueError
                if blocked_peer(user["id"], origin, peer_id):
                    raise ValueError("该远端用户已被屏蔽")
                if settings.native:
                    friend_peers.validate_endpoints(origin, result.get("endpoints", {}))
                    store.execute(
                        "UPDATE relationships SET peer_endpoints=? WHERE id=?",
                        (store.encrypt(json.dumps(result["endpoints"])), relation_id),
                    )
            except (PeerError, KeyError, ValueError) as exc:
                store.execute(
                    "UPDATE relationships SET status='revoked' WHERE id=?", (relation_id,)
                )
                raise PeerError("申请未能确认送达；请稍后重新尝试") from exc
            store.execute(
                "UPDATE relationships SET peer_user_id=?,peer_name=? WHERE id=?",
                (peer_id, name, relation_id),
            )
        return {
            "id": relation_id,
            "status": status,
            "is_demo": bool(target and is_demo(target)),
        }

    from .network_interactions import NetworkInteractions

    async def network_friend(invite_code, request, public=False):
        return await create_friend_request(
            FriendRequest(invite_code=invite_code, address_consent=True),
            request,
            app.state.network.discovery.peer if public else peers,
        )

    interactions = NetworkInteractions(app.state.network, invitation, network_friend)
    interactions.register(app)
    app.state.network_interactions = interactions

    @app.get("/api/requests")
    async def requests(request: Request):
        user = await verified_user(request)
        rows = store.all(
            "SELECT * FROM relationships WHERE (user_id=? OR target_id=?) AND status='pending'",
            (user["id"], user["id"]),
        )
        result = {"incoming": [], "outgoing": []}
        for row in rows:
            if not allowed_relation(row, user["id"]):
                continue
            if row["direction"] == "outgoing":
                with suppress(PeerError):
                    row = await refresh_remote(row)
            if row["status"] != "pending" or not allowed_relation(row, user["id"]):
                continue
            incoming = row["direction"] == "incoming" or row["target_id"] == user["id"]
            result["incoming" if incoming else "outgoing"].append(display_relation(row, user["id"]))
        if not demo_enabled_for(user["id"]):
            result = {
                key: [item for item in rows if not item["is_demo"]] for key, rows in result.items()
            }
        return result

    @app.post("/api/requests/{relation_id}/accept")
    async def accept(relation_id: str, data: Consent, request: Request):
        user = await verified_user(request)
        row = relation_for(relation_id, user["id"])
        if not data.address_consent:
            raise HTTPException(400, "请先阅读服务器地址及数据共享提示并明确同意")
        if row["status"] != "pending" or (
            row["direction"] != "incoming" and row["target_id"] != user["id"]
        ):
            raise HTTPException(409, "只有收到申请的一方可以接受待处理申请")
        store.execute("UPDATE relationships SET status='accepted' WHERE id=?", (relation_id,))
        return {"ok": True}

    @app.post("/api/requests/{relation_id}/reject")
    async def reject(relation_id: str, request: Request):
        user = user_for(request)
        row = relation_for(relation_id, user["id"], allow_blocked=True)
        if row["status"] != "pending" or (
            row["direction"] != "incoming" and row["target_id"] != user["id"]
        ):
            raise HTTPException(409, "该申请不能被当前用户拒绝")
        store.execute(
            "UPDATE relationships SET status='rejected',cache=NULL WHERE id=?", (relation_id,)
        )
        return {"ok": True}

    @app.get("/api/friends")
    async def friends(request: Request):
        user = await verified_user(request)
        rows = store.all(
            "SELECT * FROM relationships WHERE (user_id=? OR target_id=?) "
            "AND status IN ('pending','accepted') ORDER BY created_at DESC",
            (user["id"], user["id"]),
        )
        result = []
        for row in rows:
            if not allowed_relation(row, user["id"]):
                continue
            if row["direction"] != "local":
                with suppress(PeerError):
                    row = await refresh_remote(row)
            if row["status"] == "accepted" and allowed_relation(row, user["id"]):
                item = display_relation(row, user["id"])
                result.append(item)
        if not demo_enabled_for(user["id"]):
            result = [item for item in result if not item["is_demo"]]
        return {"friends": result}

    @app.delete("/api/friends/{relation_id}")
    async def revoke(relation_id: str, request: Request):
        user = user_for(request)
        relation_for(relation_id, user["id"], allow_blocked=True)
        store.execute(
            "UPDATE relationships SET status='revoked',cache=NULL WHERE id=?", (relation_id,)
        )
        return {"ok": True}

    @app.get("/api/friends/{relation_id}")
    async def friend_data(relation_id: str, request: Request):
        user = await verified_user(request)
        row = relation_for(relation_id, user["id"])
        if row["direction"] != "local":
            row = await refresh_remote(row)
        if row["status"] != "accepted":
            raise HTTPException(403, "双方同意后才能访问好友数据，或该关系已撤销")
        if row["direction"] == "local":
            target_id = row["target_id"] if row["user_id"] == user["id"] else row["user_id"]
            data = await shared_snapshot(target_id)
        else:
            result = await peer_request(row, "/federation/data")
            # 数据返回前，对端可能已经撤销关系或屏蔽；再次确认授权后才接收旧快照。
            row = await refresh_remote(row)
            if row["status"] != "accepted":
                raise HTTPException(403, "好友关系已撤销")
            try:
                data = SharedData.model_validate(result).model_dump(mode="json")
                if not data["sharing"]["current"]:
                    data["current"] = None
                if not data["sharing"]["history"]:
                    data["history"] = []
                if data["current"]:
                    expires = data["current"].get("expires_at")
                    if not expires or datetime.fromisoformat(expires) <= datetime.now(UTC):
                        data["current"] = None
            except (ValidationError, ValueError, TypeError) as exc:
                raise PeerError("好友插件返回的数据格式不兼容") from exc
        # 网络与宿主请求期间若撤销了关系，结果不写缓存也不发送给页面。
        row = relation_for(relation_id, user["id"])
        if row["status"] != "accepted":
            raise HTTPException(403, "好友关系已撤销")
        store.execute(
            "UPDATE relationships SET cache=?,synced_at=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), now(), relation_id),
        )
        return {"friend": display_relation(row, user["id"]), "data": data}

    @app.post("/federation/requests")
    async def incoming(data: IncomingRequest):
        target = store.one("SELECT * FROM users WHERE invite_token=?", (data.invite_token,))
        if not target:
            raise HTTPException(404, "邀请无效")
        if is_demo(target):
            raise HTTPException(403, "虚拟用户仅供本服演示，不接受跨服务器连接")
        origin = peers.validate(data.origin)
        if origin == public_url:
            raise HTTPException(400, "同服用户请使用本地好友申请")
        # 对端必须证明其连接凭据确实登记于所声明的实例，不能冒充其他服务器发申请。
        if settings.native:
            peers.validate_endpoints(origin, data.endpoints)
        authority = await peers.request(
            origin,
            "/federation/state",
            token=data.token,
            endpoint=data.endpoints.get("/federation/state"),
            require_signed=settings.native,
        )
        profile = authority.get("profile", {})
        if (
            authority.get("request_id") != data.request_id
            or authority.get("intended_peer") != public_url
            or authority.get("status") != "pending"
            or profile.get("id") != data.user_id
            or not isinstance(profile.get("nickname"), str)
            or not 1 <= len(profile["nickname"]) <= 60
        ):
            raise HTTPException(400, "无法确认申请来自所声明的插件实例")
        if blocked_peer(target["id"], origin, data.user_id):
            raise HTTPException(403, "当前无法接受该用户的好友申请")
        existing = store.one(
            "SELECT * FROM relationships WHERE peer_origin=? AND remote_request_id=?",
            (origin, data.request_id),
        )
        if existing:
            if existing["token_hash"] != digest(data.token) or existing["user_id"] != target["id"]:
                raise HTTPException(409, "申请标识冲突")
        else:
            duplicate = store.one(
                "SELECT id FROM relationships WHERE user_id=? AND peer_origin=? AND peer_user_id=? "
                "AND status IN ('pending','accepted')",
                (target["id"], origin, data.user_id),
            )
            if duplicate:
                raise HTTPException(409, "已经是好友，或已有待处理的申请")
            if store.one("SELECT id FROM relationships WHERE token_hash=?", (digest(data.token),)):
                raise HTTPException(409, "连接凭据已被使用")
            incoming_id = str(uuid.uuid4())
            store.execute(
                "INSERT INTO relationships(id,user_id,peer_origin,peer_user_id,peer_name,"
                "direction,status,token,token_hash,remote_request_id,created_at) "
                "VALUES(?,?,?,?,?,'incoming','pending',?,?,?,?)",
                (
                    incoming_id,
                    target["id"],
                    origin,
                    data.user_id,
                    profile["nickname"],
                    store.encrypt(data.token),
                    digest(data.token),
                    data.request_id,
                    time.time(),
                ),
            )
            if data.endpoints:
                store.execute(
                    "UPDATE relationships SET peer_endpoints=? WHERE peer_origin=? "
                    "AND remote_request_id=?",
                    (store.encrypt(json.dumps(data.endpoints)), origin, data.request_id),
                )
            community.notify(
                target["id"],
                community.remote_actor(incoming_id),
                "friend_request",
                incoming_id,
                key=f"friend_request:{incoming_id}",
            )
        return {
            "user_id": target["id"],
            "nickname": target["nickname"],
            "protocol": 1,
            "endpoints": {
                path: url
                for path, url in settings.federation_endpoints.items()
                if path != "/federation/social" or "/federation/social" in data.endpoints
            },
        }

    @app.get("/federation/state")
    async def state(request: Request):
        row = peer_relation(request)
        if not allowed_relation(row, row["user_id"]):
            raise HTTPException(403, "该好友连接已被屏蔽")
        if row["status"] in {"rejected", "revoked"}:
            return {"status": row["status"], "request_id": row["id"]}
        user = store.one("SELECT * FROM users WHERE id=?", (row["user_id"],))
        return {
            "status": row["status"],
            "request_id": row["id"],
            "intended_peer": row["peer_origin"],
            "profile": {
                "id": user["id"],
                "nickname": user["nickname"],
                **person_fields(user, user["id"]),
            },
            "endpoints": settings.federation_endpoints,
            "sharing": {
                "current": bool(user["share_current"]),
                "history": bool(user["share_history"]),
                "posts": bool(user["share_posts"]),
            },
        }

    @app.get("/federation/data")
    async def federated_data(request: Request):
        row = peer_relation(request)
        if not allowed_relation(row, row["user_id"]):
            raise HTTPException(403, "该好友连接已被屏蔽")
        if row["status"] in {"pending", "accepted"}:
            row = await refresh_remote(row)
        if row["status"] != "accepted":
            raise HTTPException(403, "好友关系尚未确认或已撤销")
        result = await shared_snapshot(row["user_id"])
        latest = store.one("SELECT * FROM relationships WHERE id=?", (row["id"],))
        if latest["status"] != "accepted" or not allowed_relation(latest, row["user_id"]):
            raise HTTPException(403, "好友关系已撤销")
        return result

    static_dir = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir, check_dir=False), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    return app
