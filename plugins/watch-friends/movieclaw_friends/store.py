"""插件自己的 SQLite 存储；不会打开 MovieClaw 数据库。"""

import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    """社交记录与密钥限定在插件目录中，宿主令牌和关系凭据均加密保存。

    SQLite 操作很短且不跨 await；锁防止并行请求共用连接时交错提交。
    Session 与跨服凭据通过摘要查找，明文不会进入查询结果或日志。
    """

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = directory / "credentials.key"
        if not key_path.exists():
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.lock = threading.RLock()
        database_path = directory / "friends.sqlite3"
        self.db = sqlite3.connect(database_path, check_same_thread=False, isolation_level=None)
        os.chmod(database_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
                member_id INTEGER, role TEXT NOT NULL, token TEXT NOT NULL,
                invite_token TEXT NOT NULL UNIQUE, discoverable INTEGER NOT NULL DEFAULT 0,
                share_current INTEGER NOT NULL DEFAULT 0, share_history INTEGER NOT NULL DEFAULT 0,
                public_history TEXT NOT NULL DEFAULT '[]', public_updated_at TEXT,
                privacy_revision INTEGER NOT NULL DEFAULT 0,
                demo_enabled INTEGER NOT NULL DEFAULT 1,
                public_recent INTEGER NOT NULL DEFAULT 1,
                share_favorites INTEGER NOT NULL DEFAULT 0,
                public_favorites TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS account_profiles (
                user_id TEXT PRIMARY KEY REFERENCES users(id), server_name TEXT,
                joined_at TEXT, avatar_data TEXT, avatar_version TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS plugin_memberships (
                user_id TEXT PRIMARY KEY REFERENCES users(id), joined_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relationships (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                target_id TEXT REFERENCES users(id), peer_origin TEXT, peer_user_id TEXT,
                peer_name TEXT, direction TEXT NOT NULL, status TEXT NOT NULL,
                token TEXT, token_hash TEXT UNIQUE, remote_request_id TEXT,
                cache TEXT, synced_at TEXT, created_at REAL NOT NULL,
                UNIQUE(peer_origin, remote_request_id)
            );
            CREATE TABLE IF NOT EXISTS collections (
                user_id TEXT NOT NULL REFERENCES users(id), source_id INTEGER NOT NULL,
                name TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(user_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS plugin_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                description TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_favorites (
                user_id TEXT PRIMARY KEY REFERENCES users(id), data TEXT NOT NULL
            );
        """)
        # 仅迁移插件私有库；原生公开路由的签名能力保存在关系后台，不返回页面。
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(relationships)")}
        if "peer_endpoints" not in columns:
            self.db.execute("ALTER TABLE relationships ADD COLUMN peer_endpoints TEXT")
        # 每个账号只保存自己的演示偏好；旧插件库默认继续显示，不改变真实共享授权。
        user_columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
        if "demo_enabled" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN demo_enabled INTEGER NOT NULL DEFAULT 1")
        # 原有广场授权包含最近五条记录；迁移保留该语义，喜欢的电影始终另行同意。
        for name, definition in (
            ("public_recent", "INTEGER NOT NULL DEFAULT 1"),
            ("share_favorites", "INTEGER NOT NULL DEFAULT 0"),
            ("public_favorites", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if name not in user_columns:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        if "share_posts" not in user_columns:
            self.db.execute("ALTER TABLE users ADD COLUMN share_posts INTEGER NOT NULL DEFAULT 0")
        if "share_imported_history" not in user_columns:
            self.db.execute(
                "ALTER TABLE users ADD COLUMN share_imported_history INTEGER NOT NULL DEFAULT 0"
            )
        # 私有片单以版本号防止多标签页互相覆盖，不改变已发布的片单快照。
        list_columns = {row[1] for row in self.db.execute("PRAGMA table_info(plugin_lists)")}
        if "revision" not in list_columns:
            self.db.execute(
                "ALTER TABLE plugin_lists ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
            )

    def plugin_joined_at(self, owner):
        with self.lock:
            existing = self.one(
                "SELECT joined_at FROM plugin_memberships WHERE user_id=?", (owner,)
            )
            if existing:
                return existing["joined_at"]
            # 旧账号采用最早可确认的插件活动；从不使用外部影片的观看日期。
            candidates = []
            tables = {
                row["name"] for row in self.all("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ("watch_jobs", "watch_sources", "plugin_lists", "display_scans"):
                if table in tables:
                    value = self.one(
                        f"SELECT MIN(created_at) AS first FROM {table} WHERE user_id=?", (owner,)
                    )
                    if value and value["first"]:
                        candidates.append(value["first"])
            value = self.one("SELECT updated_at FROM account_profiles WHERE user_id=?", (owner,))
            if value and value["updated_at"]:
                candidates.append(value["updated_at"])
            joined = min(candidates) if candidates else datetime.now(UTC).isoformat()
            self.execute(
                "INSERT OR IGNORE INTO plugin_memberships(user_id,joined_at) VALUES(?,?)",
                (owner, joined),
            )
            return self.one("SELECT joined_at FROM plugin_memberships WHERE user_id=?", (owner,))[
                "joined_at"
            ]

    def encrypt(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self.cipher.decrypt(value.encode()).decode()

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.lock:
            self.db.execute(sql, params)

    def secret(self) -> str:
        return secrets.token_urlsafe(32)

    def close(self) -> None:
        self.db.close()
