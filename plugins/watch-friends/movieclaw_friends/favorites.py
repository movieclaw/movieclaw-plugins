"""可反复编辑的私人喜欢列表；保存不会自动发布或更新广场上的已授权快照。"""

import json

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .store import Store, digest


def favorite_items(store: Store, user_id: str) -> list:
    """仅由可信本人账号取其私人列表，调用方不得使用客户端提供的成员编号。"""
    row = store.one("SELECT data FROM user_favorites WHERE user_id=?", (user_id,))
    return json.loads(row["data"]) if row else []


def favorites_digest(items: list) -> str:
    """预览凭据只绑定快照摘要，避免把五百部影片塞进有长度上限的凭据。"""
    return digest(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def register_favorites(app: FastAPI, store: Store, user_for, media_model) -> None:
    """喜欢列表只在插件私有库保存；公开选择与快照由 profile 的确认流程管理。"""

    class SaveFavorites(BaseModel):
        items: list[media_model] = Field(max_length=500)

    def response(user_id: str) -> dict:
        user = store.one(
            "SELECT discoverable,share_favorites,public_favorites FROM users WHERE id=?", (user_id,)
        )
        return {
            "items": favorite_items(store, user_id),
            "public_items": json.loads(user["public_favorites"])
            if user["discoverable"] and user["share_favorites"]
            else [],
        }

    @app.get("/api/favorites")
    async def get_favorites(request: Request):
        return response(user_for(request)["id"])

    @app.put("/api/favorites")
    async def save_favorites(payload: SaveFavorites, request: Request):
        user_id = user_for(request)["id"]
        items = [item.model_dump(mode="json") for item in payload.items]
        with store.lock:
            store.execute(
                "INSERT INTO user_favorites(user_id,data) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
                (user_id, json.dumps(items, ensure_ascii=False)),
            )
            # 与预览使用同一版本：编辑期间的旧确认不能覆盖后来保存的喜欢列表。
            store.execute(
                "UPDATE users SET privacy_revision=privacy_revision+1 WHERE id=?", (user_id,)
            )
            return response(user_id)
