"""仅经已确认关系与正式签名能力交换动态；远端作者由关系令牌决定。"""

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, ValidationError

from .peers import PeerError
from .store import digest

SOCIAL_PATH = "/federation/social"
MAX_ID = 2**63 - 1


def remote_actor(relation_id: str) -> str:
    return "remote:" + digest(relation_id)[:24]


class SocialAction(BaseModel):
    """固定动作与有界参数；不接收任何作者编号、服务器地址或任意目标 URL。"""

    action: Literal["feed", "comments", "like", "comment", "delete_comment"]
    post_id: int | None = Field(default=None, ge=1, le=MAX_ID)
    comment_id: str = Field(default="", max_length=32)
    before: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=20, ge=1, le=30)
    liked: bool | None = Field(default=None, strict=True)
    body: str = Field(default="", max_length=500)
    request_id: str = Field(default="", max_length=64)


class SocialStorage:
    """远端互动另存私有表，不创建冒充真实成员的 users 行。

    一条关系对应一个远端作者。远端评论仅帖作者和该关系参与者可见；其他本服
    成员及其他远端关系均不会读到。令牌更换后不能以任意用户编号继承旧互动。
    """

    def __init__(self, store, community=None):
        self.store, self.community = store, community
        with store.lock:
            store.db.executescript("""
                CREATE TABLE IF NOT EXISTS network_public_actors(id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,avatar_data TEXT);
                CREATE TABLE IF NOT EXISTS network_likes(
                    post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                    actor TEXT NOT NULL,PRIMARY KEY(post_id,actor));
                CREATE TABLE IF NOT EXISTS network_comments(id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,
                request_id TEXT NOT NULL,UNIQUE(user_id,request_id));
                CREATE TABLE IF NOT EXISTS social_remote_likes (
                    post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                    relationship_id TEXT NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
                    PRIMARY KEY(post_id,relationship_id)
                );
                CREATE TABLE IF NOT EXISTS social_remote_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                    relationship_id TEXT NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
                    body TEXT NOT NULL, created_at TEXT NOT NULL, request_id TEXT NOT NULL,
                    UNIQUE(relationship_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS social_remote_comment_post
                    ON social_remote_comments(post_id,created_at,id);
            """)

    def allowed(self, actor_id, owner_id):
        return self.community is None or self.community.can_interact(actor_id, owner_id)

    def visible(self, kind, item_id, viewer_id):
        return self.community is None or self.community.visible(kind, item_id, viewer_id)

    def profile(self, actor, viewer_id):
        if self.community is not None:
            actor = self.community.public_profile(actor, viewer_id)
        return {**actor, "is_self": actor["id"] == viewer_id}

    def notify(self, owner, actor, kind, post_id, key):
        if self.community is not None:
            self.community.notify(owner, actor, kind, post_id, key=key)

    def relation_profile(self, relation_id, viewer_id):
        row = self.store.one(
            "SELECT peer_name,user_id FROM relationships WHERE id=?", (relation_id,)
        )
        # 社区只向该关系的本服主人解析远端简介；网络参与者身份仍由此关系令牌决定。
        result = self.profile(
            {"id": remote_actor(relation_id), "nickname": row["peer_name"], "is_remote": True},
            row["user_id"],
        )
        return result | {"is_self": result["id"] == viewer_id}

    def post_page(self, viewer_id, *, author_id=None, before=None, limit=20):
        """先执行屏蔽、审核和媒体库规则，再对可见推荐分页，避免产生假空页。"""
        visible, cursor = [], before
        while len(visible) <= limit:
            batch = self.store.all(
                "SELECT * FROM social_posts WHERE (? IS NULL OR user_id=?) "
                "AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 100",
                (author_id, author_id, cursor, cursor),
            )
            for row in batch:
                if self.visible("post", row["id"], viewer_id):
                    visible.append(row)
                    if len(visible) > limit:
                        break
            if len(visible) > limit or len(batch) < 100:
                break
            cursor = batch[-1]["id"]
        return visible[:limit], visible[limit - 1]["id"] if len(visible) > limit else None

    def cursor(self, row):
        value = [row["created_at"], row["source"], row["id"]]
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def comment_page(self, post, viewer_id, *, relation_id=None, before=None, limit=30):
        boundary = None
        if before:
            try:
                # 继续接受旧本服评论的整数游标；新增跨表游标使用无 URL 的不透明数据。
                if before.isdigit():
                    if int(before) > MAX_ID:
                        raise ValueError
                    row = self.store.one(
                        "SELECT * FROM social_comments WHERE id=? AND post_id=?",
                        (int(before), post["id"]),
                    )
                    if row is None:
                        raise ValueError
                    boundary = [row["created_at"], 0, row["id"]]
                else:
                    boundary = json.loads(
                        base64.urlsafe_b64decode(before + "=" * (-len(before) % 4))
                    )
                    if (
                        not isinstance(boundary, list)
                        or len(boundary) != 3
                        or not isinstance(boundary[0], str)
                        or len(boundary[0]) > 64
                        or type(boundary[1]) is not int
                        or boundary[1] not in (0, 1, 2)
                        or type(boundary[2]) is not int
                        or not 1 <= boundary[2] <= MAX_ID
                    ):
                        raise ValueError
            except (ValueError, TypeError):
                raise HTTPException(422, "评论翻页标识无效") from None
        cutoff, source, number = boundary or (None, 0, 0)
        visible = []
        while len(visible) <= limit:
            rows = self.store.all(
                "SELECT * FROM ("
                "SELECT id,user_id,NULL AS relationship_id,body,created_at,0 AS source "
                "FROM social_comments WHERE post_id=? AND (? IS NULL OR user_id=?) "
                "UNION ALL SELECT id,NULL AS user_id,relationship_id,body,created_at,1 AS "
                "source "
                "FROM social_remote_comments WHERE post_id=? "
                "AND ((? IS NOT NULL AND relationship_id=?) OR (? IS NULL AND ?=?)) "
                "UNION ALL SELECT id,user_id,NULL AS relationship_id,body,created_at,2 AS "
                "source "
                "FROM network_comments WHERE post_id=?) "
                "WHERE (? IS NULL OR created_at<? OR (created_at=? AND "
                "(source<? OR (source=? AND id<?)))) "
                "ORDER BY created_at DESC,source DESC,id DESC LIMIT ?",
                (
                    post["id"],
                    relation_id,
                    post["user_id"],
                    post["id"],
                    relation_id,
                    relation_id,
                    relation_id,
                    viewer_id,
                    post["user_id"],
                    post["id"],
                    cutoff,
                    cutoff,
                    cutoff,
                    source,
                    source,
                    number,
                    100,
                ),
            )
            for row in rows:
                allowed = (
                    self.visible("network_comment", row["id"], viewer_id)
                    if row["source"] == 2
                    else self.allowed(viewer_id, remote_actor(row["relationship_id"]))
                    if row["source"]
                    else self.visible("comment", row["id"], viewer_id)
                )
                if allowed:
                    visible.append(row)
                    if len(visible) > limit:
                        break
            if len(visible) > limit or len(rows) < 100:
                break
            cutoff, source, number = rows[-1]["created_at"], rows[-1]["source"], rows[-1]["id"]
        return {
            "comments": [self.comment_view(row, viewer_id) for row in visible[:limit]],
            "next_before": self.cursor(visible[limit - 1]) if len(visible) > limit else None,
        }

    def comment_view(self, row, viewer_id):
        if row.get("source") == 2:
            author = self.community.public_profile(row["user_id"], viewer_id)
            comment_id = "n" + str(row["id"])
        elif row.get("relationship_id"):
            author = self.relation_profile(row["relationship_id"], viewer_id)
            comment_id = "r" + str(row["id"])
        else:
            user = self.store.one("SELECT id,nickname FROM users WHERE id=?", (row["user_id"],))
            author, comment_id = self.profile(user, viewer_id), row["id"]
        return {
            "id": comment_id,
            "author": author,
            "body": row["body"],
            "created_at": row["created_at"],
        }

    def extra_likes(self, post_id):
        return self.store.one(
            "SELECT (SELECT COUNT(*) FROM social_remote_likes WHERE post_id=?) + "
            "(SELECT COUNT(*) FROM network_likes WHERE post_id=?) AS total",
            (post_id, post_id),
        )["total"]

    def local_remote_count(self, post_id, viewer_id, owner_id):
        if viewer_id != owner_id:
            return 0
        return self.store.one(
            "SELECT COUNT(*) AS total FROM social_remote_comments WHERE post_id=?", (post_id,)
        )["total"]

    def act(self, owner_id, relation, data, post_for, post_view):
        actor = remote_actor(relation["id"])
        if not self.allowed(actor, owner_id):
            raise HTTPException(404, "这位好友的动态暂不可访问")
        user = self.store.one("SELECT share_posts FROM users WHERE id=?", (owner_id,))
        if not user["share_posts"]:
            if data.action == "feed":
                return {"posts": [], "next_before": None, "enabled": False}
            raise HTTPException(403, "对方没有开启跨服动态分享")
        if data.action == "feed":
            if data.before and (not data.before.isdigit() or not 1 <= int(data.before) <= MAX_ID):
                raise HTTPException(422, "动态翻页标识无效")
            before = int(data.before) if data.before else None
            limit = min(data.limit, 20)
            rows_visible, next_before = self.post_page(
                actor, author_id=owner_id, before=before, limit=limit
            )
            return {
                "posts": [
                    self.remote_post(row, actor, relation["id"], post_view) for row in rows_visible
                ],
                "next_before": next_before,
                "enabled": True,
            }
        post = post_for(data.post_id)
        if post["user_id"] != owner_id or not self.visible("post", post["id"], actor):
            raise HTTPException(404, "这条好友推荐不存在或已隐藏")
        if data.action == "comments":
            return self.wire_comments(
                self.comment_page(
                    post, actor, relation_id=relation["id"], before=data.before, limit=data.limit
                )
            )
        if data.action == "like":
            if data.liked is None:
                raise HTTPException(422, "请提供点赞状态")
            if data.liked:
                changed = self.store.db.execute(
                    "INSERT OR IGNORE INTO social_remote_likes VALUES(?,?)",
                    (post["id"], relation["id"]),
                ).rowcount
                if changed:
                    self.notify(
                        owner_id,
                        self.relation_profile(relation["id"], owner_id),
                        "like",
                        post["id"],
                        f"like:{post['id']}:{actor}",
                    )
            else:
                self.store.execute(
                    "DELETE FROM social_remote_likes WHERE post_id=? AND relationship_id=?",
                    (post["id"], relation["id"]),
                )
            result = post_view(post, actor)
            return {"liked": data.liked, "like_count": result["like_count"]}
        if data.action == "comment":
            body = data.body.strip()
            if not body or len(data.request_id) < 16:
                raise HTTPException(422, "请填写评论与有效请求标识")
            previous = self.store.one(
                "SELECT * FROM social_remote_comments WHERE relationship_id=? AND request_id=?",
                (relation["id"], data.request_id),
            )
            if previous and (previous["post_id"] != post["id"] or previous["body"] != body):
                raise HTTPException(409, "评论请求标识已被其他内容使用")
            if previous is None:
                cursor = self.store.db.execute(
                    "INSERT INTO social_remote_comments(post_id,relationship_id,body,created_at,"
                    "request_id) VALUES(?,?,?,?,?)",
                    (
                        post["id"],
                        relation["id"],
                        body,
                        datetime.now(UTC).isoformat(),
                        data.request_id,
                    ),
                )
                previous = self.store.one(
                    "SELECT * FROM social_remote_comments WHERE id=?", (cursor.lastrowid,)
                )
                self.notify(
                    owner_id,
                    self.relation_profile(relation["id"], owner_id),
                    "comment",
                    post["id"],
                    f"comment:r{previous['id']}",
                )
            return self.comment_view(previous, actor)
        if (
            not data.comment_id.startswith("r")
            or not data.comment_id[1:].isdigit()
            or not 1 <= int(data.comment_id[1:]) <= MAX_ID
        ):
            raise HTTPException(404, "没有这条本人评论")
        changed = self.store.db.execute(
            ("DELETE FROM social_remote_comments WHERE id=? AND relationship_id=? AND post_id=?"),
            (int(data.comment_id[1:]), relation["id"], post["id"]),
        ).rowcount
        if not changed:
            raise HTTPException(404, "没有这条本人评论")
        return {"ok": True}

    def wire_comments(self, result):
        return {**result, "comments": [{**row, "id": str(row["id"])} for row in result["comments"]]}

    def remote_post(self, row, actor, relation_id, post_view):
        result = post_view(row, actor)
        comments = self.wire_comments(
            self.comment_page(row, actor, relation_id=relation_id, limit=3)
        )
        local_count = self.store.one(
            "SELECT COUNT(*) AS total FROM social_comments WHERE post_id=? AND user_id=?",
            (row["id"], row["user_id"]),
        )["total"]
        remote_count = self.store.one(
            "SELECT COUNT(*) AS total FROM social_remote_comments "
            "WHERE post_id=? AND relationship_id=?",
            (row["id"], relation_id),
        )["total"]
        return {
            **result,
            "comments": list(reversed(comments["comments"])),
            "comment_count": local_count + remote_count,
            "liked": bool(
                self.store.one(
                    "SELECT 1 FROM social_remote_likes WHERE post_id=? AND relationship_id=?",
                    (row["id"], relation_id),
                )
            ),
        }


def register_social_federation(
    app,
    store,
    verified_user,
    relation_for,
    peer_relation,
    refresh_remote,
    peer_request,
    feed,
    community=None,
):
    """浏览器只给本机关系 ID；目标源与签名地址只从已验证的私有关系记录取得。"""

    class Author(BaseModel):
        id: str = Field(max_length=128)
        nickname: str = Field(max_length=60)
        is_self: bool = False
        is_remote: bool = False
        avatar_id: Literal["initials", "xiaoying", "achuan", "xiaolu", "xingzi"] = "initials"
        bio: str = Field(default="", max_length=300)

    class Comment(BaseModel):
        id: str = Field(max_length=32)
        author: Author
        body: str = Field(max_length=500)
        created_at: datetime

    class Post(BaseModel):
        id: int = Field(ge=1)
        author: Author
        media: feed.media_model
        body: str = Field(max_length=2000)
        rating: int | None = Field(default=None, ge=1, le=5, strict=True)
        created_at: datetime
        like_count: int = Field(ge=0)
        liked: bool
        comment_count: int = Field(ge=0)
        comments: list[Comment] = Field(default_factory=list, max_length=3)

    async def accepted(row):
        if row["direction"] == "local":
            raise HTTPException(400, "同服影友请查看本服动态")
        row = await refresh_remote(row)
        if row["status"] != "accepted":
            raise HTTPException(403, "双方同意且关系有效后才能访问跨服动态")
        return row

    async def proxy(relation_id, request, action):
        user = await verified_user(request)
        row = await accepted(relation_for(relation_id, user["id"]))
        if community is not None and not community.can_interact(
            user["id"], remote_actor(row["id"])
        ):
            raise HTTPException(404, "这位好友的动态暂不可访问")
        endpoints = (
            json.loads(store.decrypt(row["peer_endpoints"])) if row.get("peer_endpoints") else {}
        )
        if SOCIAL_PATH not in endpoints:
            raise HTTPException(409, "对方尚不支持跨服动态，请双方升级并重新交换邀请码")
        result = await peer_request(row, SOCIAL_PATH, data=action.model_dump())
        # 不缓存：每次都确认对端最新授权，在途关闭分享或撤销关系不能返回旧动态。
        permission = await peer_request(row, "/federation/state")
        if permission.get("status") != "accepted" or not permission.get("sharing", {}).get(
            "posts", False
        ):
            if action.action == "feed" and result.get("enabled") is False:
                return {"posts": [], "next_before": None, "enabled": False, "supported": True}
            raise HTTPException(403, "好友关系或跨服动态分享已撤回")
        latest = relation_for(relation_id, user["id"])
        if latest["status"] != "accepted" or (
            community is not None
            and not community.can_interact(user["id"], remote_actor(latest["id"]))
        ):
            raise HTTPException(403, "好友关系或访问授权已撤回")
        try:
            if action.action == "feed":
                values = result.get("posts")
                if not isinstance(values, list) or len(values) > 20:
                    raise ValueError
                posts = []
                for value in values:
                    post = Post.model_validate(value).model_dump(mode="json")
                    if post["author"]["id"] != row["peer_user_id"]:
                        raise ValueError
                    post["media"].pop("media_item_id", None)
                    post["media"].pop("demo_id", None)
                    posts.append(
                        post
                        | {"is_remote": True, "readonly": False, "relationship_id": relation_id}
                    )
                cursor = result.get("next_before")
                if cursor is not None and (type(cursor) is not int or cursor < 1):
                    raise ValueError
                return {"posts": posts, "next_before": cursor, "enabled": True, "supported": True}
            if action.action == "comments":
                if not isinstance(result.get("comments"), list) or len(result["comments"]) > 30:
                    raise ValueError
                cursor = result.get("next_before")
                if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
                    raise ValueError
                return {
                    "comments": [
                        Comment.model_validate(row).model_dump(mode="json")
                        for row in result["comments"]
                    ],
                    "next_before": cursor,
                }
            if action.action == "comment":
                return Comment.model_validate(result).model_dump(mode="json")
            if action.action == "like":
                if (
                    type(result.get("liked")) is not bool
                    or type(result.get("like_count")) is not int
                    or result["like_count"] < 0
                ):
                    raise ValueError
                return {"liked": result["liked"], "like_count": result["like_count"]}
            return {"ok": result.get("ok") is True}
        except (ValidationError, ValueError, TypeError) as exc:
            raise PeerError("好友动态格式不兼容，已停止展示") from exc

    @app.post(SOCIAL_PATH)
    async def federated_social(data: SocialAction, request: Request):
        row = await accepted(peer_relation(request))
        with store.lock:
            latest = store.one("SELECT * FROM relationships WHERE id=?", (row["id"],))
            if latest["status"] != "accepted":
                raise HTTPException(403, "好友关系已撤回")
            return feed.social.act(row["user_id"], latest, data, feed.post_for, feed.post_view)

    @app.get("/api/friends/{relation_id}/feed")
    async def friend_feed(
        relation_id: str,
        request: Request,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        return await proxy(
            relation_id,
            request,
            SocialAction(action="feed", before=str(before) if before else None, limit=limit),
        )

    @app.put("/api/friends/{relation_id}/feed/{post_id}/like")
    async def friend_like(
        relation_id: str, request: Request, payload: feed.LikeInput, post_id: int = Path(ge=1)
    ):
        return await proxy(
            relation_id, request, SocialAction(action="like", post_id=post_id, liked=payload.liked)
        )

    @app.get("/api/friends/{relation_id}/feed/{post_id}/comments")
    async def friend_comments(
        relation_id: str,
        request: Request,
        post_id: int = Path(ge=1),
        before: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=20, ge=1, le=30),
    ):
        return await proxy(
            relation_id,
            request,
            SocialAction(action="comments", post_id=post_id, before=before, limit=limit),
        )

    class NewComment(feed.CommentInput):
        request_id: str = Field(
            default_factory=lambda: str(uuid.uuid4()), min_length=16, max_length=64
        )

    @app.post("/api/friends/{relation_id}/feed/{post_id}/comments")
    async def friend_comment(
        relation_id: str, request: Request, payload: NewComment, post_id: int = Path(ge=1)
    ):
        return await proxy(
            relation_id,
            request,
            SocialAction(
                action="comment", post_id=post_id, body=payload.body, request_id=payload.request_id
            ),
        )

    @app.delete("/api/friends/{relation_id}/feed/{post_id}/comments/{comment_id}")
    async def friend_delete_comment(
        relation_id: str, request: Request, comment_id: str, post_id: int = Path(ge=1)
    ):
        return await proxy(
            relation_id,
            request,
            SocialAction(action="delete_comment", post_id=post_id, comment_id=comment_id),
        )
