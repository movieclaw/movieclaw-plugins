"""联网名片的好友入口与公开推荐互动；陌生人权限不继承好友私有共享。"""

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field

from .network import canonical
from .store import digest
from .watch_archive import safe_avatar


def actor_id(node, actor):
    return "network:" + digest(node + ":" + actor)


def post_key(post):
    return post.get("key") or digest(canonical(post).decode())


class Interaction(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["invite", "view", "like", "comment", "delete_comment", "report"]
    post_key: str = Field(default="", max_length=64)
    liked: bool = Field(default=False, strict=True)
    body: str = Field(default="", max_length=500)
    comment_id: int | None = Field(default=None, ge=1)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=16, max_length=64)
    before: int | None = Field(default=None, ge=1)
    consent: bool = Field(default=False, strict=True)


class NetworkInteractions:
    """仅对来源仍公开且未被库排除的具体推荐操作；明细与评论有界分页。"""

    def __init__(self, network, invitation, send_friend):
        self.network, self.store, self.invitation, self.send_friend = (
            network,
            network.store,
            invitation,
            send_friend,
        )
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS network_public_actors(id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,avatar_data TEXT);
            CREATE TABLE IF NOT EXISTS network_likes(
                post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                actor TEXT NOT NULL,PRIMARY KEY(post_id,actor));
            CREATE TABLE IF NOT EXISTS network_comments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,
                request_id TEXT NOT NULL,
                UNIQUE(user_id,request_id));
            CREATE TABLE IF NOT EXISTS network_report_actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,report_id INTEGER NOT NULL,
                moderator_id TEXT NOT NULL,action TEXT NOT NULL,note TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS network_reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,actor TEXT NOT NULL,
                reason TEXT NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,moderator_id TEXT,note TEXT,
                UNIQUE(post_id,actor));
        """)
        network.mail.handler = self.receive
        network.interactions = self

    def target(self, owner, key):
        row = self.network.preference(owner)
        if not self.network.enabled() or not row["snapshot"]:
            raise HTTPException(404, "对方名片已撤下")
        card = self.network.filtered_snapshot(owner, json.loads(row["snapshot"]))
        snapshot = json.loads(row["snapshot"])
        for index, post in enumerate(snapshot["card"]["recommendations"]):
            if post in card["recommendations"] and post_key(post) == key:
                number = snapshot["provenance"]["recommendations"][index]["post_id"]
                result = self.store.one(
                    "SELECT * FROM social_posts WHERE id=? AND user_id=?", (number, owner)
                )
                if result:
                    return result
        raise HTTPException(404, "这条推荐已撤下、更新或被排除")

    async def receive(self, node, body, authorized=lambda: None):
        if (
            set(body) != {"target", "actor", "profile", "action"}
            or not isinstance(body["actor"], str)
            or not 1 <= len(body["actor"]) <= 64
        ):
            raise HTTPException(422, "互联用户身份无效")
        action = Interaction.model_validate(body["action"])
        profile = body["profile"]
        if (
            not isinstance(profile, dict)
            or set(profile) != {"nickname", "avatar_data"}
            or not isinstance(profile["nickname"], str)
            or not 1 <= len(profile["nickname"]) <= 60
        ):
            raise HTTPException(422, "互联用户资料无效")
        actor = actor_id(node, body["actor"])
        owner = body["target"]
        # 发布者账号必须有效；好友与社交身份不借管理员权限绕过停用账号。
        await self.network.verify_publishers()
        authorized()
        if not any(v["payload"]["actor"] == owner for v in self.network.local_envelopes()):
            raise HTTPException(404, "对方名片已撤下或授权失效")
        if self.store.one(
            "SELECT 1 FROM network_blocks WHERE user_id=? AND actor=?", (owner, actor)
        ) or not self.network.community.can_interact(actor, owner):
            raise HTTPException(404, "对方目前不接受此互动")
        user = self.store.one("SELECT * FROM users WHERE id=?", (owner,))
        if action.action == "invite":
            return {"invite_code": self.invitation(user)}
        post = self.target(owner, action.post_key)
        self.store.execute(
            (
                "INSERT INTO network_public_actors VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
                "SET nickname=excluded.nickname,avatar_data=excluded.avatar_data"
            ),
            (actor, profile["nickname"], safe_avatar(profile["avatar_data"])),
        )
        if action.action == "like":
            if action.liked:
                changed = not self.store.one(
                    "SELECT 1 FROM network_likes WHERE post_id=? AND actor=?", (post["id"], actor)
                )
                self.store.execute(
                    "INSERT OR IGNORE INTO network_likes VALUES(?,?)", (post["id"], actor)
                )
                if changed:
                    self.network.community.notify(
                        owner,
                        {"id": actor, "nickname": profile["nickname"], "is_remote": True},
                        "like",
                        post["id"],
                        key=f"network-like:{post['id']}:{actor}",
                    )
            else:
                self.store.execute(
                    "DELETE FROM network_likes WHERE post_id=? AND actor=?", (post["id"], actor)
                )
        elif action.action == "comment":
            if not action.consent or not action.body.strip():
                raise HTTPException(422, "请确认向对方服务器及推荐查看者公开昵称和评论")
            previous = self.store.one(
                "SELECT * FROM network_comments WHERE user_id=? AND request_id=?",
                (actor, action.request_id),
            )
            if previous and (
                previous["body"] != action.body.strip() or previous["post_id"] != post["id"]
            ):
                raise HTTPException(409, "该评论请求已被使用")
            self.store.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "network_comments(post_id,user_id,body,created_at,request_id) "
                    "VALUES(?,?,?,?,?)"
                ),
                (
                    post["id"],
                    actor,
                    action.body.strip(),
                    datetime.now(UTC).isoformat(),
                    action.request_id,
                ),
            )
            if previous is None:
                self.network.community.notify(
                    owner,
                    {"id": actor, "nickname": profile["nickname"], "is_remote": True},
                    "comment",
                    post["id"],
                    key=f"network-comment:{actor}:{action.request_id}",
                )
        elif action.action == "delete_comment":
            self.store.execute(
                "DELETE FROM network_comments WHERE post_id=? AND id=? AND user_id=?",
                (post["id"], action.comment_id, actor),
            )
        elif action.action == "report":
            if not action.consent or not action.body.strip():
                raise HTTPException(422, "请填写并确认发送举报理由")
            self.store.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "network_reports(post_id,actor,reason,snapshot,created_at) "
                    "VALUES(?,?,?,?,?)"
                ),
                (
                    post["id"],
                    actor,
                    action.body.strip(),
                    json.dumps(
                        {"media": json.loads(post["media"]), "body": post["body"]},
                        ensure_ascii=False,
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return {"ok": True}
        visible, cursor = [], action.before
        while len(visible) <= 20:
            batch = self.store.all(
                "SELECT * FROM network_comments WHERE post_id=? "
                "AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 100",
                (post["id"], cursor, cursor),
            )
            for row in batch:
                if not self.network.community.hidden(
                    "network_comment", row["id"]
                ) and self.network.community.can_interact(actor, row["user_id"]):
                    visible.append(row)
                    if len(visible) > 20:
                        break
            if len(visible) > 20 or len(batch) < 100:
                break
            cursor = batch[-1]["id"]
        rows = visible
        return {
            "liked": bool(
                self.store.one(
                    "SELECT 1 FROM network_likes WHERE post_id=? AND actor=?", (post["id"], actor)
                )
            ),
            "like_count": self.store.one(
                "SELECT COUNT(*) AS n FROM network_likes WHERE post_id=?", (post["id"],)
            )["n"]
            + self.store.one(
                "SELECT COUNT(*) AS n FROM social_likes WHERE post_id=?", (post["id"],)
            )["n"],
            "comments": [
                {
                    "id": r["id"],
                    "body": r["body"],
                    "created_at": r["created_at"],
                    "is_self": r["user_id"] == actor,
                    "author": self.wire_profile(r["user_id"]),
                }
                for r in rows[:20]
            ],
            "next_before": rows[19]["id"] if len(rows) > 20 else None,
        }

    def wire_profile(self, actor):
        # 评论列表只显示署名；不重复附带大缩略图，二十条评论也必须满足消息大小边界。
        row = self.store.one("SELECT nickname FROM network_public_actors WHERE id=?", (actor,))
        if not row:
            row = self.store.one("SELECT nickname FROM users WHERE id=?", (actor,))
        return {"nickname": row["nickname"] if row else "影友", "avatar_data": None}

    def card(self, user, actor):
        if not any(c["id"] == actor for c in self.network.directory(user["id"])):
            raise HTTPException(404, "这张名片已撤下、过期或被屏蔽")
        for row in self.store.all("SELECT * FROM network_cache WHERE expires>?", (time.time(),)):
            if actor_id(row["node"], row["actor"]) == actor:
                return row, json.loads(row["envelope"])
        raise HTTPException(404, "没有仍有效的名片")

    def register(self, app):
        @app.post("/api/network/users/{actor}/interact")
        async def network_interact(actor: str, payload: Interaction, request: Request):
            user = self.network.account(request)
            current = await self.network.source.identity(self.store.decrypt(user["token"]))
            if (current["username"], current["role"], current["member_id"]) != (
                user["username"],
                user["role"],
                user["member_id"],
            ):
                raise HTTPException(403, "本人账号身份已改变")
            visible = next(
                (v for v in self.network.directory(user["id"]) if v["id"] == actor), None
            )
            if visible and visible.get("discovered"):
                if payload.action != "invite":
                    raise HTTPException(403, "公共发现仅支持申请好友，进一步访问需双方同意")
                if not payload.consent:
                    raise HTTPException(422, "请确认向对方发送好友申请及服务器地址提示")
                code = await self.network.discovery.friend_code(user["id"], actor)
                return await self.send_friend(code, request, public=True)
            row, envelope = self.card(user, actor)
            if payload.action != "view" and not payload.consent:
                raise HTTPException(422, "请确认向对方分享昵称和本次互动内容")
            profile = self.network.community.public_profile(user["id"], user["id"])
            thumbnail = self.store.one(
                "SELECT avatar_data FROM account_profiles WHERE user_id=?", (user["id"],)
            )
            result = await self.network.mail.send(
                row["node"],
                envelope["key"],
                {
                    "target": row["actor"],
                    "actor": user["id"],
                    "profile": {
                        "nickname": profile["nickname"],
                        "avatar_data": safe_avatar(thumbnail["avatar_data"]) if thumbnail else None,
                    },
                    "action": payload.model_dump(),
                },
            )
            if payload.action == "invite":
                return await self.send_friend(result["invite_code"], request)
            return result

        class LocalComment(BaseModel):
            body: str = Field(min_length=1, max_length=500)
            consent: bool = Field(default=False, strict=True)
            request_id: str = Field(
                default_factory=lambda: str(uuid.uuid4()), min_length=16, max_length=64
            )

        @app.post("/api/feed/{post_id}/network-comments", status_code=201)
        async def local_network_comment(post_id: int, payload: LocalComment, request: Request):
            user = self.network.account(request)
            post = self.store.one("SELECT * FROM social_posts WHERE id=?", (post_id,))
            if (
                not self.network.enabled()
                or not post
                or not self.network.community.visible("post", post_id, user["id"])
            ):
                raise HTTPException(404, "没有这条可见推荐")
            pref = self.network.preference(post["user_id"])
            snapshot = json.loads(pref["snapshot"] or "null")
            if not snapshot or post_id not in {
                r["post_id"] for r in snapshot["provenance"]["recommendations"]
            }:
                raise HTTPException(409, "此推荐尚未加入联网名片，请先在联网设置中预览发布")
            if not payload.consent or not payload.body.strip():
                raise HTTPException(422, "请确认将本条评论公开给联网推荐查看者")
            self.store.execute(
                "INSERT OR IGNORE INTO "
                "network_comments(post_id,user_id,body,created_at,request_id)"
                " VALUES(?,?,?,?,?)",
                (
                    post_id,
                    user["id"],
                    payload.body.strip(),
                    datetime.now(UTC).isoformat(),
                    payload.request_id,
                ),
            )
            row = self.store.one(
                "SELECT * FROM network_comments WHERE user_id=? AND request_id=?",
                (user["id"], payload.request_id),
            )
            if row["post_id"] != post_id or row["body"] != payload.body.strip():
                raise HTTPException(409, "评论请求已被其他内容使用")
            return {
                "id": "n" + str(row["id"]),
                "author": self.network.community.public_profile(user["id"], user["id"]),
                "body": row["body"],
                "created_at": row["created_at"],
            }

        @app.delete("/api/feed/network-comments/{comment_id}")
        async def delete_local_network_comment(comment_id: int, request: Request):
            user = self.network.account(request)
            row = self.store.one(
                "SELECT * FROM network_comments WHERE id=? AND user_id=?", (comment_id, user["id"])
            )
            if not row:
                raise HTTPException(404, "没有这条本人联网评论")
            self.store.execute("DELETE FROM network_comments WHERE id=?", (comment_id,))
            return {"ok": True}

        @app.get("/api/network/admin/reports")
        async def network_reports(request: Request, before: int | None = Query(None, ge=1)):
            self.network.account(request, admin=True)
            rows = self.store.all(
                (
                    "SELECT id,post_id,actor,reason,snapshot,status,created_at,note FROM "
                    "network_reports WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT 21"
                ),
                (before, before),
            )
            return {
                "reports": [{**r, "snapshot": json.loads(r["snapshot"])} for r in rows[:20]],
                "next_before": rows[19]["id"] if len(rows) > 20 else None,
            }

        class Resolution(BaseModel):
            action: Literal["hide", "unhide", "dismiss"]
            note: str = Field(default="", max_length=500)

        @app.post("/api/network/admin/reports/{number}/resolve")
        async def network_resolve(number: int, payload: Resolution, request: Request):
            user = self.network.account(request, admin=True)
            row = self.store.one("SELECT * FROM network_reports WHERE id=?", (number,))
            if not row:
                raise HTTPException(404, "没有这条举报")
            if payload.action != "dismiss":
                self.store.execute(
                    (
                        "INSERT INTO community_hidden VALUES('post',?,?,?,?,?) ON "
                        "CONFLICT(kind,target_id) DO UPDATE SET "
                        "hidden=excluded.hidden,moderator_id=excluded.moderator_id,note=excluded.note,updated_at=excluded.updated_at"
                    ),
                    (
                        row["post_id"],
                        int(payload.action == "hide"),
                        user["id"],
                        payload.note,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            self.store.execute(
                "INSERT INTO "
                "network_report_actions(report_id,moderator_id,action,note,created_at)"
                " VALUES(?,?,?,?,?)",
                (number, user["id"], payload.action, payload.note, datetime.now(UTC).isoformat()),
            )
            self.store.execute(
                "UPDATE network_reports SET status='resolved',moderator_id=?,note=? WHERE id=?",
                (user["id"], payload.note, number),
            )
            return {"ok": True}
