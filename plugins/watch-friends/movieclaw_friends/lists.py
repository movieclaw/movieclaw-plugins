"""插件私有片单：不创建或修改宿主片单，只有主动发布的快照会提供给好友。"""

import json
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .store import Store


def register_lists(app: FastAPI, store: Store, user_for, media_model, collection_model) -> None:
    """复用宿主已确认的本人身份与现有分享存储，隔离本人与其他用户的私有列表。

    ``plugin_lists.data`` 只存媒体白名单数组。发布到 ``collections`` 时使用负数
    ``source_id``，与宿主片单的正数编号分开；删除或撤回立即提升隐私版本并清除
    关系缓存，让已经在途的读取也能发现授权变化。此处没有宿主操作或外部请求。
    """

    # 同版本迁移只增加可空封面，不重写原有片单或已发布快照。
    if "cover" not in {row[1] for row in store.db.execute("PRAGMA table_info(plugin_lists)")}:
        store.db.execute("ALTER TABLE plugin_lists ADD COLUMN cover TEXT")

    class NewList(BaseModel):
        """只接收片单内容；账号、共享状态和列表编号均由服务端决定。"""

        name: str = Field(min_length=1, max_length=120)
        description: str = Field(default="", max_length=500)
        items: list[media_model] = Field(default_factory=list, max_length=500)
        cover: media_model | None = None

        @model_validator(mode="after")
        def cover_from_list(self):
            if self.cover is not None and (
                not self.cover.cover_path
                or not self.cover.cover_type
                or not any(
                    item.tmdb_id == self.cover.tmdb_id
                    and item.kind == self.cover.kind
                    and item.tmdb_id
                    and item.kind in ("movie", "tv")
                    for item in self.items
                )
            ):
                raise ValueError("封面必须来自此片单中的影片，并选择有效图片")
            return self

        @field_validator("name")
        @classmethod
        def meaningful_name(cls, value: str) -> str:
            value = value.strip()
            if not value:
                raise ValueError("请填写片单名称")
            return value

    class AddItem(BaseModel):
        media: media_model

    class EditList(NewList):
        """完整编辑只接受打开时的私有版本号，避免覆盖另一个页面已保存的修改。"""

        revision: int = Field(ge=0)

    def owned(list_id: int, user_id: str) -> dict:
        item = store.one("SELECT * FROM plugin_lists WHERE id=? AND user_id=?", (list_id, user_id))
        if item is None:
            raise HTTPException(404, "没有这个自建片单")
        return item

    def view(item: dict, shared: bool) -> dict:
        return {
            "id": item["id"],
            "name": item["name"],
            "description": item["description"],
            "items": json.loads(item["data"]),
            "shared": shared,
            "revision": item["revision"],
            **({"cover": json.loads(item["cover"])} if item.get("cover") else {}),
        }

    def revoke(user_id: str, list_id: int) -> None:
        store.execute(
            "DELETE FROM collections WHERE user_id=? AND source_id=?", (user_id, -list_id)
        )
        store.execute("UPDATE users SET privacy_revision=privacy_revision+1 WHERE id=?", (user_id,))
        store.execute(
            "UPDATE relationships SET cache=NULL WHERE user_id=? OR target_id=?", (user_id, user_id)
        )

    @app.get("/api/lists")
    async def list_plugin_lists(request: Request):
        user_id = user_for(request)["id"]
        rows = store.all(
            "SELECT plugin_lists.*, EXISTS(SELECT 1 FROM collections WHERE "
            "collections.user_id=plugin_lists.user_id AND collections.source_id=-plugin_lists.id) "
            "AS shared FROM plugin_lists WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        )
        return {"lists": [view(row, bool(row["shared"])) for row in rows]}

    @app.post("/api/lists", status_code=201)
    async def create_plugin_list(payload: NewList, request: Request):
        user_id = user_for(request)["id"]
        items = [item.model_dump(mode="json") for item in payload.items]
        with store.lock:
            count = store.one(
                "SELECT COUNT(*) AS total FROM plugin_lists WHERE user_id=?", (user_id,)
            )
            if count["total"] >= 50:
                raise HTTPException(400, "最多创建五十个片单，请先删除不需要的片单")
            result = store.db.execute(
                "INSERT INTO plugin_lists(user_id,name,description,data,created_at,cover) "
                "VALUES(?,?,?,?,?,?)",
                (
                    user_id,
                    payload.name,
                    payload.description,
                    json.dumps(items, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    payload.cover.model_dump_json() if payload.cover else None,
                ),
            )
            item = owned(result.lastrowid, user_id)
        # 原生路由复用 endpoint 时仍保留创建响应码，不依赖独立应用的路由元数据。
        return JSONResponse(view(item, False), status_code=201)

    @app.put("/api/lists/{list_id}")
    async def edit_plugin_list(payload: EditList, request: Request, list_id: int = Path(ge=1)):
        user_id = user_for(request)["id"]
        with store.lock:
            item = owned(list_id, user_id)
            if item["revision"] != payload.revision:
                raise HTTPException(409, "片单已在别处修改，请重新打开后编辑")
            cover = payload.cover
            if "cover" not in payload.model_fields_set and item.get("cover"):
                cover = media_model.model_validate_json(item["cover"])
                if not any(
                    m.tmdb_id == cover.tmdb_id and m.kind == cover.kind for m in payload.items
                ):
                    cover = None
            store.execute(
                "UPDATE plugin_lists SET name=?,description=?,data=?,cover=?,revision=revision+1 "
                "WHERE id=? AND user_id=?",
                (
                    payload.name,
                    payload.description,
                    json.dumps(
                        [m.model_dump(mode="json") for m in payload.items], ensure_ascii=False
                    ),
                    cover.model_dump_json() if cover else None,
                    list_id,
                    user_id,
                ),
            )
            shared = store.one(
                "SELECT 1 FROM collections WHERE user_id=? AND source_id=?", (user_id, -list_id)
            )
            return view(owned(list_id, user_id), bool(shared))

    @app.post("/api/lists/{list_id}/items")
    async def add_plugin_list_item(payload: AddItem, request: Request, list_id: int = Path(ge=1)):
        """把推荐作品加入本人私有片单；已发布的快照仍需本人重新确认分享。"""
        user_id = user_for(request)["id"]
        media = payload.media.model_dump(mode="json")
        if (
            not media.get("tmdb_id")
            or media["tmdb_id"] <= 0
            or media["kind"] not in {"movie", "tv"}
        ):
            raise HTTPException(400, "请选择有明确 TMDB 身份的影片")
        with store.lock:
            item = owned(list_id, user_id)
            items = json.loads(item["data"])
            if not any(
                entry.get("tmdb_id") == media["tmdb_id"] and entry.get("kind") == media["kind"]
                for entry in items
            ):
                if len(items) >= 500:
                    raise HTTPException(400, "每个片单最多五百部作品")
                items.append(media)
                store.execute(
                    "UPDATE plugin_lists SET data=?,revision=revision+1 WHERE id=? AND user_id=?",
                    (json.dumps(items, ensure_ascii=False), list_id, user_id),
                )
            shared = store.one(
                "SELECT 1 FROM collections WHERE user_id=? AND source_id=?", (user_id, -list_id)
            )
            return view(owned(list_id, user_id), bool(shared))

    @app.post("/api/lists/{list_id}/share")
    async def share_plugin_list(request: Request, list_id: int = Path(ge=1)):
        user = user_for(request)
        user_id = user["id"]
        with store.lock:
            item = owned(list_id, user_id)
            existing = store.one(
                "SELECT source_id FROM collections WHERE user_id=? AND source_id=?",
                (user_id, -list_id),
            )
            count = store.one(
                "SELECT COUNT(*) AS total FROM collections WHERE user_id=?", (user_id,)
            )
            if count["total"] >= 50 and not existing:
                raise HTTPException(400, "最多同时分享五十个片单，请先取消部分分享")
            latest = store.one("SELECT privacy_revision FROM users WHERE id=?", (user_id,))
            if latest["privacy_revision"] != user["privacy_revision"]:
                raise HTTPException(409, "共享设置已经变化，请重新确认片单分享")
            snapshot = collection_model.model_validate(
                {
                    "id": -list_id,
                    "name": item["name"],
                    "description": item["description"],
                    "items": json.loads(item["data"]),
                    **({"cover": json.loads(item["cover"])} if item.get("cover") else {}),
                }
            ).model_dump(mode="json")
            store.execute(
                "INSERT INTO collections(user_id,source_id,name,data) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,source_id) "
                "DO UPDATE SET name=excluded.name,data=excluded.data",
                (user_id, -list_id, item["name"], json.dumps(snapshot, ensure_ascii=False)),
            )
        return {"ok": True}

    @app.delete("/api/lists/{list_id}/share")
    async def unshare_plugin_list(request: Request, list_id: int = Path(ge=1)):
        user_id = user_for(request)["id"]
        with store.lock:
            owned(list_id, user_id)
            revoke(user_id, list_id)
        return {"ok": True}

    @app.delete("/api/lists/{list_id}")
    async def delete_plugin_list(request: Request, list_id: int = Path(ge=1)):
        user_id = user_for(request)["id"]
        with store.lock:
            owned(list_id, user_id)
            revoke(user_id, list_id)
            store.execute("DELETE FROM plugin_lists WHERE id=? AND user_id=?", (list_id, user_id))
        return {"ok": True}
