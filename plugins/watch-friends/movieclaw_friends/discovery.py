"""自愿加入的公共发现：多个可选入口、独立签名公开授权，不建立受信任连接。"""

import asyncio
import json
import time
import uuid
from contextlib import suppress

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .network import CAPACITY, LEASE, PAGE, PATH, canonical, decode, encode, node_id
from .peers import PeerClient, PeerError
from .store import digest

PURPOSE = "yingji-public-discovery-v1"


class DiscoveryConfig(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool = False
    serve: bool = False
    consent: bool = False


class SeedInput(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=60)
    code: str = Field(min_length=10, max_length=4096)
    consent: bool = False


class Discovery:
    """公共目录只保存明确再次确认的名片；好友请求仍交给原有双方同意协议。"""

    def __init__(self, network):
        self.net, self.store = network, network.store
        # 私网例外只用于注入的测试 transport；公开发现不沿用生产私网互联权限。
        self.peer = PeerClient(
            allow_private=network.peers.transport is not None,
            transport=network.peers.transport,
            transport_factory=network.peers.transport_factory,
        )
        self.lock = asyncio.Lock()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS discovery_config(
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                serve INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
            INSERT OR IGNORE INTO discovery_config(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS discovery_seeds(
                id TEXT PRIMARY KEY, node TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                descriptor TEXT NOT NULL, last_at REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS discovery_cards(
                node TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL,
                expires REAL NOT NULL, packet TEXT NOT NULL, via TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(node,actor));
            CREATE TABLE IF NOT EXISTS discovery_exports(actor TEXT PRIMARY KEY, revision INTEGER);
        """)

    def config(self):
        return self.store.one("SELECT * FROM discovery_config WHERE id=1")

    def active(self):
        return self.net.enabled() and bool(self.config()["enabled"])

    def descriptor(self):
        return {
            "origin": self.net.settings.public_url,
            "endpoint": self.net.endpoint() or "",
            "key": self.net.public,
        }

    def validate_descriptor(self, value):
        try:
            if not isinstance(value, dict) or set(value) != {"origin", "endpoint", "key"}:
                raise ValueError
            if not isinstance(value["key"], str) or len(decode(value["key"])) != 32:
                raise ValueError
            self.peer.validate(value["origin"])
            if self.net.settings.native or value["endpoint"]:
                self.peer.validate_endpoint(value["origin"], PATH, value["endpoint"])
            return value
        except (ValueError, TypeError, KeyError, PeerError):
            raise HTTPException(422, "公共节点连接信息无效；需公网 HTTPS 和兼容签名入口") from None

    def sign(self, payload):
        return {
            "key": self.net.public,
            "payload": payload,
            "signature": encode(self.net.key.sign(canonical(payload))),
        }

    def response(self, nonce, **values):
        return self.sign({"purpose": PURPOSE + ":response", "nonce": nonce, **values})

    def verify_response(self, result, descriptor, nonce):
        try:
            if set(result) != {"key", "payload", "signature"} or result["key"] != descriptor["key"]:
                raise ValueError
            self.net.verify_signature(result["key"], result["signature"], result["payload"])
            body = result["payload"]
            if body["purpose"] != PURPOSE + ":response" or body["nonce"] != nonce:
                raise ValueError
            return body
        except (KeyError, ValueError, TypeError, InvalidSignature):
            raise PeerError("公共节点身份或响应校验失败") from None

    async def request(self, descriptor, message):
        nonce = str(uuid.uuid4())
        result = await self.peer.request(
            descriptor["origin"],
            PATH,
            endpoint=descriptor["endpoint"] or None,
            require_signed=self.net.settings.native,
            data={"action": "discovery", "message": {**message, "nonce": nonce}},
        )
        return self.verify_response(result, descriptor, nonce)

    def seeds(self):
        return self.store.all("SELECT * FROM discovery_seeds ORDER BY name")

    def view(self, admin=False):
        config = self.config()
        result = {
            "enabled": bool(config["enabled"]),
            "serve": bool(config["serve"]),
            "active": self.active(),
            "seed_count": len(self.seeds()),
            "available": self.store.one(
                "SELECT COUNT(*) AS n FROM discovery_cards WHERE expires>? AND revoked=0",
                (time.time(),),
            )["n"],
        }
        if admin:
            result["seeds"] = [
                {k: row[k] for k in ("id", "name", "last_at", "error")} for row in self.seeds()
            ]
        return result

    async def local_packets(self):
        if not self.active():
            return []
        await self.net.verify_publishers()
        if not self.active():
            return []
        result = []
        for envelope in self.net.local_envelopes():
            value = envelope["payload"]
            row = self.net.preference(value["actor"])
            if not json.loads(row["options"]).get("discovery", False):
                continue
            result.append(
                self.sign(
                    {
                        "purpose": PURPOSE,
                        "descriptor": self.descriptor(),
                        **{k: value[k] for k in ("actor", "revision", "issued", "expires", "card")},
                    }
                )
            )
        return result

    def validate_packet(self, packet):
        try:
            if (
                not isinstance(packet, dict)
                or len(canonical(packet)) > 230000
                or set(packet) != {"key", "payload", "signature"}
            ):
                raise ValueError
            payload, key = packet["payload"], packet["key"]
            self.net.verify_signature(key, packet["signature"], payload)
            if not isinstance(payload, dict) or set(payload) != {
                "purpose",
                "descriptor",
                "actor",
                "revision",
                "issued",
                "expires",
                "card",
            }:
                raise ValueError
            descriptor = self.validate_descriptor(payload["descriptor"])
            if payload["purpose"] != PURPOSE or descriptor["key"] != key:
                raise ValueError
            if not isinstance(payload["actor"], str) or not 1 <= len(payload["actor"]) <= 64:
                raise ValueError
            if type(payload["revision"]) is not int or payload["revision"] < 1:
                raise ValueError
            if (
                type(payload["issued"]) not in (int, float)
                or type(payload["expires"]) not in (int, float)
                or not time.time() - LEASE <= payload["issued"] <= time.time() + 30
                or not time.time() < payload["expires"] <= payload["issued"] + LEASE
            ):
                raise ValueError
            card = payload["card"]
            if card is not None and self.net.clean_card(card) != card:
                raise ValueError
            node = node_id(key)
            if self.store.one(
                "SELECT id FROM network_peers WHERE node=? AND state='blocked'", (node,)
            ):
                raise ValueError
            return node, payload
        except (KeyError, ValueError, TypeError, InvalidSignature, HTTPException, OverflowError):
            raise HTTPException(422, "公共名片授权、签名或有效期无效") from None

    def receive(self, packet, via):
        node, payload = self.validate_packet(packet)
        if node == self.net.node:
            return
        actor = payload["actor"]
        previous = self.store.one(
            "SELECT * FROM discovery_cards WHERE node=? AND actor=?", (node, actor)
        )
        if previous and (
            previous["revision"] > payload["revision"]
            or (previous["revoked"] and previous["revision"] == payload["revision"])
        ):
            return
        if (
            previous
            and previous["revision"] == payload["revision"]
            and payload["card"] is not None
            and previous["expires"] >= payload["expires"]
        ):
            return
        if (
            not previous
            and self.store.one("SELECT COUNT(*) AS n FROM discovery_cards")["n"] >= CAPACITY
        ):
            raise HTTPException(409, "公共发现目录容量已满，请稍后重试")
        revoked = payload["card"] is None
        self.store.execute(
            "INSERT INTO discovery_cards VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(node,actor) DO UPDATE SET "
            "revision=excluded.revision,expires=excluded.expires,packet=excluded.packet,"
            "via=excluded.via,revoked=excluded.revoked",
            (
                node,
                actor,
                payload["revision"],
                time.time() + 86400 if revoked else payload["expires"],
                self.store.encrypt(canonical(packet).decode()),
                via,
                revoked,
            ),
        )

    def cached(self):
        result = []
        for row in self.store.all(
            "SELECT * FROM discovery_cards WHERE revoked=0 AND expires>?", (time.time(),)
        ):
            try:
                packet = json.loads(self.store.decrypt(row["packet"]))
                self.validate_packet(packet)
            except (HTTPException, ValueError, InvalidToken):
                continue
            result.append((row, packet))
        return result

    def directory(self, user):
        if not self.active() or not self.net.preference(user)["browse"]:
            return []
        result = []
        for row, packet in self.cached():
            actor = "network:" + digest(row["node"] + ":" + row["actor"])
            if self.store.one(
                "SELECT 1 FROM network_blocks WHERE user_id=? AND actor=?", (user, actor)
            ):
                continue
            result.append(
                {
                    "id": actor,
                    **packet["payload"]["card"],
                    "expires_at": row["expires"],
                    "revision": row["revision"],
                    "discovered": True,
                    "recommendation_keys": [],
                    "hops": 0,
                }
            )
        return result

    def find(self, user, actor):
        if not any(
            row["id"] == actor and row.get("discovered") for row in self.net.directory(user)
        ):
            raise HTTPException(404, "这张公共名片已撤下、过期或被屏蔽")
        for row, packet in self.cached():
            if actor == "network:" + digest(row["node"] + ":" + row["actor"]):
                return row, packet
        raise HTTPException(404, "没有仍有效的公共名片")

    async def friend_code(self, user, actor):
        row, packet = self.find(user, actor)
        descriptor = packet["payload"]["descriptor"]
        # 在发送申请人标识前先验证来源实际持有名片签名私钥，防止伪造回调地址。
        await self.request(descriptor, {"kind": "ping"})
        self.find(user, actor)
        request = self.sign(
            {
                "purpose": PURPOSE + ":contact",
                "target": row["actor"],
                "actor": user,
                "issued": time.time(),
                "request_id": str(uuid.uuid4()),
            }
        )
        result = await self.request(
            packet["payload"]["descriptor"], {"kind": "contact", "request": request}
        )
        self.find(user, actor)  # 网络请求期间撤回或屏蔽后，不再发起好友申请。
        code = result.get("invite_code")
        if not isinstance(code, str) or not code.startswith("mcf1.") or len(code) > 8192:
            raise PeerError("对方未返回兼容的好友申请入口")
        try:
            invitation = json.loads(decode(code[5:]))
            if self.peer.validate(invitation["origin"]) != descriptor["origin"]:
                raise ValueError
            if self.net.settings.native:
                self.peer.validate_endpoints(descriptor["origin"], invitation["endpoints"])
        except (ValueError, TypeError, KeyError, PeerError):
            raise PeerError("好友申请入口与名片来源不一致") from None
        return code

    async def retract(self, events, targets=None):
        packets = []
        for event in events:
            if event.get("key") != self.net.public:
                continue
            value = event["payload"]
            if not self.store.one(
                "SELECT 1 FROM discovery_exports WHERE actor=?", (value["actor"],)
            ):
                continue
            packets.append(
                self.sign(
                    {
                        "purpose": PURPOSE,
                        "descriptor": self.descriptor(),
                        **{k: value[k] for k in ("actor", "revision", "issued", "expires")},
                        "card": None,
                    }
                )
            )
        if not packets:
            return
        for seed in self.seeds() if targets is None else targets:
            with suppress(PeerError, HTTPException, TimeoutError):
                descriptor = json.loads(self.store.decrypt(seed["descriptor"]))
                for offset in range(0, len(packets), PAGE):
                    await asyncio.wait_for(
                        self.request(
                            descriptor,
                            {"kind": "announce", "cards": packets[offset : offset + PAGE]},
                        ),
                        timeout=4,
                    )

    def withdraw_exports(self):
        """撤回后推进来源修订，重新启用或重新接入时不会被旧撤回墓碑阻挡。"""
        events = [
            self.net.revocation(r["actor"], r["revision"])
            for r in self.store.all("SELECT * FROM discovery_exports")
        ]
        for row in self.store.all("SELECT actor,revision FROM discovery_exports"):
            self.store.execute(
                "UPDATE network_preferences SET revision=revision+1 "
                "WHERE user_id=? AND revision<=?",
                (row["actor"], row["revision"]),
            )
        return events

    async def refresh(self):
        if not self.active() or self.lock.locked():
            return
        async with self.lock:
            config = self.config()
            packets = await self.local_packets()

            def current(seed):
                return (
                    self.active()
                    and self.config()["revision"] == config["revision"]
                    and self.store.one("SELECT 1 FROM discovery_seeds WHERE id=?", (seed["id"],))
                )

            for seed in self.seeds():
                try:
                    if not current(seed):
                        return
                    descriptor = json.loads(self.store.decrypt(seed["descriptor"]))
                    for offset in range(0, len(packets), PAGE):
                        if not current(seed):
                            return
                        batch = [
                            p
                            for p in packets[offset : offset + PAGE]
                            if self.net.preference(p["payload"]["actor"])["revision"]
                            == p["payload"]["revision"]
                            and json.loads(
                                self.net.preference(p["payload"]["actor"])["options"]
                            ).get("discovery")
                            and self.net.local_card(p["payload"]["actor"]) == p["payload"]["card"]
                        ]
                        if not batch:
                            continue
                        # 先记录可能已发出的一轮，响应丢失仍可发送撤回通知。
                        for packet in batch:
                            value = packet["payload"]
                            self.store.execute(
                                "INSERT INTO discovery_exports VALUES(?,?) ON CONFLICT(actor) "
                                "DO UPDATE SET revision=excluded.revision",
                                (value["actor"], value["revision"]),
                            )
                        await self.request(descriptor, {"kind": "announce", "cards": batch})
                    for offset in range(0, CAPACITY, PAGE):
                        if not current(seed):
                            return
                        result = await self.request(
                            descriptor, {"kind": "directory", "offset": offset}
                        )
                        if not self.active() or self.config()["revision"] != config["revision"]:
                            return
                        if not self.store.one(
                            "SELECT 1 FROM discovery_seeds WHERE id=?", (seed["id"],)
                        ):
                            return
                        cards = result.get("cards")
                        if not isinstance(cards, list) or len(cards) > PAGE:
                            raise PeerError("公共节点目录格式无效")
                        for packet in cards:
                            self.receive(packet, seed["node"])
                        if result.get("more") is not True:
                            break
                    self.store.execute(
                        "UPDATE discovery_seeds SET last_at=?,error=NULL WHERE id=?",
                        (time.time(), seed["id"]),
                    )
                except (PeerError, HTTPException, ValueError, KeyError) as exc:
                    message = exc.detail if isinstance(exc, HTTPException) else str(exc)
                    self.store.execute(
                        "UPDATE discovery_seeds SET error=? WHERE id=?", (message[:200], seed["id"])
                    )
            self.store.execute("DELETE FROM discovery_cards WHERE expires<=?", (time.time(),))

    async def wire(self, message, request):
        if not self.active():
            raise HTTPException(403, "公共发现未开启")
        if len(canonical(message)) > 950000:
            raise HTTPException(413, "公共发现请求过大")
        self.net.limited("public-discovery", 512)
        self.net.limited("discovery:" + (request.client.host if request.client else "unknown"), 128)
        nonce, kind = message.get("nonce"), message.get("kind")
        if not isinstance(nonce, str) or len(nonce) > 64 or not nonce:
            raise HTTPException(422, "发现请求标识无效")
        if kind == "ping":
            return self.response(nonce, serving=bool(self.config()["serve"]))
        if kind == "contact":
            proof = message.get("request", {})
            try:
                value = proof["payload"]
                if set(proof) != {"key", "payload", "signature"} or len(decode(proof["key"])) != 32:
                    raise ValueError
                self.net.verify_signature(proof["key"], proof["signature"], value)
                if (
                    set(value) != {"purpose", "target", "actor", "issued", "request_id"}
                    or not all(
                        isinstance(value[k], str) and 1 <= len(value[k]) <= 64
                        for k in ("target", "actor", "request_id")
                    )
                    or type(value["issued"]) not in (int, float)
                    or value["purpose"] != PURPOSE + ":contact"
                    or not time.time() - 60 <= value["issued"] <= time.time() + 30
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError, InvalidSignature):
                raise HTTPException(422, "好友请求签名无效") from None
            cards = await self.local_packets()
            if not any(card["payload"]["actor"] == value["target"] for card in cards):
                raise HTTPException(404, "对方已撤下公共发现名片")
            from .network_interactions import actor_id

            actor = actor_id(node_id(proof["key"]), value["actor"])
            if self.store.one(
                "SELECT 1 FROM network_blocks WHERE user_id=? AND actor=?", (value["target"], actor)
            ) or self.net.community.blocked(value["target"], actor):
                raise HTTPException(403, "来源已被屏蔽")
            # 此处只返回申请入口，不建立好友关系或提供任何私人观看数据。
            if self.store.one(
                "SELECT id FROM network_peers WHERE node=? AND state='blocked'",
                (node_id(proof["key"]),),
            ):
                raise HTTPException(403, "来源已被屏蔽")
            user = self.store.one("SELECT * FROM users WHERE id=?", (value["target"],))
            return self.response(nonce, invite_code=self.net.interactions.invitation(user))
        if not self.config()["serve"]:
            raise HTTPException(403, "此服务器未提供公共发现节点服务")
        if kind == "announce":
            cards = message.get("cards")
            if not isinstance(cards, list) or not 1 <= len(cards) <= PAGE:
                raise HTTPException(422, "公告数量无效")
            for packet in cards:
                self.receive(packet, "public")
            return self.response(nonce, accepted=len(cards))
        if kind == "directory":
            offset = message.get("offset", 0)
            if type(offset) is not int or not 0 <= offset < CAPACITY:
                raise HTTPException(422, "目录分页无效")
            packets = {(node_id(p["key"]), p["payload"]["actor"]): p for _, p in self.cached()}
            for p in await self.local_packets():
                packets[(self.net.node, p["payload"]["actor"])] = p
            if not self.active() or not self.config()["serve"]:
                raise HTTPException(409, "公共发现设置已改变")
            values = [packets[k] for k in sorted(packets)][:CAPACITY]
            return self.response(
                nonce, cards=values[offset : offset + PAGE], more=len(values) > offset + PAGE
            )
        raise HTTPException(422, "不支持的公共发现动作")

    def register(self, app):
        @app.get("/api/discovery/settings")
        async def discovery_settings(request: Request):
            self.net.account(request)
            return self.view()

        @app.get("/api/discovery/admin")
        async def discovery_admin(request: Request):
            self.net.account(request, admin=True)
            return self.view(admin=True)

        @app.put("/api/discovery/admin")
        async def discovery_admin_save(payload: DiscoveryConfig, request: Request):
            self.net.account(request, admin=True)
            old = self.config()
            if (
                payload.enabled != bool(old["enabled"]) or payload.serve != bool(old["serve"])
            ) and not payload.consent:
                raise HTTPException(422, "请确认公共发现的网络地址与公开范围提示")
            if payload.serve and not payload.enabled:
                raise HTTPException(422, "提供公共节点服务前需开启公共发现")
            if payload.enabled and not self.net.enabled():
                raise HTTPException(409, "请先开启服务器互联")
            self.store.execute(
                "UPDATE discovery_config SET enabled=?,serve=?,revision=revision+1 WHERE id=1",
                (payload.enabled, payload.serve),
            )
            if not payload.enabled and old["enabled"]:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self.retract(self.withdraw_exports()),
                        timeout=5,
                    )
            if not payload.enabled:
                self.store.execute("DELETE FROM discovery_cards")
            return self.view(admin=True)

        @app.post("/api/discovery/admin/code")
        async def discovery_node_code(request: Request):
            self.net.account(request, admin=True)
            if not self.active() or not self.config()["serve"]:
                raise HTTPException(409, "请先明确启用公共发现节点服务")
            descriptor = self.validate_descriptor(self.descriptor())
            return {"code": "mcd1." + encode(canonical(descriptor))}

        @app.post("/api/discovery/admin/seeds")
        async def discovery_seed_add(payload: SeedInput, request: Request):
            self.net.account(request, admin=True)
            if not payload.consent or not self.active():
                raise HTTPException(422, "请先开启公共发现，并确认向此节点发送已授权的公开名片")
            try:
                if not payload.code.startswith("mcd1."):
                    raise ValueError
                descriptor = self.validate_descriptor(json.loads(decode(payload.code[5:])))
            except (ValueError, TypeError):
                raise HTTPException(422, "公共节点接入码无效") from None
            node = node_id(descriptor["key"])
            if node == self.net.node or self.store.one(
                "SELECT id FROM discovery_seeds WHERE node=?", (node,)
            ):
                raise HTTPException(409, "不能重复添加或连接自己")
            if len(self.seeds()) >= 4:
                raise HTTPException(409, "最多配置四个公共发现入口")
            revision = self.config()["revision"]
            result = await self.request(descriptor, {"kind": "ping"})
            if not self.active() or self.config()["revision"] != revision:
                raise HTTPException(409, "公共发现设置已改变，请重新添加入口")
            if len(self.seeds()) >= 4 or self.store.one(
                "SELECT id FROM discovery_seeds WHERE node=?", (node,)
            ):
                raise HTTPException(409, "公共入口已添加或容量已满")
            if result.get("serving") is not True:
                raise HTTPException(409, "对方未启用公共节点服务")
            self.store.execute(
                "INSERT INTO discovery_seeds(id,node,name,descriptor) VALUES(?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    node,
                    payload.name.strip() or "公共入口",
                    self.store.encrypt(canonical(descriptor).decode()),
                ),
            )
            return self.view(admin=True)

        @app.delete("/api/discovery/admin/seeds/{seed_id}")
        async def discovery_seed_delete(seed_id: str, request: Request):
            self.net.account(request, admin=True)
            row = self.store.one("SELECT * FROM discovery_seeds WHERE id=?", (seed_id,))
            if not row:
                raise HTTPException(404, "没有这个公共入口")
            self.store.execute("DELETE FROM discovery_seeds WHERE id=?", (seed_id,))
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.retract(
                        self.withdraw_exports(),
                        [row],
                    ),
                    timeout=5,
                )
            self.store.execute("DELETE FROM discovery_cards WHERE via=?", (row["node"],))
            return self.view(admin=True)

        @app.post("/api/discovery/refresh")
        async def discovery_refresh(request: Request):
            self.net.account(request, admin=True)
            await self.refresh()
            return self.view(admin=True)
