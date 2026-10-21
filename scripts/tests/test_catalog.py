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


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


MANIFEST = """\
[plugin]
id = "demo-note"
title = "演示"
version = "{version}"
entry = "demo_note"
runtime = "process"
sdk = "^1.0"
description = "一条说明"

[requires]
"downloader-adapters" = "^1.0"

[permissions]
operations = []
network = {network}
"""


class Repo:
    """A throwaway plugins repo with one plugin, released by tagging."""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        self.plugin = self.root / "plugins" / "demo-note"
        self.plugin.mkdir(parents=True)
        _git(self.root, "init", "-q")
        self.store({})
        (self.plugin / "README.md").write_text("# 演示\n", encoding="utf-8")
        self.notes: dict[str, str] = {}

    def store(self, extra: dict) -> None:
        data = {
            "author": "MovieClaw",
            "license": "MIT",
            "origin": "official",
            "summary": "一句话",
            **extra,
        }
        (self.plugin / "store.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def release(self, version: str, *, network: bool = False, note: str = "- 改了点东西") -> dict:
        (self.plugin / "movieclaw-plugin.toml").write_text(
            MANIFEST.format(version=version, network="true" if network else "false"),
            encoding="utf-8",
        )
        self.notes[version] = note
        body = "".join(f"## {v}\n\n{n}\n\n" for v, n in reversed(self.notes.items()))
        (self.plugin / "CHANGELOG.md").write_text("# 演示\n\n" + body, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", version)
        _git(self.root, "tag", f"demo-note-v{version}")
        return {
            "id": "demo-note",
            "version": version,
            "sha256": hashlib.sha256(version.encode()).hexdigest(),
            "size": 12,
            "released_at": "2026-10-13T01:02:03Z",
        }

    def build(self, releases: list[dict], previous: dict | None = None, git_root: Path | None = None) -> dict:
        return generate_index.build_index(
            plugins_dir=self.root / "plugins",
            repo="movieclaw/movieclaw-plugins",
            generated_at="2026-10-13T00:00:00Z",
            previous=previous,
            releases=releases,
            signed=False,
            key_id=None,
            git_root=git_root or self.root,
        )


def _valid(document: dict, tmp: str) -> None:
    out = Path(tmp) / "index.json"
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    generate_index.check_index(out)


class IndexTest(unittest.TestCase):
    def test_tree_reproduces_committed_index(self) -> None:
        # The committed index carries the published versions; regenerating it
        # from the plugin tree and the tags with itself as --previous must give it back.
        self.assertEqual(
            generate_index.main(["--check", str(ROOT / "index.json")]), 0
        )

    def test_release_record_fills_download_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.2.0")
            document = repo.build([record])
            plugin = document["plugins"][0]
            self.assertEqual(document["schema"], 2)
            self.assertEqual(plugin["latest"], "1.2.0")
            self.assertEqual(plugin["latest_stable"], "1.2.0")
            self.assertEqual(plugin["summary"], "一句话")
            self.assertEqual(plugin["category"], "downloader")
            self.assertEqual(plugin["author"], {"name": "MovieClaw", "url": None})
            version = plugin["versions"][0]
            self.assertEqual(version["notes"], "- 改了点东西\n")
            self.assertEqual(version["requires"], {"downloader-adapters": "^1.0"})
            self.assertFalse(version["prerelease"])
            self.assertEqual(
                version["readme"]["url"],
                "https://raw.githubusercontent.com/movieclaw/movieclaw-plugins/"
                "demo-note-v1.2.0/plugins/demo-note/README.md",
            )
            artifact = version["artifacts"][0]
            self.assertEqual(artifact["sha256"], record["sha256"])
            self.assertEqual(artifact["platform"], "any")
            self.assertEqual(
                artifact["url"],
                "https://github.com/movieclaw/movieclaw-plugins/releases/download/"
                "demo-note-v1.2.0/demo-note-1.2.0.mcplugin",
            )
            _valid(document, tmp)

    def test_old_version_keeps_what_it_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            first = repo.release("1.0.0", network=False, note="- 第一版")
            second = repo.release("1.1.0", network=True, note="- 联网了")
            versions = repo.build([first, second])["plugins"][0]["versions"]
            self.assertEqual([v["version"] for v in versions], ["1.1.0", "1.0.0"])
            self.assertTrue(versions[0]["permissions"]["network"])
            self.assertFalse(versions[1]["permissions"]["network"])
            self.assertEqual(versions[1]["notes"], "- 第一版\n")

    def test_tag_without_release_record_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            first = repo.release("1.0.0")
            repo.release("1.1.0")
            versions = repo.build([first])["plugins"][0]["versions"]
            self.assertEqual([v["version"] for v in versions], ["1.0.0"])

    def test_shallow_clone_falls_back_to_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo = Repo(tmp)
            full = repo.build([repo.release("1.0.0")])
            again = repo.build([], previous=full, git_root=Path(empty))
            self.assertEqual(again, full)

    def test_schema_1_previous_without_tags_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            repo = Repo(tmp)
            repo.release("1.0.0")
            previous = {
                "schema": 1,
                "plugins": [
                    {
                        "id": "demo-note",
                        "versions": [
                            {
                                "version": "1.0.0",
                                "released_at": "2026-10-13T01:02:03Z",
                                "artifacts": [{"platform": "any", "sha256": "a" * 64, "size": 1}],
                            }
                        ],
                    }
                ],
            }
            with self.assertRaisesRegex(pluginlib.PluginError, "git fetch --tags"):
                repo.build([], previous=previous, git_root=Path(empty))
            upgraded = repo.build([], previous=previous)
            self.assertEqual(upgraded["plugins"][0]["versions"][0]["artifacts"][0]["size"], 1)

    def test_yanked_and_prerelease_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            records = [
                repo.release("1.0.0"),
                repo.release("1.1.0"),
                repo.release("1.2.0-beta.2"),
                repo.release("1.2.0-beta.10"),
            ]
            repo.store({"yanked": {"1.1.0": "会删掉做种任务"}})
            plugin = repo.build(records)["plugins"][0]
            self.assertEqual(
                [v["version"] for v in plugin["versions"]],
                ["1.2.0-beta.10", "1.2.0-beta.2", "1.1.0", "1.0.0"],
            )
            self.assertEqual(plugin["latest"], "1.2.0-beta.10")
            self.assertEqual(plugin["latest_stable"], "1.0.0")
            self.assertEqual(plugin["versions"][2]["yanked"], {"reason": "会删掉做种任务"})
            self.assertTrue(plugin["versions"][0]["prerelease"])
            _valid(repo.build(records), tmp)

    def test_yanking_an_unknown_version_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            repo.store({"yanked": {"9.9.9": "笔误"}})
            with self.assertRaisesRegex(pluginlib.PluginError, "9.9.9"):
                repo.build([record])

    def test_icon_becomes_a_content_named_catalog_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            record = repo.release("1.0.0")
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
            (repo.plugin / "icon.svg").write_bytes(svg)
            repo.store({"icon": "icon.svg", "category": "tools"})
            assets: dict[str, bytes] = {}
            document = generate_index.build_index(
                plugins_dir=repo.root / "plugins",
                repo="movieclaw/movieclaw-plugins",
                generated_at="2026-10-13T00:00:00Z",
                previous=None,
                releases=[record],
                signed=False,
                key_id=None,
                git_root=repo.root,
                assets=assets,
            )
            plugin = document["plugins"][0]
            digest = hashlib.sha256(svg).hexdigest()
            name = f"demo-note-icon-{digest[:16]}.svg"
            self.assertEqual(assets, {name: svg})
            self.assertEqual(plugin["icon"]["sha256"], digest)
            self.assertTrue(plugin["icon"]["url"].endswith(f"/releases/download/catalog/{name}"))
            self.assertEqual(plugin["category"], "tools")

    def test_bad_images_are_rejected(self) -> None:
        cases = {
            "script": ("icon.svg", b"<svg><script>alert(1)</script></svg>"),
            "handler": ("icon.svg", b'<svg onload="x()"></svg>'),
            "type": ("icon.gif", b"GIF89a"),
            "url": ("https://example.com/i.svg", None),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo = Repo(tmp)
                record = repo.release("1.0.0")
                if data is not None:
                    (repo.plugin / name).write_bytes(data)
                repo.store({"icon": name})
                with self.assertRaises(pluginlib.PluginError):
                    repo.build([record])

    def test_store_json_rules(self) -> None:
        cases = {
            "removed field": {"min_app": None},
            "long summary": {"summary": "长" * 61},
            "bad category": {"category": "games"},
            "bad homepage": {"homepage": "http://example.com"},
            "bad deprecated": {"deprecated": {"replaced_by": "x"}},
        }
        for label, extra in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                repo = Repo(tmp)
                repo.store(extra)
                with self.assertRaises(pluginlib.PluginError):
                    pluginlib.load_store(repo.plugin)

    def test_lint_wants_a_changelog_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            repo.release("1.0.0")
            self.assertEqual(generate_index.lint(repo.root / "plugins"), [])
            manifest = repo.plugin / "movieclaw-plugin.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace("1.0.0", "1.1.0"), encoding="utf-8"
            )
            problems = generate_index.lint(repo.root / "plugins")
            self.assertEqual(len(problems), 1)
            self.assertIn("## 1.1.0", problems[0])

    def test_prerelease_ordering(self) -> None:
        key = pluginlib.version_sort_key
        self.assertGreater(key("1.0.0"), key("1.0.0-dev.1"))
        self.assertGreater(key("1.0.0-beta.10"), key("1.0.0-beta.2"))
        self.assertGreater(key("1.0.0-rc.1"), key("1.0.0-beta.9"))
        self.assertGreater(key("1.0.0-beta"), key("1.0.0-1"))
        self.assertEqual(key("1.0.0+build.5"), key("1.0.0"))


class ChangelogTest(unittest.TestCase):
    def test_section_forms(self) -> None:
        text = "# x\n\n## [1.1.0] - 2026-10-21\n\n- b\n### 细节\n- c\n\n## v1.0.0\n\n- a\n"
        self.assertEqual(pluginlib.changelog_section(text, "1.1.0"), "- b\n### 细节\n- c\n")
        self.assertEqual(pluginlib.changelog_section(text, "1.0.0"), "- a\n")
        self.assertIsNone(pluginlib.changelog_section(text, "1.0"))
        self.assertIsNone(pluginlib.changelog_section(text, "0.9.0"))

    def test_heading_does_not_swallow_the_next_line(self) -> None:
        text = "## 1.0.0\n\n- 第一行，\n  续行\n"
        self.assertEqual(pluginlib.changelog_section(text, "1.0.0"), "- 第一行，\n  续行\n")

    def test_lock_packages(self) -> None:
        text = (
            "# python: 3.12\n"
            "#> lz4-4.4.4-cp312-cp312-manylinux_2_17_x86_64.whl 1 sha256:" + "a" * 64 + "\n"
            "lz4==4.4.4 \\\n    --hash=sha256:" + "a" * 64 + "\n"
            "#> six-1.17.0-py2.py3-none-any.whl 1 sha256:" + "b" * 64 + "\n"
            "Six==1.17.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
        )
        self.assertEqual(
            pluginlib.lock_packages(text),
            [
                {"name": "lz4", "version": "4.4.4", "native": True},
                {"name": "six", "version": "1.17.0", "native": False},
            ],
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
