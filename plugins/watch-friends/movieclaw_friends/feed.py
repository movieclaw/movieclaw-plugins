"""同服影友动态：公开推荐与互动仅存插件私有库，不调用下载或播放操作。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .social_federation import MAX_ID, SocialStorage
from .store import Store


def register_feed(
    app: FastAPI, store: Store, user_for, media_model, *, community=None, source=None
):
    """以宿主确认的本人身份发布和互动，分页读取；演示人物不写真实动态。

    推荐发布是独立的公开动作，只公开昵称、评分、推荐文字与作品资料，不读取私人
    观影或喜欢列表。作者编号始终取当前会话，删除推荐时级联清除它的互动。
    """
    with store.lock:
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS social_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id), media TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS social_likes (
                post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                PRIMARY KEY(post_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS social_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id),
                body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS social_comments_post ON social_comments(post_id,id);
        """)
        # 同版本升级只为旧推荐补充可空评分列，原有正文、互动和记录编号保持不变。
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(social_posts)")}
        if "rating" not in columns:
            store.db.execute(
                "ALTER TABLE social_posts ADD COLUMN rating INTEGER "
                "CHECK(rating IS NULL OR rating BETWEEN 1 AND 5)"
            )
    social = SocialStorage(store, community)

    class PostInput(BaseModel):
        """公开推荐必须明确同意；作者、账号和互动数量都由服务端决定。"""

        media: media_model
        body: str = Field(default="", max_length=2000)
        rating: int | None = Field(default=None, ge=1, le=5, strict=True)
        publish_consent: bool = Field(default=False, strict=True)

        @field_validator("body")
        @classmethod
        def meaningful_body(cls, value: str) -> str:
            return value.strip()

        @model_validator(mode="after")
        def reason_or_rating(self):
            """推荐理由与星级至少提供一项，允许仅打星而不编造推荐文字。"""
            if not self.body and self.rating is None:
                raise ValueError("请打星或填写推荐理由")
            return self

    class MediaInput(BaseModel):
        media: media_model

    class CommentInput(BaseModel):
        """评论只接收有界文字，不接受前端指定作者或可执行富文本。"""

        body: str = Field(min_length=1, max_length=500)

        @field_validator("body")
        @classmethod
        def meaningful_body(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("请填写评论")
            return value

    class LikeInput(BaseModel):
        """使用目标状态而非计数增减，重复请求不会产生重复点赞。"""

        liked: bool = Field(strict=True)

    def account(request: Request) -> dict:
        user = user_for(request)
        if user["role"] == "demo":
            raise HTTPException(403, "演示用户不能发布真实动态")
        return user

    def post_for(post_id: int) -> dict:
        if type(post_id) is not int or not 1 <= post_id <= MAX_ID:
            raise HTTPException(404, "这条推荐已删除或不存在")
        post = store.one("SELECT * FROM social_posts WHERE id=?", (post_id,))
        if post is None:
            raise HTTPException(404, "这条推荐已删除或不存在")
        return post

    def author(user_id: str, viewer_id: str) -> dict:
        user = store.one("SELECT id,nickname FROM users WHERE id=?", (user_id,))
        return social.profile(user, viewer_id)

    def comment_view(row: dict, viewer_id: str) -> dict:
        return social.comment_view(row, viewer_id)

    def post_view(row: dict, viewer_id: str) -> dict:
        likes = store.one(
            ("SELECT COUNT(*) AS total, MAX(user_id=?) AS liked FROM social_likes WHERE post_id=?"),
            (viewer_id, row["id"]),
        )
        count = store.one(
            "SELECT COUNT(*) AS total FROM social_comments WHERE post_id=?", (row["id"],)
        )
        comments = social.comment_page(row, viewer_id, limit=3)["comments"]
        return {
            "id": row["id"],
            "network_public": bool(
                getattr(app.state, "network_interactions", None)
                and app.state.network.enabled()
                and any(
                    v.get("post_id") == row["id"]
                    for v in json.loads(
                        app.state.network.preference(row["user_id"])["snapshot"]
                        or '{"provenance":{"recommendations":[]}}'
                    )["provenance"]["recommendations"]
                )
            ),
            "author": author(row["user_id"], viewer_id),
            "media": json.loads(row["media"]),
            "body": row["body"],
            "rating": row["rating"],
            "created_at": row["created_at"],
            "like_count": likes["total"] + social.extra_likes(row["id"]),
            "liked": bool(likes["liked"]),
            "comment_count": count["total"]
            + store.one("SELECT COUNT(*) AS n FROM network_comments WHERE post_id=?", (row["id"],))[
                "n"
            ]
            + social.local_remote_count(row["id"], viewer_id, row["user_id"]),
            "comments": list(reversed(comments)),
        }

    @app.get("/api/feed")
    async def feed(
        request: Request,
        before: int | None = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=30),
        author_id: str | None = Query(default=None, min_length=1, max_length=64),
    ):
        viewer_id = account(request)["id"]
        with store.lock:
            rows, next_before = social.post_page(
                viewer_id, author_id=author_id, before=before, limit=limit
            )
            return {
                "posts": [post_view(row, viewer_id) for row in rows],
                "next_before": next_before,
            }

    async def eligibility(user, media):
        if (
            type(media.get("tmdb_id")) is not int
            or media["tmdb_id"] <= 0
            or media["kind"] not in {"movie", "tv"}
        ):
            raise HTTPException(400, "请先搜索并选择有明确 TMDB 身份的影片")
        policy = getattr(store, "display_policy", None)
        state = policy.verdict(user["id"], media) if policy else "allowed"
        if state == "unverified" and hasattr(source, "recommendation_provenance"):
            try:
                records = await source.recommendation_provenance(
                    store.decrypt(user["token"]), media
                )
                policy.remember(user["id"], "movieclaw", records)
            except Exception:
                # 不泄露宿主异常、私有库名、地址或凭据；查询失败不缓存为排除。
                pass
            state = policy.verdict(user["id"], media)
        message = {
            "allowed": "可公开推荐",
            "excluded": "这部作品命中了你排除的媒体库，不能公开推荐",
            "unverified": "尚未核实这部作品的媒体库来源，请在设置中刷新媒体库与来源后重试",
        }[state]
        return {"allowed": state == "allowed", "status": state, "message": message}

    @app.post("/api/feed/eligibility")
    async def recommendation_eligibility(payload: MediaInput, request: Request):
        """只核对是否可分享，不发布动态、不导入已看记录、不修改排除设置。"""
        return await eligibility(account(request), payload.media.model_dump(mode="json"))

    @app.post("/api/feed", status_code=201)
    async def publish_recommendation(payload: PostInput, request: Request):
        user = account(request)
        user_id = user["id"]
        if not payload.publish_consent:
            raise HTTPException(400, "发布会向本服用户公开昵称和推荐内容，请确认后再发布")
        media = payload.media.model_dump(mode="json")
        result = await eligibility(user, media)
        if not result["allowed"]:
            raise HTTPException(403, result["message"])
        # 公共推荐不保存发布者的本地媒体编号、演示标识、文件路径或服务器地址。
        media.pop("media_item_id", None)
        media.pop("demo_id", None)
        with store.lock:
            result = store.db.execute(
                (
                    "INSERT INTO social_posts(user_id,media,body,rating,created_at) "
                    "VALUES(?,?,?,?,?)"
                ),
                (
                    user_id,
                    json.dumps(media, ensure_ascii=False),
                    payload.body,
                    payload.rating,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return JSONResponse(post_view(post_for(result.lastrowid), user_id), status_code=201)

    @app.get("/api/feed/{post_id}")
    async def get_recommendation(request: Request, post_id: int = Path(ge=1)):
        viewer_id = account(request)["id"]
        with store.lock:
            post = post_for(post_id)
            if not social.visible("post", post_id, viewer_id):
                raise HTTPException(404, "这条推荐暂不可访问")
            return post_view(post, viewer_id)

    @app.delete("/api/feed/{post_id}")
    async def delete_recommendation(request: Request, post_id: int = Path(ge=1)):
        user_id = account(request)["id"]
        with store.lock:
            post = post_for(post_id)
            if post["user_id"] != user_id:
                raise HTTPException(404, "没有这条本人推荐")
            store.execute("DELETE FROM social_posts WHERE id=?", (post_id,))
        return {"ok": True}

    @app.put("/api/feed/{post_id}/like")
    async def like_recommendation(payload: LikeInput, request: Request, post_id: int = Path(ge=1)):
        user_id = account(request)["id"]
        with store.lock:
            post = post_for(post_id)
            if not social.visible("post", post_id, user_id) or not social.allowed(
                user_id, post["user_id"]
            ):
                raise HTTPException(404, "这条推荐暂不可访问")
            if payload.liked:
                changed = store.db.execute(
                    "INSERT OR IGNORE INTO social_likes(post_id,user_id) VALUES(?,?)",
                    (post_id, user_id),
                ).rowcount
                if changed:
                    social.notify(
                        post["user_id"],
                        author(user_id, post["user_id"]),
                        "like",
                        post_id,
                        f"like:{post_id}:{user_id}",
                    )
            else:
                store.execute(
                    "DELETE FROM social_likes WHERE post_id=? AND user_id=?", (post_id, user_id)
                )
            count = store.one(
                "SELECT COUNT(*) AS total FROM social_likes WHERE post_id=?", (post_id,)
            )
        return {"liked": payload.liked, "like_count": count["total"] + social.extra_likes(post_id)}

    @app.get("/api/feed/{post_id}/comments")
    async def recommendation_comments(
        request: Request,
        post_id: int = Path(ge=1),
        before: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=20, ge=1, le=30),
    ):
        viewer_id = account(request)["id"]
        with store.lock:
            post = post_for(post_id)
            if not social.visible("post", post_id, viewer_id):
                raise HTTPException(404, "这条推荐暂不可访问")
            return social.comment_page(post, viewer_id, before=before, limit=limit)

    @app.post("/api/feed/{post_id}/comments", status_code=201)
    async def comment_recommendation(
        payload: CommentInput,
        request: Request,
        post_id: int = Path(ge=1),
    ):
        user_id = account(request)["id"]
        with store.lock:
            post = post_for(post_id)
            if not social.visible("post", post_id, user_id) or not social.allowed(
                user_id, post["user_id"]
            ):
                raise HTTPException(404, "这条推荐暂不可访问")
            result = store.db.execute(
                "INSERT INTO social_comments(post_id,user_id,body,created_at) VALUES(?,?,?,?)",
                (post_id, user_id, payload.body, datetime.now(UTC).isoformat()),
            )
            row = store.one("SELECT * FROM social_comments WHERE id=?", (result.lastrowid,))
            social.notify(
                post["user_id"],
                author(user_id, post["user_id"]),
                "comment",
                post_id,
                f"comment:{row['id']}",
            )
            return JSONResponse(comment_view(row, user_id), status_code=201)

    @app.delete("/api/feed/comments/{comment_id}")
    async def delete_recommendation_comment(request: Request, comment_id: int = Path(ge=1)):
        user_id = account(request)["id"]
        with store.lock:
            row = store.one(
                "SELECT id FROM social_comments WHERE id=? AND user_id=?", (comment_id, user_id)
            )
            if row is None:
                raise HTTPException(404, "没有这条本人评论")
            store.execute("DELETE FROM social_comments WHERE id=?", (comment_id,))
        return {"ok": True}

    return SimpleNamespace(
        social=social,
        post_for=post_for,
        post_view=post_view,
        media_model=media_model,
        LikeInput=LikeInput,
        CommentInput=CommentInput,
    )
