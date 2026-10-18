"""Pack, tag, and index tests. They do not import MovieClaw."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

import generate_index
import pack
import pluginlib
from ci_dirs import lock_files, select

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "example" / "hello-note"


class PackTest(unittest.TestCase):
    def test_example_pack_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.mcplugin"
            second = Path(tmp) / "b.mcplugin"
            digest_a, size_a = pack.pack(EXAMPLE, first)
            digest_b, size_b = pack.pack(EXAMPLE, second)
            self.assertEqual(digest_a, digest_b)
            self.assertEqual(size_a, size_b)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest_a)

    def test_output_inside_the_plugin_is_rejected(self) -> None:
        with self.assertRaises(pluginlib.PluginError):
            pack.pack(EXAMPLE, EXAMPLE / "hello-note-0.1.0.mcplugin")


class TagTest(unittest.TestCase):
    def test_split_last_version_marker(self) -> None:
        self.assertEqual(pluginlib.split_tag("hello-note-v0.1.0"), ("hello-note", "0.1.0"))
        self.assertEqual(
            pluginlib.split_tag("my-cool-plugin-v1.2.3-dev.9"),
            ("my-cool-plugin", "1.2.3-dev.9"),
        )

    def test_rejects_latest_style_tags(self) -> None:
        with self.assertRaises(pluginlib.PluginError):
            pluginlib.split_tag("v0.1.0")
        with self.assertRaises(pluginlib.PluginError):
            pluginlib.split_tag("catalog")

    def test_check_tag_requires_plugins_directory(self) -> None:
        env = os.environ.copy()
        env["TAG"] = "hello-note-v0.1.0"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_tag.py")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("hello-note", proc.stderr)


class IndexTest(unittest.TestCase):
    def test_tree_reproduces_committed_index(self) -> None:
        # The committed index carries the published versions; regenerating it
        # from the plugin tree with itself as --previous must give it back.
        committed = (ROOT / "index.json").read_text(encoding="utf-8")
        previous = json.loads(committed)
        document = generate_index.build_index(
            plugins_dir=ROOT / "plugins",
            repo=pluginlib.REPO,
            generated_at=previous["generated_at"],
            previous=previous,
            releases=[],
            signed=previous["signed"],
            key_id=previous["key_id"],
        )
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(text, committed)
        generate_index.check_index(ROOT / "index.json")

    def test_release_record_fills_download_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "demo-note"
            plugin.mkdir()
            (plugin / "movieclaw-plugin.toml").write_text(
                textwrap.dedent(
                    """\
                    [plugin]
                    id = "demo-note"
                    title = "演示"
                    version = "1.2.0"
                    entry = "demo_note"
                    runtime = "process"
                    sdk = "^1.0"
                    description = "一条说明"

                    [permissions]
                    operations = []
                    """
                ),
                encoding="utf-8",
            )
            (plugin / "store.json").write_text(
                json.dumps(
                    {
                        "author": "MovieClaw",
                        "homepage": "https://github.com/movieclaw/movieclaw-plugins",
                        "license": "见仓库许可证",
                        "origin": "official",
                        "min_app": None,
                        "max_app": None,
                    }
                ),
                encoding="utf-8",
            )
            sha = "a" * 64
            document = generate_index.build_index(
                plugins_dir=Path(tmp),
                repo="movieclaw/movieclaw-plugins",
                generated_at="2026-10-13T00:00:00Z",
                previous=None,
                releases=[
                    {
                        "id": "demo-note",
                        "version": "1.2.0",
                        "sha256": sha,
                        "size": 12,
                        "released_at": "2026-10-13T01:02:03Z",
                    }
                ],
                signed=False,
                key_id=None,
            )
            self.assertEqual(document["plugins"][0]["latest"], "1.2.0")
            self.assertFalse(document["signed"])
            self.assertIsNone(document["key_id"])
            artifact = document["plugins"][0]["versions"][0]["artifacts"][0]
            self.assertEqual(artifact["sha256"], sha)
            self.assertEqual(artifact["size"], 12)
            self.assertEqual(artifact["platform"], "any")
            self.assertEqual(
                artifact["url"],
                "https://github.com/movieclaw/movieclaw-plugins/releases/download/"
                "demo-note-v1.2.0/demo-note-1.2.0.mcplugin",
            )
            out = Path(tmp) / "index.json"
            out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            generate_index.check_index(out)

    def test_prerelease_is_not_latest_when_a_stable_exists(self) -> None:
        self.assertGreater(
            pluginlib.version_sort_key("1.0.0"),
            pluginlib.version_sort_key("1.0.0-dev.1"),
        )


class CiDirsTest(unittest.TestCase):
    def test_readme_only_skips_movieclaw(self) -> None:
        run, dirs = select(["README.md"])
        self.assertFalse(run)
        self.assertEqual(dirs, [])

    def test_example_change_validates_the_example(self) -> None:
        run, dirs = select(["example/GUIDE.md"])
        record = json.loads((ROOT / "example" / "UPSTREAM.json").read_text(encoding="utf-8"))
        expected = ["example/hello-note"] + [f"example/{item['id']}" for item in record["plugins"]]
        self.assertTrue(run)
        self.assertEqual(dirs, expected)

    def test_script_change_includes_example(self) -> None:
        run, dirs = select(["scripts/pack.py"])
        self.assertTrue(run)
        self.assertIn("example/hello-note", dirs)
        self.assertIn("example/watch-presence", dirs)
        self.assertIn("example/strm-prefetch", dirs)

    def test_reference_examples_follow_upstream_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = root / "example"
            (example / "hello-note").mkdir(parents=True)
            (example / "hello-note" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "weixin-channel").mkdir()
            (example / "weixin-channel" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "skip-me").mkdir()
            (example / "skip-me" / "movieclaw-plugin.toml").write_text("x", encoding="utf-8")
            (example / "UPSTREAM.json").write_text(
                json.dumps(
                    {
                        "plugins": [
                            {"id": "weixin-channel"},
                            {"id": "missing"},
                            {"id": "../nope"},
                            {"id": "hello-note"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            run, dirs = select(["example/GUIDE.md"], root)
            self.assertTrue(run)
            self.assertEqual(dirs, ["example/hello-note", "example/weixin-channel"])


class DependencyLockTest(unittest.TestCase):
    def plugin(self, tmp: str, table: str, *, lock: str | None = "requirements.lock") -> Path:
        directory = Path(tmp) / "plugins" / "hello-note"
        shutil.copytree(EXAMPLE, directory)
        manifest = directory / "movieclaw-plugin.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + table, encoding="utf-8")
        if lock is not None:
            (directory / lock).parent.mkdir(parents=True, exist_ok=True)
            (directory / lock).write_text("# python: 3.12\n", encoding="utf-8")
        return directory

    def test_without_dependencies_there_is_no_lock(self) -> None:
        self.assertIsNone(pluginlib.dependency_lock(EXAMPLE))

    def test_lock_file_is_packed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.plugin(tmp, '\n[dependencies]\nlock = "requirements.lock"\n')
            self.assertEqual(
                pluginlib.dependency_lock(directory), (directory / "requirements.lock").resolve()
            )
            output = Path(tmp) / "out.mcplugin"
            pack.pack(directory, output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("requirements.lock", archive.namelist())
            self.assertEqual(lock_files(["plugins/hello-note"], Path(tmp)), [
                "plugins/hello-note/requirements.lock"
            ])

    def test_rejected_locks(self) -> None:
        cases = {
            "missing": ('\n[dependencies]\nlock = "requirements.lock"\n', None),
            "outside": ('\n[dependencies]\nlock = "../requirements.lock"\n', None),
            "skipped by pack": ('\n[dependencies]\nlock = ".deps/requirements.lock"\n',
                                ".deps/requirements.lock"),
            "not a path": ("\n[dependencies]\nlock = 1\n", "requirements.lock"),
        }
        for label, (table, lock) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                directory = self.plugin(tmp, table, lock=lock)
                with self.assertRaises(pluginlib.PluginError):
                    pluginlib.dependency_lock(directory)
                with self.assertRaises(pluginlib.PluginError):
                    pack.pack(directory, Path(tmp) / "out.mcplugin")

    def test_only_process_plugins_take_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = self.plugin(tmp, '\n[dependencies]\nlock = "requirements.lock"\n')
            manifest = directory / "movieclaw-plugin.toml"
            text = manifest.read_text(encoding="utf-8").replace(
                'runtime = "process"', 'runtime = "inline"'
            )
            manifest.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(pluginlib.PluginError, "process"):
                pluginlib.dependency_lock(directory)


if __name__ == "__main__":
    unittest.main()
