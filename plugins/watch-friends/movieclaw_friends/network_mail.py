"""受控网络上的两跳密封消息；中间服务器只能验证路由，不能读取私人载荷。"""

import asyncio
import json
import os
import time
import uuid

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException

from .network import canonical, decode, encode, node_id
from .peers import PeerError
from .store import digest


def raw(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def secret(private, public, number):
    shared = private.exchange(X25519PublicKey.from_public_bytes(decode(public)))
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=number.encode(), info=b"yingji-mail-v1"
    ).derive(shared)


class NetworkMail:
    """密钥声明、消息及逐跳证明均由持久节点身份签名；响应绑定原请求。"""

    def __init__(self, network):
        self.network, self.store, self.handler = network, network.store, None
        self.inflight = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS network_mail_key(id INTEGER PRIMARY KEY,
                secret TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS network_mail_replies(
                id TEXT PRIMARY KEY,digest TEXT NOT NULL,response TEXT NOT NULL,
                expires REAL NOT NULL);
        """)
        saved = self.store.one("SELECT secret FROM network_mail_key WHERE id=1")
        if not saved:
            key = X25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            self.store.execute(
                "INSERT INTO network_mail_key VALUES(1,?)", (self.store.encrypt(encode(key)),)
            )
            saved = self.store.one("SELECT secret FROM network_mail_key WHERE id=1")
        self.private = X25519PrivateKey.from_private_bytes(
            decode(self.store.decrypt(saved["secret"]))
        )

    def key_statement(self):
        value = {
            "node": self.network.node,
            "encryption_key": encode(raw(self.private.public_key())),
        }
        return {**value, "signature": encode(self.network.key.sign(canonical(value)))}

    def route(self, destination, visited=()):
        if not self.network.enabled() or destination in visited:
            raise HTTPException(403, "互联路由已停止或存在循环")
        direct = self.store.one(
            "SELECT * FROM network_peers WHERE node=? AND state='accepted'", (destination,)
        )
        if direct and direct["node"] not in visited:
            return direct
        rows = self.store.all(
            (
                "SELECT p.*,c.envelope FROM network_cache c JOIN network_peers p ON "
                "p.id=c.via WHERE c.node=? AND c.expires>? AND p.state='accepted' AND "
                "p.remote_state='accepted' ORDER BY c.expires DESC"
            ),
            (destination, time.time()),
        )
        for row in rows:
            if row["node"] not in visited:
                envelope = json.loads(row["envelope"])
                if len(envelope["proofs"]) == 1:
                    return row
        raise HTTPException(404, "没有仍获批准的互联路径，请刷新联网广场")

    async def send(self, destination, public, body):
        row = self.route(destination)
        statement = await self.network.request(
            row,
            {
                "action": "encryption_key",
                "message": {"destination": destination, "visited": [self.network.node]},
            },
        )
        try:
            value = {k: statement[k] for k in ("node", "encryption_key")}
            if value["node"] != destination:
                raise ValueError
            self.network.verify_signature(public, statement["signature"], value)
            ephemeral = X25519PrivateKey.generate()
            number = str(uuid.uuid4())
            key = secret(ephemeral, statement["encryption_key"], number)
            nonce = os.urandom(12)
            packet = {
                "id": number,
                "destination": destination,
                "key": self.network.public,
                "issued": time.time(),
                "ephemeral": encode(raw(ephemeral.public_key())),
                "nonce": encode(nonce),
                "cipher": encode(AESGCM(key).encrypt(nonce, canonical(body), destination.encode())),
            }
            message = {
                "packet": packet,
                "signature": encode(self.network.key.sign(canonical(packet))),
                "proofs": [],
            }
            result = await self.network.request(row, {"action": "message", "message": message})
            self.network.verify_signature(
                public,
                result["signature"],
                {"id": number, "nonce": result["nonce"], "cipher": result["cipher"]},
            )
            value = json.loads(
                AESGCM(key).decrypt(
                    decode(result["nonce"]), decode(result["cipher"]), number.encode()
                )
            )
            if not value.get("ok"):
                raise HTTPException(value.get("status", 502), value.get("error", "远端请求未完成"))
            return value["data"]
        except (KeyError, ValueError, TypeError, InvalidSignature, InvalidTag):
            raise PeerError("互联消息身份或加密响应无效") from None

    async def wire_key(self, row, message):
        destination = message.get("destination")
        visited = message.get("visited")
        if (
            not isinstance(destination, str)
            or len(destination) != 64
            or not isinstance(visited, list)
            or len(visited) > 2
            or visited[-1:] != [row["node"]]
            or self.network.node in visited
        ):
            raise HTTPException(422, "密钥路由无效")
        if destination == self.network.node:
            return self.key_statement()
        if len(visited) >= 2:
            raise HTTPException(403, "超过两跳互联范围")
        target = self.route(destination, visited)
        return await self.network.request(
            target,
            {
                "action": "encryption_key",
                "message": {"destination": destination, "visited": [*visited, self.network.node]},
            },
        )

    def authorized(self, row):
        latest = self.store.one("SELECT state,revision FROM network_peers WHERE id=?", (row["id"],))
        if (
            not self.network.enabled()
            or not latest
            or latest["state"] != "accepted"
            or latest["revision"] != row["revision"]
        ):
            raise HTTPException(403, "互联连接授权已变化")

    async def wire(self, row, message):
        number = (
            message.get("packet", {}).get("id") if isinstance(message.get("packet"), dict) else None
        )
        if not isinstance(number, str) or not 1 <= len(number) <= 64 or len(self.inflight) >= 128:
            raise HTTPException(422, "互联消息标识无效或在途请求过多")
        entry = self.inflight.setdefault(number, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                return await self.deliver(row, message)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self.inflight.pop(number, None)

    async def deliver(self, row, message):
        self.authorized(row)
        try:
            if len(canonical(message)) > 700000 or set(message) != {
                "packet",
                "signature",
                "proofs",
            }:
                raise ValueError
            packet, proofs = message["packet"], message["proofs"]
            if (
                set(packet)
                != {"id", "destination", "key", "issued", "ephemeral", "nonce", "cipher"}
                or not isinstance(proofs, list)
                or len(proofs) > 1
            ):
                raise ValueError
            if (
                not isinstance(packet["id"], str)
                or len(packet["id"]) > 64
                or not isinstance(packet["issued"], (int, float))
                or not time.time() - 300 <= packet["issued"] <= time.time() + 30
            ):
                raise ValueError
            self.network.verify_signature(packet["key"], message["signature"], packet)
            path = [node_id(packet["key"])]
            for proof in proofs:
                self.network.verify_signature(
                    proof["key"],
                    proof["signature"],
                    {
                        "message": digest(
                            canonical(
                                {"packet": packet, "signature": message["signature"]}
                            ).decode()
                        )
                    },
                )
                path.append(node_id(proof["key"]))
            if path[-1] != row["node"] or len(set(path)) != len(path) or self.network.node in path:
                raise ValueError
            if self.store.one(
                "SELECT 1 FROM network_peers WHERE node=? AND state='blocked'", (path[0],)
            ):
                raise HTTPException(403, "来源服务器已被屏蔽")
            if packet["destination"] != self.network.node:
                if proofs:
                    raise ValueError
                target = self.route(packet["destination"], path)
                proof = {
                    "key": self.network.public,
                    "signature": encode(
                        self.network.key.sign(
                            canonical(
                                {
                                    "message": digest(
                                        canonical(
                                            {"packet": packet, "signature": message["signature"]}
                                        ).decode()
                                    )
                                }
                            )
                        )
                    ),
                }
                return await self.network.request(
                    target, {"action": "message", "message": {**message, "proofs": [proof]}}
                )
            saved = self.store.one("SELECT * FROM network_mail_replies WHERE id=?", (packet["id"],))
            hashed = digest(canonical(packet).decode())
            if saved:
                if saved["digest"] != hashed:
                    raise ValueError
                return json.loads(self.store.decrypt(saved["response"]))
            key = secret(self.private, packet["ephemeral"], packet["id"])
            body = json.loads(
                AESGCM(key).decrypt(
                    decode(packet["nonce"]), decode(packet["cipher"]), self.network.node.encode()
                )
            )
            if not isinstance(body, dict) or self.handler is None:
                raise ValueError
            try:
                data = await self.handler(
                    node_id(packet["key"]), body, lambda: self.authorized(row)
                )
                response = {"ok": True, "data": data}
            except HTTPException as exc:
                response = {"ok": False, "status": exc.status_code, "error": str(exc.detail)}
            nonce = os.urandom(12)
            result = {
                "id": packet["id"],
                "nonce": encode(nonce),
                "cipher": encode(
                    AESGCM(key).encrypt(nonce, canonical(response), packet["id"].encode())
                ),
            }
            result["signature"] = encode(self.network.key.sign(canonical(result)))
            self.store.execute("DELETE FROM network_mail_replies WHERE expires<=?", (time.time(),))
            self.store.execute(
                "INSERT INTO network_mail_replies VALUES(?,?,?,?)",
                (packet["id"], hashed, self.store.encrypt(json.dumps(result)), time.time() + 600),
            )
            return result
        except (KeyError, ValueError, TypeError, InvalidSignature, InvalidTag):
            raise HTTPException(422, "互联密封消息无效") from None
