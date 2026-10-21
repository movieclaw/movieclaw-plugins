"""受控影友互联：管理员双向批准、用户独立预览授权、短期签名名片与两跳转发。

只交换插件私有投影，不转发好友凭据、服务器地址、播放地址或原始档案。
每一跳有签名证明；转发不能改变来源内容、有效期或用户允许的最大跳数。
"""

import asyncio
import base64
import json
import time
import uuid
from contextlib import suppress
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from .favorites import favorite_items
from .media import sanitize_media
from .peers import PeerError
from .store import digest
from .timeline import merge_history
from .upstream import UpstreamError
from .watch_archive import safe_avatar

PATH = "/federation/directory"
LEASE = 300
PAGE = 4
CAPACITY = 200
NOTICE = (
    "公开名片将发送给管理员已批准的互联服务器。允许转发时，最多经过两次传递。"
    "页面隐藏服务器地址，但通信对端和相关管理员仍可能获知地址，这不是匿名服务。"
    "关闭后立即停止本服发布，并通知已连接服务器撤回；遵守协议的页面最迟五分钟隐藏。"
    "截图、人工保存或不遵守协议的副本无法强制收回。"
)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def node_id(key):
    return digest(key)


class Options(BaseModel):
    """浏览、发布与转发分别授权；没有任何从旧广场设置继承的默认开启项。"""

    model_config = {"extra": "forbid"}
    publish: bool = False
    forward: bool = False
    discovery: bool = False
    favorites: bool = False
    recommendations: bool = False
    recent: bool = False


class Publication(Options):
    consent: bool = False
    preview_token: str = Field(default="", max_length=2048)


class AdminOptions(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool
    consent: bool = False


class Connect(BaseModel):
    model_config = {"extra": "forbid"}
    code: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=60)
    consent: bool = False


class PeerChange(BaseModel):
    model_config = {"extra": "forbid"}
    state: Literal["accepted", "paused", "blocked"]


class Wire(BaseModel):
    """公开区只接受固定动作及有界参数；关系凭据绝不作为公共名片内容。"""

    model_config = {"extra": "forbid"}
    action: Literal[
        "join",
        "challenge",
        "status",
        "directory",
        "withdraw",
        "rotate_prepare",
        "rotate_commit",
        "disconnect",
        "message",
        "encryption_key",
        "discovery",
    ]
    invite: str = Field(default="", max_length=128)
    origin: str = Field(default="", max_length=256)
    endpoint: str = Field(default="", max_length=2048)
    key: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=60)
    nonce: str = Field(default="", max_length=64)
    token: str = Field(default="", max_length=128)
    offset: int = Field(default=0, ge=0, le=CAPACITY)
    events: list[dict] = Field(default_factory=list, max_length=4)
    request_id: str = Field(default="", max_length=64)
    message: dict = Field(default_factory=dict)


class Network:
    """所有持久状态属于插件；关闭时拒绝输出，短期缓存不拥有续签权。"""

    def __init__(self, app, store, settings, peers, community, user_for, source, task_factory):
        self.app, self.store, self.settings = app, store, settings
        self.peers, self.community, self.user_for, self.source = peers, community, user_for, source
        self.task_factory, self.task, self.logger = task_factory, None, None
        self.refresh_lock = asyncio.Lock()
        self.publisher_lock = asyncio.Lock()
        self.publisher_checks = {}
        self.rates = {}
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS network_config (
                id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                private_key TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS network_invites (
                token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS network_peers (
                id TEXT PRIMARY KEY, node TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                origin TEXT NOT NULL, endpoint TEXT NOT NULL, public_key TEXT NOT NULL,
                token TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, nonce TEXT NOT NULL,
                direction TEXT NOT NULL, state TEXT NOT NULL, remote_state TEXT,
                revision INTEGER NOT NULL DEFAULT 0, last_at REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS network_preferences (
                user_id TEXT PRIMARY KEY REFERENCES users(id), browse INTEGER NOT NULL DEFAULT 0,
                options TEXT NOT NULL DEFAULT '{}', snapshot TEXT,
                revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS network_cache (
                node TEXT NOT NULL, actor TEXT NOT NULL, via TEXT NOT NULL,
                envelope TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(node,actor,via));
            CREATE TABLE IF NOT EXISTS network_versions (
                node TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL,
                revoked INTEGER NOT NULL, expires REAL NOT NULL, PRIMARY KEY(node,actor));
            CREATE TABLE IF NOT EXISTS network_blocks (
                user_id TEXT NOT NULL REFERENCES users(id), actor TEXT NOT NULL,
                nickname TEXT NOT NULL, PRIMARY KEY(user_id,actor));
        """)
        if not store.one("SELECT id FROM network_config WHERE id=1"):
            key = Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            store.execute(
                "INSERT INTO network_config(id,private_key) VALUES(1,?)",
                (store.encrypt(encode(key)),),
            )
        key = store.one("SELECT private_key FROM network_config WHERE id=1")["private_key"]
        self.key = Ed25519PrivateKey.from_private_bytes(decode(store.decrypt(key)))
        self.public = encode(
            self.key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        self.node = node_id(self.public)

    def enabled(self):
        return bool(self.store.one("SELECT enabled FROM network_config WHERE id=1")["enabled"])

    def account(self, request, admin=False):
        user = self.user_for(request)
        if user["role"] == "demo" or (admin and user["role"] != "admin"):
            raise HTTPException(
                403, "只有本服管理员可以管理互联服务器" if admin else "请使用真实账号"
            )
        return user

    def preference(self, user):
        row = self.store.one("SELECT * FROM network_preferences WHERE user_id=?", (user,))
        return row or {"browse": 0, "options": "{}", "snapshot": None, "revision": 0}

    def setting_view(self, user):
        row = self.preference(user)
        return {
            "server_enabled": self.enabled(),
            "peer_count": self.store.one(
                "SELECT COUNT(*) AS n FROM network_peers WHERE "
                "state='accepted' AND remote_state='accepted'"
            )["n"],
            "discovery": self.discovery.view(),
            "browse": bool(row["browse"]),
            "options": Options(**json.loads(row["options"])).model_dump(),
            "published": bool(row["snapshot"]),
            "notice": NOTICE,
            "blocks": self.store.all(
                "SELECT actor,nickname FROM network_blocks WHERE user_id=?", (user,)
            ),
        }

    def admin_view(self):
        return {
            "enabled": self.enabled(),
            "node": self.node,
            "endpoint_ready": bool(self.endpoint()),
            "peers": self.store.all(
                "SELECT p.id,node,name,state,remote_state,last_at,error,h.phase AS "
                "credential_phase,"
                "(h.join_payload IS NOT NULL) AS awaiting_receipt "
                "FROM network_peers p LEFT JOIN network_handshakes h ON h.peer_id=p.id "
                "ORDER BY name"
            ),
            "capacity": CAPACITY,
        }

    def endpoint(self):
        return self.settings.network_endpoint

    def check_endpoint(self, origin, endpoint):
        origin = self.peers.validate(origin)
        if self.settings.native or endpoint:
            self.peers.validate_endpoint(origin, PATH, endpoint)
        if origin == self.settings.public_url:
            raise HTTPException(400, "不能连接本服务器")
        return origin

    def limited(self, key, maximum):
        """凭据验证前后分别限流，保存有界桶，错误不回显对方地址或正文。"""
        now = time.time()
        self.rates = {k: v for k, v in self.rates.items() if v[0] > now - 60}
        start, count = self.rates.get(key, (now, 0))
        if count >= maximum or len(self.rates) > 512:
            raise HTTPException(429, "互联请求较多，请稍后重试")
        self.rates[key] = (start, count + 1)

    async def request(self, row, data):
        return await self.peers.request(
            self.store.decrypt(row["origin"]),
            PATH,
            token=self.store.decrypt(row["token"]),
            endpoint=self.store.decrypt(row["endpoint"]) or None,
            require_signed=self.settings.native,
            data=data,
        )

    def signed(self, payload):
        return {
            "key": self.public,
            "payload": payload,
            "signature": encode(self.key.sign(canonical(payload))),
            "proofs": [],
        }

    @staticmethod
    def verify_signature(key, signature, value):
        Ed25519PublicKey.from_public_bytes(decode(key)).verify(decode(signature), canonical(value))

    def validate_envelope(self, envelope, sender):
        """验证根签名及不可删减的逐跳签名链，最后一跳必须属于当前已批准的连接。"""
        try:
            if set(envelope) != {"key", "payload", "signature", "proofs"}:
                raise ValueError
            key, payload, proofs = envelope["key"], envelope["payload"], envelope["proofs"]
            if not isinstance(key, str) or len(key) != 43 or not isinstance(payload, dict):
                raise ValueError
            node = node_id(key)
            if not isinstance(proofs, list) or len(proofs) > 1:
                raise ValueError
            self.verify_signature(key, envelope["signature"], payload)
            if set(payload) != {"actor", "revision", "issued", "expires", "max_hops", "card"}:
                raise ValueError
            actor = payload["actor"]
            if not isinstance(actor, str) or not actor or len(actor) > 64:
                raise ValueError
            if type(payload["revision"]) is not int or payload["revision"] < 1:
                raise ValueError
            now = time.time()
            if (
                type(payload["issued"]) not in (int, float)
                or type(payload["expires"]) not in (int, float)
                or not now - LEASE <= payload["issued"] <= now + 30
                or not now < payload["expires"] <= min(payload["issued"] + LEASE, now + LEASE)
                or type(payload["max_hops"]) is not int
                or payload["max_hops"] not in (1, 2)
                or len(proofs) + 1 > payload["max_hops"]
            ):
                raise ValueError
            path = [node]
            previous = {k: envelope[k] for k in ("key", "payload", "signature")}
            for proof in proofs:
                if set(proof) != {"key", "signature"}:
                    raise ValueError
                self.verify_signature(
                    proof["key"],
                    proof["signature"],
                    {"previous": digest(canonical(previous).decode())},
                )
                path.append(node_id(proof["key"]))
                previous = {"previous": previous, "proof": proof}
            if path[-1] != sender or self.node in path or len(set(path)) != len(path):
                raise ValueError
            if self.store.one(
                "SELECT id FROM network_peers WHERE node=? AND state='blocked'", (node,)
            ):
                raise ValueError
            card = self.clean_card(payload["card"]) if payload["card"] is not None else None
            if card is not None and card != payload["card"]:
                raise ValueError
            return node, actor, payload, card
        except (ValueError, TypeError, KeyError, InvalidSignature, OverflowError):
            raise PeerError("互联名片签名、路径或有效期无效") from None

    @staticmethod
    def clean_media(value):
        if not isinstance(value, dict) or len(canonical(value)) > 4096:
            raise ValueError
        result = sanitize_media(value)
        result.pop("media_item_id", None)
        if (
            not isinstance(result.get("title"), str)
            or len(result["title"]) > 300
            or result.get("kind") not in ("movie", "tv")
        ):
            raise ValueError
        for field in ("tmdb_id", "year", "season_number", "episode_number"):
            value = result.get(field)
            if value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
                raise ValueError
        for field in ("episode_title", "imdb_id"):
            value = result.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 300):
                raise ValueError
        return result

    def clean_card(self, value):
        if not isinstance(value, dict) or len(canonical(value)) > 200000:
            raise ValueError
        if set(value) != {
            "nickname",
            "bio",
            "avatar_data",
            "favorites",
            "recommendations",
            "history",
        }:
            raise ValueError
        if not isinstance(value["nickname"], str) or not 1 <= len(value["nickname"]) <= 60:
            raise ValueError
        if not isinstance(value["bio"], str) or len(value["bio"]) > 300:
            raise ValueError
        result = {
            "nickname": value["nickname"],
            "bio": value["bio"],
            "avatar_data": safe_avatar(value["avatar_data"]),
        }
        for field, limit in (("favorites", 12), ("recommendations", 6), ("history", 5)):
            rows = value[field]
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError
            result[field] = []
            for row in rows:
                if field == "favorites":
                    result[field].append(self.clean_media(row))
                else:
                    clean = {"media": self.clean_media(row["media"])}
                    if field == "recommendations":
                        rating, body = row.get("rating"), row.get("body", "")
                        if (
                            (
                                rating is not None
                                and (type(rating) is not int or not 1 <= rating <= 5)
                            )
                            or not isinstance(body, str)
                            or len(body) > 2000
                        ):
                            raise ValueError
                        if "key" in row:
                            key = row["key"]
                            if (
                                not isinstance(key, str)
                                or len(key) != 64
                                or any(c not in "0123456789abcdef" for c in key)
                            ):
                                raise ValueError
                            clean["key"] = key
                        clean.update(rating=rating, body=body)
                    else:
                        clean["ended_at"] = str(row.get("ended_at") or "")[:64]
                    result[field].append(clean)
        return result

    async def projection(self, user, options):
        """只读取一次候选记录；展示字段与排除所需的本地来源证明一一对应。"""
        profile = self.community.public_profile(user["id"], user["id"])
        thumbnail = self.store.one(
            "SELECT avatar_data FROM account_profiles WHERE user_id=?", (user["id"],)
        )
        guard = self.store.display_policy.guard(user["id"])

        def usable(value):
            try:
                self.clean_media(value.get("media", value))
                return True
            except (ValueError, TypeError):
                return False

        favorites = (
            [v for v in favorite_items(self.store, user["id"]) if guard(v) and usable(v)][:12]
            if options.favorites
            else []
        )
        posts, post_sources = [], []
        if options.recommendations:
            for row in self.store.all(
                "SELECT * FROM social_posts WHERE user_id=? ORDER BY id DESC LIMIT 100",
                (user["id"],),
            ):
                if self.community.visible("post", row["id"], user["id"]) and usable(
                    json.loads(row["media"])
                ):
                    media = json.loads(row["media"])
                    posts.append(
                        {
                            "media": self.clean_media(media),
                            "body": row["body"],
                            "rating": row["rating"],
                            "key": digest(self.node + ":" + user["id"] + ":" + str(row["id"])),
                        }
                    )
                    post_sources.append({**media, "post_id": row["id"]})
                if len(posts) == 6:
                    break
        history = []
        if options.recent:
            snapshot = await self.source.snapshot(
                self.store.decrypt(user["token"]), user["member_id"], False, True
            )
            imported = self.app.state.watch_archive.shareable_history(user["id"])
            history = self.store.display_policy.snapshot(
                user["id"], {"history": merge_history(snapshot["history"], imported)}
            )["history"]
            history = [v for v in history if usable(v)][:5]
        card = self.clean_card(
            {
                "nickname": user["nickname"],
                "bio": profile["bio"],
                "avatar_data": safe_avatar(thumbnail["avatar_data"]) if thumbnail else None,
                "favorites": [self.clean_media(v) for v in favorites],
                "recommendations": posts,
                "history": [
                    {"media": self.clean_media(v["media"]), "ended_at": v.get("ended_at")}
                    for v in history
                ],
            }
        )
        return {
            "card": card,
            "provenance": {
                "favorites": favorites,
                "recommendations": post_sources,
                "history": history,
            },
        }

    def filtered_snapshot(self, user_id, snapshot):
        """每次续签重查当前库排除及删除/审核；不自动添加后来导入或发布的内容。"""
        guard = self.store.display_policy.guard(user_id)
        # 投影中的本地来源仅在私有快照另存，绝不进入网络签名字段。
        raw = snapshot["card"]
        card = {**raw, "favorites": [], "recommendations": [], "history": []}
        for field in ("favorites", "recommendations", "history"):
            for item, provenance in zip(raw[field], snapshot["provenance"][field], strict=True):
                if not guard(provenance):
                    continue
                if field == "recommendations" and not self.community.visible(
                    "post", provenance["post_id"], user_id
                ):
                    continue
                card[field].append(item)
        return self.clean_card(card)

    async def preview(self, user, options):
        if options.discovery and not self.discovery.active():
            raise HTTPException(409, "管理员尚未开启公共发现，请先配置可选公共入口")
        row = self.preference(user["id"])
        snapshot = await self.projection(user, options)
        card = snapshot["card"]
        fingerprint = digest(canonical(snapshot).decode())
        token = self.store.encrypt(
            json.dumps(
                {
                    "user": user["id"],
                    "revision": row["revision"],
                    "scope_revision": user["privacy_revision"],
                    "options": options.model_dump(),
                    "digest": fingerprint,
                }
            )
        )
        # 预览单独留存，更新不会扩大旧授权；确认只认该账号当前修订。
        self.previews = {k: v for k, v in self.previews.items() if v[2] > time.time()}
        self.previews[user["id"]] = (token, snapshot, time.time() + LEASE)
        return {
            "card": card,
            "preview_token": token,
            "notice": NOTICE
            + "联网公开的同一份名片也将在本服广场展示。"
            + (
                "另已选择公共发现：此名片可被公共节点及其参与者获取和继续分发，"
                "不受可信服务器两次传递限制。私人观看数据仍需双方同意；"
                "关闭后停止续期，离线缓存最多五分钟隐藏。"
                if options.discovery
                else ""
            ),
        }

    @staticmethod
    def publisher_binding(row):
        return digest(
            canonical([row["username"], row["role"], row["member_id"], row["token"]]).decode()
        )

    def published_users(self):
        return self.store.all(
            "SELECT p.*,u.username,u.role,u.member_id,u.token FROM network_preferences p "
            "JOIN users u ON u.id=p.user_id WHERE snapshot IS NOT NULL LIMIT ?",
            (CAPACITY,),
        )

    async def verify_publishers(self):
        """被停用、删除或复用编号的成员不能继续给旧名片续期。

        身份读取在锁外的业务 await 中完成，结果最多缓存一分钟。名片租期仍以
        最后有效核验为上限，而非每次响应都把陈旧授权向后延长五分钟。
        """
        async with self.publisher_lock:
            rows = self.published_users()
            self.publisher_checks = {
                k: v for k, v in self.publisher_checks.items() if k in {r["user_id"] for r in rows}
            }
            for row in rows:
                key = self.publisher_binding(row)
                cached = self.publisher_checks.get(row["user_id"])
                if cached and cached[0] == key and time.time() - cached[1] < 60:
                    continue
                allowed = False
                try:
                    identity = await self.source.identity(self.store.decrypt(row["token"]))
                    allowed = tuple(
                        identity.get(k) for k in ("username", "role", "member_id")
                    ) == tuple(row[k] for k in ("username", "role", "member_id"))
                except (UpstreamError, PeerError, HTTPException):
                    pass
                self.publisher_checks[row["user_id"]] = (key, time.time(), allowed)

    def local_envelopes(self):
        result = []
        for row in self.published_users():
            checked = self.publisher_checks.get(row["user_id"])
            if (
                not checked
                or not checked[2]
                or checked[0] != self.publisher_binding(row)
                or time.time() - checked[1] >= 60
            ):
                continue
            options = Options(**json.loads(row["options"]))
            if row["role"] == "demo" or not options.publish:
                continue
            card = self.filtered_snapshot(row["user_id"], json.loads(row["snapshot"]))
            now = time.time()
            result.append(
                self.signed(
                    {
                        "actor": row["user_id"],
                        "revision": row["revision"],
                        "issued": now,
                        "expires": min(now + LEASE, checked[1] + LEASE),
                        "max_hops": 2 if options.forward else 1,
                        "card": card,
                    }
                )
            )
        return result

    def local_card(self, user):
        """联网公开包含本服公开；仅复用已确认的过滤快照，不改动本服或好友授权。"""
        if self.enabled():
            for envelope in self.local_envelopes():
                if envelope["payload"]["actor"] == user:
                    return envelope["payload"]["card"]
        return None

    async def catalog(self, target):
        if not self.enabled():
            return []
        await self.verify_publishers()
        if not self.enabled():
            return []
        result = self.local_envelopes()
        for row in self.store.all(
            "SELECT c.* FROM network_cache c JOIN network_peers p ON p.id=c.via WHERE "
            "c.expires>? AND p.state='accepted' AND p.remote_state='accepted' ORDER BY "
            "c.expires DESC",
            (time.time(),),
        ):
            envelope = json.loads(row["envelope"])
            payload = envelope["payload"]
            if payload["max_hops"] != 2 or envelope["proofs"] or row["node"] == target:
                continue
            if not self.latest(row["node"], row["actor"], payload):
                continue
            if self.store.one(
                "SELECT id FROM network_peers WHERE node=? AND state='blocked'", (row["node"],)
            ):
                continue
            previous = {k: envelope[k] for k in ("key", "payload", "signature")}
            proof = {
                "key": self.public,
                "signature": encode(
                    self.key.sign(canonical({"previous": digest(canonical(previous).decode())}))
                ),
            }
            result.append({**envelope, "proofs": [proof]})
        unique = {}
        for envelope in result:
            identity = (node_id(envelope["key"]), envelope["payload"]["actor"])
            old = unique.get(identity)
            if not old or old["payload"]["revision"] < envelope["payload"]["revision"]:
                unique[identity] = envelope
        return sorted(unique.values(), key=lambda e: (node_id(e["key"]), e["payload"]["actor"]))[
            :CAPACITY
        ]

    def latest(self, node, actor, payload):
        row = self.store.one(
            "SELECT * FROM network_versions WHERE node=? AND actor=?", (node, actor)
        )
        return not row or (not row["revoked"] and payload["revision"] >= row["revision"])

    def receive(self, envelope, sender, via):
        node, actor, payload, card = self.validate_envelope(envelope, sender)
        old = self.store.one(
            "SELECT * FROM network_versions WHERE node=? AND actor=?", (node, actor)
        )
        revoked = card is None
        if old and (
            payload["revision"] < old["revision"]
            or (old["revoked"] and payload["revision"] == old["revision"])
        ):
            return False
        self.store.execute(
            "INSERT INTO network_versions VALUES(?,?,?,?,?) ON CONFLICT(node,actor) DO "
            "UPDATE SET "
            "revision=excluded.revision,revoked=excluded.revoked,expires=excluded.expires",
            (node, actor, payload["revision"], int(revoked), time.time() + 86400),
        )
        if revoked:
            self.store.execute("DELETE FROM network_cache WHERE node=? AND actor=?", (node, actor))
        else:
            self.store.execute(
                "INSERT INTO network_cache VALUES(?,?,?,?,?) ON CONFLICT(node,actor,via) DO "
                "UPDATE SET envelope=excluded.envelope,expires=excluded.expires",
                (node, actor, via, json.dumps(envelope, ensure_ascii=False), payload["expires"]),
            )
        return True

    async def notify(self, events, *, exclude=None):
        """尽力推送撤回，不让某台离线节点阻塞本人的关闭操作。"""

        async def send(peer):
            for start in range(0, len(events), 4):
                try:
                    await asyncio.wait_for(
                        self.request(
                            peer, {"action": "withdraw", "events": events[start : start + 4]}
                        ),
                        timeout=4,
                    )
                except (PeerError, TimeoutError):
                    return

        with suppress(TimeoutError):
            await asyncio.wait_for(self.discovery.retract(events), timeout=5)
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        send(peer)
                        for peer in self.store.all(
                            "SELECT * FROM network_peers WHERE state='accepted'"
                        )
                        if peer["id"] != exclude
                    )
                ),
                timeout=5,
            )

    def revocation(self, actor, revision):
        now = time.time()
        return self.signed(
            {
                "actor": actor,
                "revision": revision,
                "issued": now,
                "expires": now + LEASE,
                "max_hops": 2,
                "card": None,
            }
        )

    async def refresh_peer(self, row):
        revision = row["revision"]
        try:
            status = await self.request(row, {"action": "status"})
            remote = status.get("state")
            if status.get("key") != row["public_key"] or remote not in {
                "accepted",
                "pending",
                "paused",
                "blocked",
            }:
                raise PeerError("互联身份或协议不兼容")
            current = self.store.one("SELECT * FROM network_peers WHERE id=?", (row["id"],))
            if (
                not current
                or current["revision"] != revision
                or current["state"] != "accepted"
                or not self.enabled()
            ):
                return
            self.store.execute(
                "UPDATE network_peers SET remote_state=?,last_at=?,error=NULL WHERE id=?",
                (remote, time.time(), row["id"]),
            )
            if remote != "accepted":
                self.store.execute("DELETE FROM network_cache WHERE via=?", (row["id"],))
                return
            envelopes = []
            for offset in range(0, CAPACITY, PAGE):
                result = await self.request(row, {"action": "directory", "offset": offset})
                batch = result.get("cards")
                if not isinstance(batch, list) or len(batch) > PAGE:
                    raise PeerError("互联名片分页不兼容")
                envelopes.extend(batch)
                if not result.get("more"):
                    break
            current = self.store.one("SELECT * FROM network_peers WHERE id=?", (row["id"],))
            if (
                not current
                or current["revision"] != revision
                or current["state"] != "accepted"
                or not self.enabled()
            ):
                return
            with self.store.lock:
                # 一页任何签名无效都不替换旧数据；旧数据不能超过来源五分钟租期。
                for envelope in envelopes:
                    self.validate_envelope(envelope, row["node"])
                self.store.execute("DELETE FROM network_cache WHERE via=?", (row["id"],))
                for envelope in envelopes:
                    self.receive(envelope, row["node"], row["id"])
        except PeerError as exc:
            self.store.execute("UPDATE network_peers SET error=? WHERE id=?", (str(exc), row["id"]))

    async def refresh(self):
        if self.refresh_lock.locked() or not self.enabled():
            return
        async with self.refresh_lock:
            for peer in self.store.all("SELECT * FROM network_peers WHERE state='accepted'"):
                await self.refresh_peer(peer)
            self.store.execute("DELETE FROM network_cache WHERE expires<=?", (time.time(),))
            self.store.execute("DELETE FROM network_versions WHERE expires<=?", (time.time(),))
            self.store.execute("DELETE FROM network_invites WHERE expires<=?", (time.time(),))
            await self.discovery.refresh()

    def start(self):
        if self.task is None:
            self.task = (
                self.task_factory(self.loop())
                if self.task_factory
                else asyncio.create_task(self.loop())
            )

    async def loop(self):
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.logger:
                    self.logger.warning("联网广场刷新失败，将在下轮重试。")
            await asyncio.sleep(60)

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def directory(self, user):
        if not self.enabled() or not self.preference(user)["browse"]:
            return []
        items = {}
        # 本人也能核对已发布的具体内容；沿用续签核验与库过滤，不读取私人档案。
        for envelope in self.local_envelopes():
            p = envelope["payload"]
            if p["actor"] != user:
                continue
            actor = "network:" + digest(self.node + ":" + user)
            items[actor] = {
                "id": actor,
                **self.clean_card(p["card"]),
                "expires_at": p["expires"],
                "revision": p["revision"],
                "hops": 0,
                "is_self": True,
            }
        for row in self.store.all(
            "SELECT c.* FROM network_cache c JOIN network_peers p ON p.id=c.via WHERE "
            "c.expires>? AND p.state='accepted' AND p.remote_state='accepted' ORDER BY "
            "c.expires DESC",
            (time.time(),),
        ):
            envelope = json.loads(row["envelope"])
            p = envelope["payload"]
            actor = "network:" + digest(row["node"] + ":" + row["actor"])
            if (
                not self.latest(row["node"], row["actor"], p)
                or self.store.one(
                    "SELECT 1 FROM network_blocks WHERE user_id=? AND actor=?", (user, actor)
                )
                or self.store.one(
                    "SELECT id FROM network_peers WHERE node=? AND state='blocked'", (row["node"],)
                )
            ):
                continue
            old = items.get(actor)
            if old and old["revision"] >= p["revision"]:
                continue
            items[actor] = {
                "id": actor,
                **self.clean_card(p["card"]),
                "expires_at": p["expires"],
                "revision": p["revision"],
                "hops": len(envelope["proofs"]) + 1,
                "recommendation_keys": [
                    v.get("key") or digest(canonical(v).decode())
                    for v in p["card"]["recommendations"]
                ],
            }
        for card in self.discovery.directory(user):
            items.setdefault(card["id"], card)
        return sorted(items.values(), key=lambda v: (not v.get("is_self"), v["nickname"]))[
            :CAPACITY
        ]


def register_network(app, store, settings, peers, community, user_for, source, task_factory):
    network = Network(app, store, settings, peers, community, user_for, source, task_factory)
    app.state.network = network
    store.network = network
    network.previews = {}
    from .network_management import NetworkManagement

    network.management = NetworkManagement(network)
    network.management.register(app)
    from .network_mail import NetworkMail

    network.mail = NetworkMail(network)
    from .discovery import Discovery

    network.discovery = Discovery(network)
    network.discovery.register(app)

    @app.get("/api/network/settings")
    async def network_settings(request: Request):
        return network.setting_view(network.account(request)["id"])

    class Browse(BaseModel):
        model_config = {"extra": "forbid"}
        enabled: bool

    @app.put("/api/network/browse")
    async def network_browse(payload: Browse, request: Request):
        user = network.account(request)["id"]
        store.execute(
            "INSERT INTO network_preferences(user_id,browse) VALUES(?,?) ON "
            "CONFLICT(user_id) DO UPDATE SET browse=excluded.browse",
            (user, payload.enabled),
        )
        return network.setting_view(user)

    @app.post("/api/network/preview")
    async def network_preview(payload: Options, request: Request):
        user = network.account(request)
        if not payload.publish:
            raise HTTPException(400, "请选择发布名片；撤下无需预览")
        return await network.preview(user, payload)

    @app.put("/api/network/publication")
    async def network_publication(payload: Publication, request: Request):
        user = network.account(request)
        options = Options(**payload.model_dump(include=set(Options.model_fields)))
        row = network.preference(user["id"])
        snapshot = None
        if payload.publish:
            if payload.discovery and not network.discovery.active():
                raise HTTPException(409, "公共发现已关闭，请重新预览发布范围")
            if not network.enabled():
                raise HTTPException(409, "管理员尚未开启影友互联")
            try:
                proof = json.loads(store.cipher.decrypt(payload.preview_token.encode(), ttl=300))
                preview = network.previews.get(user["id"])
                if (
                    not payload.consent
                    or not preview
                    or preview[0] != payload.preview_token
                    or proof["user"] != user["id"]
                    or proof["revision"] != row["revision"]
                    or proof["scope_revision"] != user["privacy_revision"]
                    or proof["options"] != options.model_dump()
                    or proof["digest"] != digest(canonical(preview[1]).decode())
                ):
                    raise ValueError
                snapshot = preview[1]
                if network.filtered_snapshot(user["id"], snapshot) != snapshot["card"]:
                    raise ValueError
            except (InvalidToken, ValueError, KeyError):
                raise HTTPException(409, "内容或授权已变化，请重新预览确认") from None
        with store.lock:
            if (
                not row["snapshot"]
                and snapshot
                and store.one(
                    "SELECT COUNT(*) AS n FROM network_preferences WHERE snapshot IS NOT NULL"
                )["n"]
                >= CAPACITY
            ):
                raise HTTPException(409, "本服联网名片已达二百张上限，请联系管理员")
            revision = row["revision"] + 1
            store.execute(
                "INSERT INTO network_preferences(user_id,options,snapshot,revision) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "options=excluded.options,snapshot=excluded.snapshot,revision=excluded.revision",
                (
                    user["id"],
                    json.dumps(options.model_dump()),
                    json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
                    revision,
                ),
            )
            network.previews.pop(user["id"], None)
        # 更新也先撤回旧版；新的名片由下一次获取提供，乱序旧响应不能覆盖。
        if row["snapshot"]:
            await network.notify(
                [network.revocation(user["id"], revision - 1 if snapshot else revision)]
            )
        return network.setting_view(user["id"])

    @app.get("/api/network/directory")
    async def network_directory(request: Request):
        user = network.account(request)["id"]
        if network.enabled() and network.preference(user)["browse"]:
            await network.verify_publishers()
        return {"users": network.directory(user), "server_enabled": network.enabled()}

    class Block(BaseModel):
        model_config = {"extra": "forbid"}
        actor: str = Field(pattern=r"^network:[0-9a-f]{64}$")

    @app.post("/api/network/blocks")
    async def network_block(payload: Block, request: Request):
        user = network.account(request)["id"]
        target = next((v for v in network.directory(user) if v["id"] == payload.actor), None)
        if not target:
            target = store.one(
                "SELECT nickname FROM network_public_actors WHERE id=?", (payload.actor,)
            )
        if not target:
            raise HTTPException(404, "这张名片已撤下或不可见")
        if target.get("is_self"):
            raise HTTPException(400, "不能屏蔽自己的联网名片")
        store.execute(
            "INSERT OR IGNORE INTO network_blocks VALUES(?,?,?)",
            (user, payload.actor, target["nickname"]),
        )
        return {"ok": True}

    @app.delete("/api/network/blocks/{actor}")
    async def network_unblock(actor: str, request: Request):
        user = network.account(request)["id"]
        store.execute("DELETE FROM network_blocks WHERE user_id=? AND actor=?", (user, actor))
        return {"ok": True}

    @app.get("/api/network/admin")
    async def network_admin(request: Request):
        network.account(request, admin=True)
        return network.admin_view()

    @app.put("/api/network/admin")
    async def network_admin_save(payload: AdminOptions, request: Request):
        network.account(request, admin=True)
        if payload.enabled and (
            (not network.enabled() and not payload.consent)
            or (settings.native and not network.endpoint())
        ):
            raise HTTPException(400, "请配置宿主外部地址，并确认参与互联的网络地址提示")
        store.execute(
            "UPDATE network_config SET enabled=?,revision=revision+1 WHERE id=1", (payload.enabled,)
        )
        if not payload.enabled:
            store.execute("DELETE FROM network_cache")
            events = []
            for row in store.all(
                "SELECT user_id,revision FROM network_preferences WHERE snapshot IS NOT NULL"
            ):
                revision = row["revision"] + 1
                store.execute(
                    "UPDATE network_preferences SET revision=? WHERE user_id=?",
                    (revision, row["user_id"]),
                )
                events.append(network.revocation(row["user_id"], revision - 1))
            await network.notify(events)
        return network.admin_view()

    @app.post("/api/network/admin/invite")
    async def network_invite(request: Request):
        network.account(request, admin=True)
        if not network.enabled():
            raise HTTPException(409, "请先开启服务器互联")
        token = store.secret()
        store.execute("DELETE FROM network_invites")
        store.execute(
            "INSERT INTO network_invites VALUES(?,?)", (digest(token), time.time() + 1800)
        )
        code = {
            "v": 1,
            "origin": settings.public_url,
            "endpoint": network.endpoint(),
            "key": network.public,
            "invite": token,
        }
        return {"code": "mcn1." + encode(canonical(code)), "expires_in": 1800}

    @app.post("/api/network/admin/connect")
    async def network_connect(payload: Connect, request: Request):
        network.account(request, admin=True)
        if not network.enabled() or not payload.consent:
            raise HTTPException(400, "请先开启互联，并确认与此服务器交换网络地址")
        try:
            if not payload.code.startswith("mcn1.") or not payload.name.strip():
                raise ValueError
            code = json.loads(decode(payload.code[5:]))
            if set(code) != {"v", "origin", "endpoint", "key", "invite"} or code["v"] != 1:
                raise ValueError
            Ed25519PublicKey.from_public_bytes(decode(code["key"]))
            origin = network.check_endpoint(code["origin"], code["endpoint"])
            node = node_id(code["key"])
            if node == network.node or len(code["invite"]) > 128:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "互联连接码无效，请管理员重新生成") from None
        if store.one("SELECT id FROM network_peers WHERE node=?", (node,)):
            raise HTTPException(409, "该服务器已有连接，请在已有连接中管理")
        if store.one("SELECT COUNT(*) AS n FROM network_peers")["n"] >= 8:
            raise HTTPException(409, "第一版最多连接八台可信服务器")
        token, nonce, peer_id = store.secret(), store.secret(), str(uuid.uuid4())
        store.execute(
            "INSERT INTO "
            "network_peers(id,node,name,origin,endpoint,public_key,token,token_hash,"
            "nonce,direction,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,'outgoing','accepted')",
            (
                peer_id,
                node,
                payload.name.strip(),
                store.encrypt(origin),
                store.encrypt(code["endpoint"]),
                code["key"],
                store.encrypt(token),
                digest(token),
                nonce,
            ),
        )
        join_payload = {
            "action": "join",
            "invite": code["invite"],
            "origin": settings.public_url,
            "endpoint": network.endpoint(),
            "key": network.public,
            "name": "待确认的影友服务器",
            "nonce": nonce,
            "token": token,
        }
        store.execute(
            "INSERT INTO network_handshakes(peer_id,join_payload) VALUES(?,?)",
            (peer_id, store.encrypt(json.dumps(join_payload))),
        )
        await network.management.retry(peer_id)
        return network.admin_view()

    @app.patch("/api/network/admin/peers/{peer_id}")
    async def network_peer_change(peer_id: str, payload: PeerChange, request: Request):
        network.account(request, admin=True)
        row = store.one("SELECT * FROM network_peers WHERE id=?", (peer_id,))
        if not row:
            raise HTTPException(404, "没有这个互联服务器")
        store.execute(
            "UPDATE network_peers SET state=?,revision=revision+1 WHERE id=?",
            (payload.state, peer_id),
        )
        if payload.state != "accepted":
            store.execute("DELETE FROM network_cache WHERE via=? OR node=?", (peer_id, row["node"]))
        return network.admin_view()

    @app.post(PATH)
    async def network_wire(payload: Wire, request: Request):
        if payload.action == "discovery":
            return await network.discovery.wire(payload.message, request)
        if payload.action == "join":
            network.limited("join", 10)
            existing = store.one(
                "SELECT * FROM network_peers WHERE node=?", (node_id(payload.key),)
            )
            if existing:
                if not network.enabled():
                    raise HTTPException(403, "服务器互联已关闭")
                # 丢失响应只接受原申请，不覆盖暂停、屏蔽、新凭据。
                if (
                    existing["direction"] == "incoming"
                    and existing["state"] in {"pending", "accepted"}
                    and existing["token_hash"] == digest(payload.token)
                    and existing["nonce"] == payload.nonce
                    and store.decrypt(existing["origin"]) == payload.origin
                    and store.decrypt(existing["endpoint"]) == payload.endpoint
                ):
                    return {"key": network.public, "state": existing["state"]}
                raise HTTPException(409, "已有连接的身份或授权不同，请先移除旧连接")
            invite = store.one(
                "SELECT * FROM network_invites WHERE token_hash=? AND expires>?",
                (digest(payload.invite), time.time()),
            )
            if (
                not network.enabled()
                or not invite
                or len(payload.token) < 32
                or len(payload.nonce) < 32
            ):
                raise HTTPException(403, "连接码已失效或服务器尚未允许互联")
            origin = network.check_endpoint(payload.origin, payload.endpoint)
            try:
                Ed25519PublicKey.from_public_bytes(decode(payload.key))
            except (ValueError, TypeError):
                raise HTTPException(422, "服务器签名身份无效") from None
            node = node_id(payload.key)
            if (
                node == network.node
                or store.one("SELECT id FROM network_peers WHERE node=?", (node,))
                or store.one("SELECT COUNT(*) AS n FROM network_peers")["n"] >= 8
            ):
                raise HTTPException(409, "服务器重复或连接数量已达上限")
            result = await network.peers.request(
                origin,
                PATH,
                endpoint=payload.endpoint or None,
                require_signed=settings.native,
                token=payload.token,
                data={"action": "challenge"},
            )
            if result != {
                "key": payload.key,
                "nonce": payload.nonce,
                "intended": settings.public_url,
            }:
                raise HTTPException(403, "无法验证对方管理员的连接授权")
            # 回调期间可能关闭或重新生成连接码；只认仍有效的同一条邀请。
            if not network.enabled() or not store.one(
                "SELECT token_hash FROM network_invites WHERE token_hash=? AND expires>?",
                (digest(payload.invite), time.time()),
            ):
                raise HTTPException(409, "连接授权已变化")
            store.execute(
                "DELETE FROM network_invites WHERE token_hash=?", (digest(payload.invite),)
            )
            store.execute(
                "INSERT INTO "
                "network_peers(id,node,name,origin,endpoint,public_key,token,token_hash,"
                "nonce,direction,state) "
                "VALUES(?,?,?,?,?,?,?,?,?,'incoming','pending')",
                (
                    str(uuid.uuid4()),
                    node,
                    payload.name or "待确认的影友服务器",
                    store.encrypt(origin),
                    store.encrypt(payload.endpoint),
                    payload.key,
                    store.encrypt(payload.token),
                    digest(payload.token),
                    payload.nonce,
                ),
            )
            return {"key": network.public, "state": "pending"}
        token = request.headers.get("x-watch-friends-token", "")
        row = (
            store.one("SELECT * FROM network_peers WHERE token_hash=?", (digest(token),))
            if token
            else None
        )
        if not row and payload.action == "rotate_commit":
            row = network.management.pending_peer(token, payload.request_id)
        if not row:
            network.limited("unknown", 20)
            raise HTTPException(401, "互联连接凭据无效")
        network.limited(row["id"], 120)
        if payload.action == "challenge":
            return {
                "key": network.public,
                "nonce": row["nonce"],
                "intended": store.decrypt(row["origin"]),
            }
        if payload.action in {"rotate_prepare", "rotate_commit", "disconnect"}:
            return network.management.wire(row, payload, token)
        state = row["state"] if network.enabled() else "paused"
        if payload.action == "status":
            return {"key": network.public, "state": state}
        if state != "accepted":
            raise HTTPException(403, "互联连接尚未双方同意或已停止")
        if payload.action == "encryption_key":
            return await network.mail.wire_key(row, payload.message)
        if payload.action == "message":
            return await network.mail.wire(row, payload.message)
        if payload.action == "directory":
            cards = await network.catalog(row["node"])
            current = store.one("SELECT state,revision FROM network_peers WHERE id=?", (row["id"],))
            if (
                not current
                or current["state"] != "accepted"
                or current["revision"] != row["revision"]
                or not network.enabled()
            ):
                raise HTTPException(403, "互联授权已变化，请重新确认连接")
            return {
                "cards": cards[payload.offset : payload.offset + PAGE],
                "more": len(cards) > payload.offset + PAGE,
            }
        # 撤回通知始终验根签名；仅向下一跳转发一次，不接受任意 URL 或来源身份字段。
        forwarded = []
        for event in payload.events:
            content = event.get("payload")
            if not isinstance(content, dict) or content.get("card", "invalid") is not None:
                raise HTTPException(422, "撤回通知格式无效")
            if network.receive(event, row["node"], row["id"]) and not event["proofs"]:
                previous = {k: event[k] for k in ("key", "payload", "signature")}
                forwarded.append(
                    {
                        **event,
                        "proofs": [
                            {
                                "key": network.public,
                                "signature": encode(
                                    network.key.sign(
                                        canonical(
                                            {"previous": digest(canonical(previous).decode())}
                                        )
                                    )
                                ),
                            }
                        ],
                    }
                )
        if forwarded:
            await network.notify(forwarded, exclude=row["id"])
        return {"ok": True}

    return network
