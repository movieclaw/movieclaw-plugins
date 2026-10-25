"""仅依赖宿主运行环境的投稿 CI；不访问互联网或任何真实账号。"""

import json
from html.parser import HTMLParser
import tempfile
import tomllib
import unittest
from pathlib import Path

from cryptography.fernet import InvalidToken
from movieclaw_friends.app import Media
from movieclaw_friends.auto_sync import SyncSettings
from movieclaw_friends.config import origin_url
from movieclaw_friends.discovery import DiscoveryConfig
from movieclaw_friends.media import safe_poster_path, sanitize_media
from movieclaw_friends.network import Options
from movieclaw_friends.peers import PeerClient, PeerError
from movieclaw_friends.store import Store
from movieclaw_friends.timeline import merge_history
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_manifest_identity_and_permissions(self):
        manifest = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text())
        self.assertEqual(manifest["plugin"]["id"], "watch-friends")
        self.assertEqual(manifest["plugin"]["version"], "0.1.1")
        self.assertEqual(manifest["plugin"]["runtime"], "inline")
        self.assertEqual(
            set(manifest["permissions"]["operations"]),
            {"members.list", "playback.history", "search.library"},
        )
        self.assertEqual(manifest["permissions"]["paths"], [])

    def test_generated_web_matches_source(self):
        source = ROOT / "movieclaw_friends" / "static"
        for path in (ROOT / "web").rglob("*"):
            if path.is_file():
                with self.subTest(file=path.name):
                    self.assertEqual(
                        path.read_bytes(),
                        (source / path.relative_to(ROOT / "web")).read_bytes(),
                    )

    def test_brand_and_theme_assets_survive_integration(self):
        # 合并图标与主题修复时，入口必须同时保留品牌资源和首屏主题初始化。
        source = ROOT / "movieclaw_friends" / "static"
        tags = []
        parser = HTMLParser()
        parser.handle_starttag = lambda tag, attrs: tags.append((tag, dict(attrs)))
        parser.feed((source / "index.html").read_text())
        assets = [attrs.get("href") or attrs.get("src") for tag, attrs in tags
                  if tag in {"link", "script"}]
        for name in ("theme.js", "style.css", "manifest.webmanifest", "icon.svg",
                     "icon-192.png", "apple-touch-icon.png", "icon-mask.svg"):
            with self.subTest(asset=name):
                self.assertIn(name, assets)
                self.assertTrue((source / name).is_file())
        self.assertLess(assets.index("theme.js"), assets.index("style.css"))

    def test_store_metadata(self):
        metadata = json.loads((ROOT / "store.json").read_text())
        self.assertEqual(metadata["author"], "sexyfeifan")
        self.assertEqual(metadata["origin"], "official")
        self.assertTrue((ROOT / "CHANGELOG.md").is_file())

    def test_real_host_isolated_load_reload_and_disposal(self):
        from .test_package_loading import (
            test_actual_host_inline_loading_startup_and_reload_in_clean_process,
        )

        with tempfile.TemporaryDirectory() as directory:
            test_actual_host_inline_loading_startup_and_reload_in_clean_process(
                Path(directory)
            )


class PrivacyTests(unittest.TestCase):
    def test_sync_disabled_by_default(self):
        self.assertFalse(SyncSettings().enabled)
        self.assertFalse(SyncSettings().consent)
        self.assertEqual(SyncSettings().sources, [])

    def test_network_grants_independent_and_default_off(self):
        self.assertFalse(any(Options().model_dump().values()))
        self.assertFalse(any(DiscoveryConfig().model_dump().values()))
        self.assertFalse(Options(publish=True).discovery)

    def test_sync_does_not_accept_truthy_string(self):
        with self.assertRaises(ValidationError):
            SyncSettings(enabled="true")

    def test_poster_paths_cannot_carry_credentials_or_traversal(self):
        for path in (
            "https://private.test/a.jpg",
            "/../secret.jpg",
            "/a.jpg?token=x",
            "/folder/a.jpg",
            "javascript:alert(1)",
        ):
            with self.subTest(path=path):
                self.assertIsNone(safe_poster_path(path))
                with self.assertRaises(ValidationError):
                    Media(cover_images=[{"path": path, "type": "poster"}])

    def test_nine_images_and_legacy_first_cover(self):
        images = [{"path": f"/image-{n}.jpg", "type": "poster"} for n in range(9)]
        media = Media(title="电影", cover_images=images).model_dump()
        self.assertEqual(sanitize_media(media)["cover_images"], images)
        self.assertEqual(media["cover_path"], images[0]["path"])
        with self.assertRaises(ValidationError):
            Media(cover_images=images + images[:1])

    def test_shared_media_strips_private_fields(self):
        media = sanitize_media(
            {
                "title": "电影",
                "token": "test-secret",
                "file_path": "/private/a.mkv",
                "stream_url": "https://private.test/stream",
                "poster_path": "/poster.jpg",
            }
        )
        self.assertEqual(media, {"title": "电影", "poster_path": "/poster.jpg"})

    def test_relative_image_and_official_tmdb_compatibility(self):
        self.assertEqual(safe_poster_path("/poster.jpg"), "/poster.jpg")
        self.assertEqual(
            sanitize_media({"poster_url": "https://image.tmdb.org/t/p/w500/a.jpg"})[
                "poster_path"
            ],
            "/a.jpg",
        )
        self.assertIsNone(
            sanitize_media(
                {"poster_url": "https://image.tmdb.org/t/p/w500/a.jpg?token=x"}
            )["poster_path"]
        )

    def test_endpoint_requires_same_origin_exact_path_and_signature(self):
        peer = PeerClient()
        self.assertTrue(
            peer.validate_endpoint(
                "https://friend.example",
                "/federation/data",
                "https://friend.example/api/v1/plugins/watch-friends/federation/data?sig=fixture",
            ).endswith("?sig=fixture")
        )
        for endpoint in (
            "https://other.example/api/v1/plugins/watch-friends/federation/data?sig=x",
            "https://friend.example/api/v1/plugins/watch-friends/federation/data",
            "https://friend.example/api/v1/admin?sig=x",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(PeerError):
                peer.validate_endpoint(
                    "https://friend.example", "/federation/data", endpoint
                )

    def test_cleartext_peers_and_credentialed_origins_rejected(self):
        with self.assertRaises(PeerError):
            PeerClient().validate("http://friend.example")
        with self.assertRaises(ValueError):
            origin_url("https://user:password@friend.example")
        self.assertEqual(
            origin_url("https://FRIEND.example:443/"), "https://friend.example"
        )

    def test_history_deduplicates_and_orders_actual_time(self):
        def row(number, date, record):
            return {
                "id": record,
                "media": {"tmdb_id": number, "kind": "movie"},
                "ended_at": date,
            }

        result = merge_history(
            [row(550, "2026-01-01T00:00:00Z", 1), row(551, None, 2)],
            [row(550, "2026-02-01T00:00:00Z", 3)],
        )
        self.assertEqual([item["id"] for item in result], [3, 2])


class StorageTests(unittest.TestCase):
    def test_encrypted_credentials_persist_only_with_own_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = Store(root / "one"), Store(root / "two")
            self.addCleanup(one.close)
            self.addCleanup(two.close)
            encrypted = one.encrypt("fixture-credential")
            self.assertNotIn("fixture-credential", encrypted)
            self.assertEqual(one.decrypt(encrypted), "fixture-credential")
            with self.assertRaises(InvalidToken):
                two.decrypt(encrypted)
            self.assertEqual(
                (root / "one" / "credentials.key").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(
                (root / "one" / "friends.sqlite3").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(one.all("SELECT * FROM users"), [])
