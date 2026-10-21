"""插件私有的资料、通知、屏蔽及举报审核；不访问宿主数据库或文件上传接口。"""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, model_validator

from .demo import profile as demo_profile
from .media import sanitize_media
from .store import Store

Avatar = Literal["initials", "xiaoying", "achuan", "xiaolu", "xingzi"]
Target = Literal["post", "comment", "network_comment"]
AVATARS = {"initials", "xiaoying", "achuan", "xiaolu", "xingzi"}
_LABELS = {
    "friend_request": "申请添加你为好友",
    "like": "赞了你的推荐",
    "comment": "评论了你的推荐",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Community:
    """集中处理社交边界，供普通 API、好友路由和动态路由复用。

    公开资料永远只有身份展示字段，不因关注、发帖或好友关系扩大观影权限。
    屏蔽双向生效；跨服对象使用本地关系推导的不透明编号，不创建虚假用户行。
    审核只改变插件内可见性，并保留举报快照和每次管理员操作的独立审计记录。
    """

    def __init__(self, store: Store, *, server_avatars=False):
        self.store = store
        self.server_avatars = server_avatars
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS community_profiles (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    avatar_id TEXT NOT NULL DEFAULT 'initials', bio TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_blocks (
                    user_id TEXT NOT NULL REFERENCES users(id), target_id TEXT NOT NULL,
                    target_name TEXT NOT NULL, is_remote INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, PRIMARY KEY(user_id,target_id)
                );
                CREATE INDEX IF NOT EXISTS community_blocks_target ON community_blocks(target_id);
                CREATE TABLE IF NOT EXISTS community_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL REFERENCES users(id), actor_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL, actor_avatar TEXT NOT NULL,
                    actor_remote INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
                    target_id TEXT NOT NULL, event_key TEXT NOT NULL,
                    created_at TEXT NOT NULL, read_at TEXT,
                    UNIQUE(user_id,event_key)
                );
                CREATE INDEX IF NOT EXISTS community_notifications_owner
                    ON community_notifications(user_id,read_at,id);
                CREATE TABLE IF NOT EXISTS community_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reporter_id TEXT NOT NULL REFERENCES users(id), target_type TEXT NOT NULL,
                    target_id INTEGER NOT NULL, target_user_id TEXT NOT NULL,
                    reason TEXT NOT NULL, snapshot TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                    resolved_at TEXT, UNIQUE(reporter_id,target_type,target_id)
                );
                CREATE INDEX IF NOT EXISTS community_reports_status ON community_reports(status,id);
                CREATE TABLE IF NOT EXISTS community_report_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL REFERENCES community_reports(id),
                    moderator_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_hidden (
                    kind TEXT NOT NULL, target_id INTEGER NOT NULL, hidden INTEGER NOT NULL,
                    moderator_id TEXT NOT NULL REFERENCES users(id), note TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(kind,target_id)
                );
            """)

    @staticmethod
    def remote_actor(relationship_id: str) -> str:
        return "remote:" + hashlib.sha256(relationship_id.encode()).hexdigest()[:24]

    def blocked(self, actor_id: str, target_id: str) -> bool:
        if getattr(self.store, "network", None) and self.store.one(
            "SELECT 1 FROM network_blocks WHERE (user_id=? AND actor=?) OR (user_id=? AND actor=?)",
            (actor_id, target_id, target_id, actor_id),
        ):
            return True
        return bool(
            self.store.one(
                "SELECT 1 FROM community_blocks WHERE "
                "(user_id=? AND target_id=?) OR (user_id=? AND target_id=?)",
                (actor_id, target_id, target_id, actor_id),
            )
        )

    def can_interact(self, actor_id: str, target_id: str) -> bool:
        # 远端 actor 没有本服 users 行；身份绑定由调用方的已验证好友连接负责。
        return bool(actor_id and target_id and not self.blocked(actor_id, target_id))

    def _relationship(self, owner_id: str, relationship_id: str) -> dict:
        row = self.store.one(
            "SELECT id,peer_name,peer_user_id,status FROM relationships WHERE id=? "
            "AND user_id=? AND direction!='local' AND status IN ('pending','accepted')",
            (relationship_id, owner_id),
        )
        if not row:
            raise HTTPException(404, "没有这条已绑定的好友连接")
        return row

    def _remote_profile(self, actor_id: str, viewer_id: str) -> dict:
        for row in self.store.all(
            "SELECT id,peer_name FROM relationships WHERE user_id=? AND direction!='local' "
            "AND status IN ('pending','accepted')",
            (viewer_id,),
        ):
            if self.remote_actor(row["id"]) == actor_id:
                return {
                    "id": actor_id,
                    "nickname": (row["peer_name"] or "跨服影友")[:60],
                    "avatar_id": "initials",
                    "bio": "",
                    "is_self": False,
                    "is_demo": False,
                    "is_remote": True,
                }
        raise HTTPException(404, "没有这位已绑定的跨服影友")

    def public_profile(self, author: str | dict, viewer_id: str) -> dict:
        """仅序列化展示资料；内容可见性仍由 visible 和各读接口单独检查。"""
        author_id = author["id"] if isinstance(author, dict) else author
        user = self.store.one("SELECT id,nickname,role FROM users WHERE id=?", (author_id,))
        if user is None:
            if re.fullmatch(r"network:[0-9a-f]{64}", author_id or ""):
                profile = self.store.one(
                    "SELECT * FROM network_public_actors WHERE id=?", (author_id,)
                )
                if not profile:
                    raise HTTPException(404, "没有这位互联影友")
                return {
                    "id": author_id,
                    "nickname": profile["nickname"],
                    "avatar_id": "initials",
                    "bio": "",
                    "is_self": False,
                    "is_demo": False,
                    "is_remote": True,
                }
            if not re.fullmatch(r"remote:[0-9a-f]{24}", author_id or ""):
                raise HTTPException(404, "没有这位影友")
            # 字典仅由已验证好友连接的服务端调用方传入；HTTP 个人页只能按绑定查字符串ID。
            if isinstance(author, dict) and author.get("is_remote") is True:
                return {
                    "id": author_id,
                    "nickname": str(author.get("nickname") or "跨服影友")[:60],
                    "avatar_id": (
                        author.get("avatar_id")
                        if author.get("avatar_id") in AVATARS
                        else "initials"
                    ),
                    "bio": str(author.get("bio") or "")[:300],
                    "is_self": author_id == viewer_id,
                    "is_demo": False,
                    "is_remote": True,
                }
            return self._remote_profile(author_id, viewer_id)
        data = self.store.one(
            "SELECT avatar_id,bio FROM community_profiles WHERE user_id=?", (author_id,)
        )
        result = {
            "id": user["id"],
            "nickname": user["nickname"],
            "avatar_id": data["avatar_id"]
            if data and data["avatar_id"] in AVATARS
            else demo_profile(author_id)["avatar_id"]
            if user["role"] == "demo"
            else "initials",
            "bio": str(data["bio"])[:300] if data else "",
            "is_self": author_id == viewer_id,
            "is_demo": user["role"] == "demo",
            "is_remote": False,
        }
        if self.server_avatars and user["role"] != "demo":
            result["avatar_id"] = "initials"
            # Table is created when the complete application is assembled.
            profile = self.store.one(
                "SELECT avatar_version FROM account_profiles WHERE user_id=?", (author_id,)
            )
            if profile and profile["avatar_version"]:
                result["avatar_url"] = (
                    "/api/v1/plugins/watch-friends/watch/avatar/"
                    + author_id
                    + "?v="
                    + profile["avatar_version"]
                )
        if author_id == viewer_id:
            result["role"] = user["role"]
        return result

    def hidden(self, kind: str, target_id: int) -> bool:
        row = self.store.one(
            "SELECT hidden FROM community_hidden WHERE kind=? AND target_id=?", (kind, target_id)
        )
        return bool(row and row["hidden"])

    def visible(self, kind: str, target_id: int, viewer_id: str) -> bool:
        if kind not in {"post", "comment", "network_comment"} or self.hidden(kind, target_id):
            return False
        table = {
            "post": "social_posts",
            "comment": "social_comments",
            "network_comment": "network_comments",
        }[kind]
        row = self.store.one(f"SELECT * FROM {table} WHERE id=?", (target_id,))
        if row is None or not self.can_interact(viewer_id, row["user_id"]):
            return False
        policy = getattr(self.store, "display_policy", None)
        if kind == "post" and policy and not policy.guard(row["user_id"])(json.loads(row["media"])):
            return False
        return kind == "post" or self.visible("post", row["post_id"], viewer_id)

    def ensure_visible(self, kind: str, target_id: int, viewer_id: str) -> None:
        if not self.visible(kind, target_id, viewer_id):
            raise HTTPException(404, "这条内容不存在或当前不可见")

    def notify(
        self,
        recipient: str,
        actor: str | dict,
        kind: str,
        target_id: str | int,
        *,
        key: str | None = None,
    ) -> int | None:
        """固定事件摘要不包含评论正文、观影数据或地址；事件键保证重复请求不刷通知。"""
        if kind not in _LABELS:
            raise ValueError("通知事件类型无效")
        actor_id = actor["id"] if isinstance(actor, dict) else actor
        with self.store.lock:
            user = self.store.one("SELECT role FROM users WHERE id=?", (recipient,))
            if (
                not user
                or user["role"] == "demo"
                or recipient == actor_id
                or not self.can_interact(recipient, actor_id)
            ):
                return None
            profile = self.public_profile(actor, recipient)
            event_key = key or f"{kind}:{target_id}:{actor_id}"
            self.store.execute(
                "INSERT OR IGNORE INTO community_notifications("
                "user_id,actor_id,actor_name,actor_avatar,actor_remote,kind,target_id,"
                "event_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    recipient,
                    actor_id,
                    profile["nickname"],
                    profile["avatar_id"],
                    int(profile["is_remote"]),
                    kind,
                    str(target_id),
                    event_key,
                    _now(),
                ),
            )
            return self.store.one(
                "SELECT id FROM community_notifications WHERE user_id=? AND event_key=?",
                (recipient, event_key),
            )["id"]

    def unread_count(self, user_id: str) -> int:
        return self.store.one(
            "SELECT COUNT(*) AS total FROM community_notifications n WHERE n.user_id=? "
            "AND n.read_at IS NULL AND NOT EXISTS(SELECT 1 FROM community_blocks b "
            "WHERE (b.user_id=n.user_id AND b.target_id=n.actor_id) "
            "OR (b.user_id=n.actor_id AND b.target_id=n.user_id))",
            (user_id,),
        )["total"]

    def published_profile(self, user_id: str) -> dict | None:
        """只读取已经确认发布的广场快照，好友或发帖不会使私有观看数据自动公开。"""
        user = self.store.one(
            "SELECT nickname,discoverable,public_recent,share_favorites,public_history,"
            "public_favorites FROM users WHERE id=?",
            (user_id,),
        )
        network = getattr(self.store, "network", None)
        if user and network:
            card = network.local_card(user_id)
            if card is not None:
                return card
        if not user or not user["discoverable"]:
            return None

        def entries(value):
            try:
                data = json.loads(value)
            except (ValueError, TypeError):
                return []
            return data if isinstance(data, list) else []

        def media(value):
            result = sanitize_media(value)
            result.pop("media_item_id", None)
            if re.fullmatch(r"[a-z-]{1,40}", str(value.get("demo_id", ""))):
                result["demo_id"] = value["demo_id"]
            return result

        policy = getattr(self.store, "display_policy", None)
        allowed = policy.guard(user_id) if policy else lambda value: True
        history = []
        if user["public_recent"]:
            for row in entries(user["public_history"])[:5]:
                if not isinstance(row, dict) or not isinstance(row.get("media"), dict):
                    continue
                if not allowed(row):
                    continue
                result = {"media": media(row["media"])}
                for field in ("started_at", "ended_at"):
                    if isinstance(row.get(field), str):
                        result[field] = row[field][:64]
                if isinstance(row.get("progress_percent"), (int, float)):
                    result["progress_percent"] = max(0, min(100, row["progress_percent"]))
                result["completed"] = row.get("completed") is True
                history.append(result)
        return {
            "nickname": user["nickname"],
            "history": history,
            "favorites": [
                media(row)
                for row in entries(user["public_favorites"])[:500]
                if isinstance(row, dict) and allowed(row)
            ]
            if user["share_favorites"]
            else [],
        }

    def target(self, kind: str, target_id: int) -> dict | None:
        if kind not in {"post", "comment", "network_comment"}:
            return None
        table = {
            "post": "social_posts",
            "comment": "social_comments",
            "network_comment": "network_comments",
        }[kind]
        return self.store.one(f"SELECT * FROM {table} WHERE id=?", (target_id,))

    def report_view(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "reporter": self.public_profile(row["reporter_id"], ""),
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "reason": row["reason"],
            "target": json.loads(row["snapshot"]),
            "target_exists": self.target(row["target_type"], row["target_id"]) is not None,
            "hidden": self.hidden(row["target_type"], row["target_id"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "audit": self.store.all(
                "SELECT id,moderator_id,action,note,created_at FROM community_report_actions "
                "WHERE report_id=? ORDER BY id",
                (row["id"],),
            ),
        }


def register_community(app: FastAPI, store: Store, user_for, *, server_avatars=False) -> Community:
    """注册只依赖可信宿主身份和插件私有库的社区 API，返回可复用的同步服务。"""
    community = Community(store, server_avatars=server_avatars)
    app.state.community = community

    class ProfileInput(BaseModel):
        """只编辑公开展示资料；姓名、角色与共享授权仍由原有身份和隐私流程维护。"""

        avatar_id: Avatar | None = None
        bio: str | None = Field(default=None, max_length=300)

    class BlockInput(BaseModel):
        """本服用户和跨服关系二选一，远端不能凭相撞的人号屏蔽本服成员。"""

        user_id: str | None = Field(default=None, min_length=1, max_length=80)
        relationship_id: str | None = Field(default=None, min_length=1, max_length=80)

        @model_validator(mode="after")
        def one_identity(self):
            if bool(self.user_id) == bool(self.relationship_id):
                raise ValueError("请选择本服用户或已绑定的跨服关系之一")
            return self

    class ReportInput(BaseModel):
        """仅接受本服确实存在的内容编号；拒绝附带远端关系或伪造身份字段。"""

        model_config = {"extra": "forbid"}
        target_type: Target
        target_id: int = Field(ge=1)
        reason: str = Field(min_length=1, max_length=500)

    class ResolutionInput(BaseModel):
        """审核员身份取可信会话，本模型只允许可审计的隐藏、恢复和忽略动作。"""

        action: Literal["hide", "unhide", "dismiss"]
        note: str = Field(default="", max_length=500)

    def account(request: Request, *, admin: bool = False) -> dict:
        user = user_for(request)
        if user["role"] == "demo":
            raise HTTPException(403, "演示用户不能修改真实社区数据")
        if admin and user["role"] != "admin":
            raise HTTPException(403, "只有 MovieClaw 管理员可以审核举报")
        return user

    def own_profile(user: dict) -> dict:
        return {
            "profile": community.public_profile(user["id"], user["id"]),
            "public_profile": community.published_profile(user["id"]),
            "is_admin": user["role"] == "admin",
        }

    @app.get("/api/community/profile")
    async def current_profile(request: Request):
        user = account(request)
        if getattr(store, "network", None) and store.network.enabled():
            await store.network.verify_publishers()
        return own_profile(user)

    @app.patch("/api/community/profile")
    async def update_personal_profile(payload: ProfileInput, request: Request):
        user = account(request)
        with store.lock:
            current = community.public_profile(user["id"], user["id"])
            store.execute(
                "INSERT INTO community_profiles(user_id,avatar_id,bio,updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET avatar_id=excluded.avatar_id,"
                "bio=excluded.bio,updated_at=excluded.updated_at",
                (
                    user["id"],
                    payload.avatar_id or current["avatar_id"],
                    payload.bio.strip() if payload.bio is not None else current["bio"],
                    _now(),
                ),
            )
        return own_profile(user)

    @app.get("/api/community/profiles/{user_id}")
    async def public_profile(request: Request, user_id: str, relationship_id: str | None = None):
        viewer = account(request)
        if relationship_id is not None:
            relation = community._relationship(viewer["id"], relationship_id)
            canonical = community.remote_actor(relation["id"])
            if relation["status"] != "accepted" or user_id not in {
                canonical,
                relation["peer_user_id"],
            }:
                raise HTTPException(404, "没有这位已同意的跨服影友")
            user_id = canonical
        if community.blocked(viewer["id"], user_id):
            raise HTTPException(404, "没有这位可查看的影友")
        target = store.one("SELECT role FROM users WHERE id=?", (user_id,))
        settings = getattr(app.state, "settings", None)
        if (
            target
            and target["role"] == "demo"
            and (
                not viewer.get("demo_enabled", True) or not getattr(settings, "demo_enabled", True)
            )
        ):
            raise HTTPException(404, "演示内容已关闭")
        if getattr(store, "network", None) and store.network.enabled():
            await store.network.verify_publishers()
        return {
            "profile": community.public_profile(user_id, viewer["id"]),
            "public_profile": community.published_profile(user_id),
        }

    @app.get("/api/community/notifications/unread")
    async def notification_count(request: Request):
        return {"unread_count": community.unread_count(account(request)["id"])}

    @app.get("/api/community/notifications")
    async def notifications(
        request: Request,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        user_id = account(request)["id"]
        with store.lock:
            # 在 SQL 分页前排除双向屏蔽，避免一页被过滤后漏掉后面的可见通知。
            rows = store.all(
                "SELECT n.* FROM community_notifications n WHERE n.user_id=? "
                "AND (? IS NULL OR n.id<?) AND NOT EXISTS(SELECT 1 FROM community_blocks b "
                "WHERE (b.user_id=n.user_id AND b.target_id=n.actor_id) "
                "OR (b.user_id=n.actor_id AND b.target_id=n.user_id)) ORDER BY n.id DESC "
                "LIMIT ?",
                (user_id, before, before, limit + 1),
            )
            return {
                "notifications": [
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "label": _LABELS[row["kind"]],
                        "actor": {
                            "id": row["actor_id"],
                            "nickname": row["actor_name"],
                            "avatar_id": row["actor_avatar"],
                            "is_remote": bool(row["actor_remote"]),
                        },
                        "target_type": "relationship"
                        if row["kind"] == "friend_request"
                        else "post",
                        "target_id": row["target_id"],
                        "created_at": row["created_at"],
                        "read_at": row["read_at"],
                    }
                    for row in rows[:limit]
                ],
                "unread_count": community.unread_count(user_id),
                "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @app.post("/api/community/notifications/{notification_id}/read")
    async def read_notification(request: Request, notification_id: int = Path(ge=1)):
        user_id = account(request)["id"]
        with store.lock:
            row = store.one(
                "SELECT id FROM community_notifications WHERE id=? AND user_id=?",
                (notification_id, user_id),
            )
            if row is None:
                raise HTTPException(404, "没有这条本人通知")
            store.execute(
                "UPDATE community_notifications SET read_at=COALESCE(read_at,?) WHERE id=?",
                (_now(), notification_id),
            )
        return {"ok": True, "unread_count": community.unread_count(user_id)}

    @app.get("/api/community/blocks")
    async def blocks(request: Request):
        user_id = account(request)["id"]
        return {
            "blocks": [
                {
                    "user": {
                        "id": row["target_id"],
                        "nickname": row["target_name"],
                        "avatar_id": "initials",
                        "is_remote": bool(row["is_remote"]),
                    },
                    "created_at": row["created_at"],
                }
                for row in store.all(
                    "SELECT * FROM community_blocks WHERE user_id=? ORDER BY created_at DESC",
                    (user_id,),
                )
            ]
        }

    @app.post("/api/community/blocks")
    async def block(payload: BlockInput, request: Request):
        user_id = account(request)["id"]
        with store.lock:
            if payload.relationship_id:
                relation = community._relationship(user_id, payload.relationship_id)
                target_id = community.remote_actor(relation["id"])
            else:
                target_id = payload.user_id
            if target_id == user_id:
                raise HTTPException(400, "不能屏蔽自己")
            profile = community.public_profile(target_id, user_id)
            store.execute(
                "INSERT OR IGNORE INTO community_blocks("
                "user_id,target_id,target_name,is_remote,created_at) VALUES(?,?,?,?,?)",
                (user_id, target_id, profile["nickname"], int(profile["is_remote"]), _now()),
            )
            # 撤回本人的社交访问后清空相关缓存，不删除对方推荐或改变好友状态。
            store.execute(
                "UPDATE relationships SET cache=NULL WHERE "
                "(user_id=? AND target_id=?) OR (user_id=? AND target_id=?)",
                (user_id, target_id, target_id, user_id),
            )
            if payload.relationship_id:
                store.execute(
                    "UPDATE relationships SET cache=NULL WHERE id=? AND user_id=?",
                    (payload.relationship_id, user_id),
                )
        return {"ok": True, "user_id": target_id}

    @app.delete("/api/community/blocks/{target_id}")
    async def unblock(request: Request, target_id: str):
        user_id = account(request)["id"]
        store.execute(
            "DELETE FROM community_blocks WHERE user_id=? AND target_id=?", (user_id, target_id)
        )
        return {"ok": True}

    @app.post("/api/community/reports")
    async def report(payload: ReportInput, request: Request):
        user_id = account(request)["id"]
        reason = payload.reason.strip()
        if not reason:
            raise HTTPException(400, "请填写举报理由")
        with store.lock:
            community.ensure_visible(payload.target_type, payload.target_id, user_id)
            target = community.target(payload.target_type, payload.target_id)
            if target["user_id"] == user_id:
                raise HTTPException(400, "不能举报自己的内容，可直接删除")
            snapshot = {
                "id": target["id"],
                "author": community.public_profile(target["user_id"], user_id),
                "body": target["body"],
            }
            if payload.target_type == "post":
                snapshot["media"] = sanitize_media(json.loads(target["media"]))
            else:
                snapshot["post_id"] = target["post_id"]
            store.execute(
                "INSERT OR IGNORE INTO community_reports("
                "reporter_id,target_type,target_id,target_user_id,reason,snapshot,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    payload.target_type,
                    payload.target_id,
                    target["user_id"],
                    reason,
                    json.dumps(snapshot, ensure_ascii=False),
                    _now(),
                ),
            )
            row = store.one(
                "SELECT id,status FROM community_reports WHERE reporter_id=? "
                "AND target_type=? AND target_id=?",
                (user_id, payload.target_type, payload.target_id),
            )
        return {"id": row["id"], "status": row["status"]}

    @app.get("/api/community/admin/reports")
    async def admin_reports(
        request: Request,
        status: Literal["pending", "resolved", "all"] = "pending",
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        account(request, admin=True)
        with store.lock:
            rows = store.all(
                "SELECT * FROM community_reports WHERE (?='all' OR status=?) "
                "AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                (status, status, before, before, limit + 1),
            )
            return {
                "reports": [community.report_view(row) for row in rows[:limit]],
                "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    @app.post("/api/community/admin/reports/{report_id}/resolve")
    async def resolve_report(
        payload: ResolutionInput, request: Request, report_id: int = Path(ge=1)
    ):
        moderator_id = account(request, admin=True)["id"]
        with store.lock:
            row = store.one("SELECT * FROM community_reports WHERE id=?", (report_id,))
            if row is None:
                raise HTTPException(404, "没有这条举报")
            timestamp = _now()
            if payload.action in {"hide", "unhide"}:
                if community.target(row["target_type"], row["target_id"]) is None:
                    raise HTTPException(404, "举报目标已删除，请将举报标记为忽略")
                store.execute(
                    "INSERT INTO community_hidden("
                    "kind,target_id,hidden,moderator_id,note,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(kind,target_id) DO UPDATE SET "
                    "hidden=excluded.hidden,moderator_id=excluded.moderator_id,"
                    "note=excluded.note,updated_at=excluded.updated_at",
                    (
                        row["target_type"],
                        row["target_id"],
                        int(payload.action == "hide"),
                        moderator_id,
                        payload.note.strip(),
                        timestamp,
                    ),
                )
            store.execute(
                "INSERT INTO community_report_actions("
                "report_id,moderator_id,action,note,created_at) "
                "VALUES(?,?,?,?,?)",
                (report_id, moderator_id, payload.action, payload.note.strip(), timestamp),
            )
            store.execute(
                "UPDATE community_reports SET status='resolved',resolved_at=? WHERE id=?",
                (timestamp, report_id),
            )
            return {
                "report": community.report_view(
                    store.one("SELECT * FROM community_reports WHERE id=?", (report_id,))
                )
            }

    return community
