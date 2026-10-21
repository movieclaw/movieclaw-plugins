"""互联握手恢复和凭据两阶段轮换；响应丢失仅重试同一意图，不扩大授权。"""

import asyncio
import json
import time
import uuid

from fastapi import HTTPException, Request

from .peers import PeerError
from .store import digest


class NetworkManagement:
    """邀请和轮换意图加密保存；提交成功立即拒绝旧凭据。"""

    def __init__(self, network):
        self.network, self.store, self.locks = network, network.store, {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS network_handshakes(
                peer_id TEXT PRIMARY KEY,join_payload TEXT,rotation_id TEXT,
                pending_token TEXT,phase TEXT,expires REAL);
        """)

    def peer(self, peer_id):
        row = self.store.one("SELECT * FROM network_peers WHERE id=?", (peer_id,))
        if not row:
            raise HTTPException(404, "没有这个互联服务器")
        return row

    async def retry(self, peer_id):
        async with self.locks.setdefault(peer_id, asyncio.Lock()):
            row = self.peer(peer_id)
            saved = self.store.one("SELECT * FROM network_handshakes WHERE peer_id=?", (peer_id,))
            if not self.network.enabled() or row["state"] != "accepted":
                raise HTTPException(409, "请先启用互联连接")
            try:
                if saved and saved["join_payload"]:
                    result = await self.network.request(
                        row, json.loads(self.store.decrypt(saved["join_payload"]))
                    )
                    if result.get("key") != row["public_key"] or result.get("state") not in {
                        "pending",
                        "accepted",
                    }:
                        raise PeerError("对方互联身份不匹配")
                    if self.peer(peer_id)["revision"] != row["revision"]:
                        raise HTTPException(409, "连接授权已变化")
                    self.store.execute(
                        "UPDATE network_handshakes SET join_payload=NULL WHERE peer_id=?",
                        (peer_id,),
                    )
                elif saved and saved["phase"] in {"outgoing_prepare", "outgoing_commit"}:
                    await self.rotate_steps(row, saved)
                else:
                    result = await self.network.request(row, {"action": "status"})
                    if result.get("key") != row["public_key"]:
                        raise PeerError("对方互联身份不匹配")
                self.store.execute("UPDATE network_peers SET error=NULL WHERE id=?", (peer_id,))
            except PeerError:
                self.store.execute(
                    "UPDATE network_peers SET error=? WHERE id=?",
                    ("连接结果尚未确认，可重试连接；不会重复创建或丢失待批准申请", peer_id),
                )
            return self.network.admin_view()

    async def rotate_steps(self, row, saved):
        token, number = self.store.decrypt(saved["pending_token"]), saved["rotation_id"]
        if saved["expires"] <= time.time():
            raise HTTPException(409, "轮换恢复期限已过，请移除连接后重新配对")
        if saved["phase"] == "outgoing_prepare":
            result = await self.network.request(
                row, {"action": "rotate_prepare", "request_id": number, "token": token}
            )
            if result != {"key": row["public_key"], "rotation_id": number}:
                raise PeerError("对方未确认凭据轮换")
            if self.peer(row["id"])["revision"] != row["revision"]:
                raise HTTPException(409, "连接授权已变化")
            with self.store.lock:
                self.store.execute(
                    "UPDATE network_peers SET token=?,token_hash=?,revision=revision+1 WHERE id=?",
                    (self.store.encrypt(token), digest(token), row["id"]),
                )
                self.store.execute(
                    "UPDATE network_handshakes SET phase='outgoing_commit' WHERE peer_id=?",
                    (row["id"],),
                )
            row = self.peer(row["id"])
        result = await self.network.request(row, {"action": "rotate_commit", "request_id": number})
        if result != {"key": row["public_key"], "rotation_id": number}:
            raise PeerError("对方未确认新凭据")
        self.store.execute("DELETE FROM network_handshakes WHERE peer_id=?", (row["id"],))

    def pending_peer(self, token, number):
        for saved in self.store.all(
            (
                "SELECT * FROM network_handshakes WHERE phase='incoming' AND rotation_id=? "
                "AND expires>?"
            ),
            (number, time.time()),
        ):
            if digest(self.store.decrypt(saved["pending_token"])) == digest(token):
                return self.peer(saved["peer_id"])
        return None

    def forget(self, row):
        self.store.execute(
            "DELETE FROM network_cache WHERE via=? OR node=?", (row["id"], row["node"])
        )
        self.store.execute("DELETE FROM network_handshakes WHERE peer_id=?", (row["id"],))
        self.store.execute("DELETE FROM network_peers WHERE id=?", (row["id"],))

    def wire(self, row, payload, presented_token=""):
        if payload.action == "rotate_commit":
            saved = self.store.one("SELECT * FROM network_handshakes WHERE peer_id=?", (row["id"],))
            if (
                not saved
                or not saved["pending_token"]
                or digest(presented_token) != digest(self.store.decrypt(saved["pending_token"]))
            ):
                raise HTTPException(401, "请使用新凭据提交轮换")
        if payload.action == "disconnect":
            self.forget(row)
            return {"ok": True}
        if not self.network.enabled() or row["state"] != "accepted":
            raise HTTPException(403, "互联授权已停止")
        saved = self.store.one("SELECT * FROM network_handshakes WHERE peer_id=?", (row["id"],))
        if not 16 <= len(payload.request_id) <= 64:
            raise HTTPException(422, "轮换请求标识无效")
        if payload.action == "rotate_prepare":
            if len(payload.token) < 32:
                raise HTTPException(422, "轮换凭据无效")
            if (
                saved
                and saved["phase"] in {"incoming", "outgoing_prepare", "outgoing_commit"}
                and (
                    saved["rotation_id"] != payload.request_id
                    or self.store.decrypt(saved["pending_token"]) != payload.token
                )
            ):
                raise HTTPException(409, "已有尚未完成的凭据轮换，请先恢复原轮换")
            if (
                not saved
                or not saved["phase"]
                or saved["phase"] == "committed"
                and saved["rotation_id"] != payload.request_id
            ):
                self.store.execute(
                    (
                        "INSERT INTO "
                        "network_handshakes(peer_id,rotation_id,pending_token,phase,expires) "
                        "VALUES(?,?,?,'incoming',?) ON CONFLICT(peer_id) DO UPDATE SET "
                        "rotation_id=excluded.rotation_id,pending_token=excluded.pending_token,phase=excluded.phase,expires=excluded.expires"
                    ),
                    (
                        row["id"],
                        payload.request_id,
                        self.store.encrypt(payload.token),
                        time.time() + 86400,
                    ),
                )
        else:
            if (
                not saved
                or saved["rotation_id"] != payload.request_id
                or saved["phase"] not in {"incoming", "committed"}
                or saved["expires"] <= time.time()
            ):
                raise HTTPException(409, "没有本次待确认轮换")
            token = self.store.decrypt(saved["pending_token"])
            if saved["phase"] == "incoming":
                self.store.execute(
                    "UPDATE network_peers SET token=?,token_hash=?,revision=revision+1 WHERE id=?",
                    (self.store.encrypt(token), digest(token), row["id"]),
                )
                self.store.execute(
                    "UPDATE network_handshakes SET phase='committed' WHERE peer_id=?", (row["id"],)
                )
        return {"key": self.network.public, "rotation_id": payload.request_id}

    def register(self, app):
        @app.post("/api/network/admin/peers/{peer_id}/retry")
        async def retry_connection(peer_id: str, request: Request):
            self.network.account(request, admin=True)
            return await self.retry(peer_id)

        @app.post("/api/network/admin/peers/{peer_id}/rotate")
        async def rotate_connection(peer_id: str, request: Request):
            self.network.account(request, admin=True)
            row = self.peer(peer_id)
            if not self.network.enabled() or row["state"] != "accepted":
                raise HTTPException(409, "请先启用互联连接")
            saved = self.store.one("SELECT * FROM network_handshakes WHERE peer_id=?", (peer_id,))
            if saved and saved["join_payload"]:
                raise HTTPException(409, "请先恢复连接申请")
            if saved and saved["phase"] == "incoming":
                raise HTTPException(409, "对方正在轮换，请等待其完成")
            if not saved or saved["phase"] not in {"outgoing_prepare", "outgoing_commit"}:
                self.store.execute(
                    (
                        "INSERT INTO "
                        "network_handshakes(peer_id,rotation_id,pending_token,phase,expires) "
                        "VALUES(?,?,?,'outgoing_prepare',?) ON CONFLICT(peer_id) DO UPDATE SET "
                        "rotation_id=excluded.rotation_id,pending_token=excluded.pending_token,phase=excluded.phase,expires=excluded.expires"
                    ),
                    (
                        peer_id,
                        str(uuid.uuid4()),
                        self.store.encrypt(self.store.secret()),
                        time.time() + 86400,
                    ),
                )
            return await self.retry(row["id"])

        @app.delete("/api/network/admin/peers/{peer_id}")
        async def remove_connection(peer_id: str, request: Request):
            self.network.account(request, admin=True)
            async with self.locks.setdefault(peer_id, asyncio.Lock()):
                row = self.peer(peer_id)
                notified = True
                try:
                    await self.network.request(row, {"action": "disconnect"})
                except PeerError:
                    notified = False
                self.forget(row)
            return {**self.network.admin_view(), "remote_notified": notified}
