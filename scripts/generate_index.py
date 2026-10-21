#!/usr/bin/env python3
"""Build ``index.json`` (schema 2) from git tags, ``store.json``, and release records.

Where each field comes from:

- Per version (permissions, sdk, requires, dependencies, notes, README): the plugin
  directory **at that version's tag** (``git show <id>-v<version>:plugins/<id>/…``).
  Old versions keep what they asked for even after the manifest on main changed.
  Without the tag (shallow clone) the version is copied from ``--previous``.
- Per plugin (summary, category, author, icon, yanked, deprecated): ``store.json`` in
  the working tree, so a metadata fix does not need a new release. Name and
  description come from the newest version.
- Download fields (url, sha256, size, released_at): ``--previous`` (any schema)
  overlaid with ``--release`` / ``--releases``. A tag without such a record was never
  published and is left out.

    python3 scripts/generate_index.py --update                    # refresh index.json in place
    python3 scripts/generate_index.py --check index.json          # schema + reproducible
    python3 scripts/generate_index.py --lint                      # store.json, icon, CHANGELOG
    python3 scripts/generate_index.py --previous index.json \\
        --release hello-note 0.1.0 <sha256> <size> 2026-10-13T00:00:00Z \\
        --assets dist/assets --sign --output dist/index.json --sig dist/index.json.sig
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pluginlib import (
    CHANGELOG,
    ICON_MAX_BYTES,
    IMAGE_TYPES,
    MANIFEST,
    REPO,
    SHA256_RE,
    VERSION_RE,
    PluginError,
    asset_url,
    catalog_url,
    changelog_section,
    infer_category,
    is_prerelease,
    load_store,
    lock_packages,
    lock_path_of,
    parse_toml,
    permissions_from,
    plugin_table,
    requires_of,
    table_of,
    version_sort_key,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "index.schema.json"
SCHEMA = 2
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _dumps(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------- git
class Git:
    """Read files as they were at a tag. ``None`` means the tag or the file is absent."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tags: set[str] | None = None

    def _run(self, *args: str) -> bytes | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                check=False,
            )
        except OSError:
            return None
        return proc.stdout if proc.returncode == 0 else None

    def tags(self) -> set[str]:
        if self._tags is None:
            out = self._run("tag", "--list")
            self._tags = set(out.decode().split()) if out else set()
        return self._tags

    def show(self, ref: str, path: str) -> bytes | None:
        if ref not in self.tags():
            return None
        return self._run("show", f"refs/tags/{ref}:{path}")

    def has_tree(self, ref: str, path: str) -> bool:
        if ref not in self.tags():
            return False
        out = self._run("ls-tree", "--name-only", f"refs/tags/{ref}", f"{path}/")
        return bool(out and out.strip())


# ---------------------------------------------------------------------- helpers
def _asset(url: str, data: bytes) -> dict:
    return {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _raw_url(repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"


def _platforms(table: dict) -> list[str]:
    if table["runtime"] == "exec":
        platforms = list(table.get("platforms") or [])
        if not platforms:
            raise PluginError(f"{table['id']} 是 exec，清单里必须有 platforms")
        return [str(item) for item in platforms]
    return ["any"]


def _image(directory: Path, plugin_id: str, value: str, label: str, repo: str) -> tuple[dict, str, bytes]:
    """A store.json image path → (asset, catalog file name, bytes). Paths only, no URLs:
    the catalog hosts every image itself, named by content, so a mirror cannot swap it."""
    if value.startswith(("http://", "https://")):
        # 指向本仓库里这个插件自己目录的 raw 地址，按目录里的文件处理
        own = re.match(
            rf"^https://raw\.githubusercontent\.com/{re.escape(repo)}/[^/]+/plugins/{re.escape(plugin_id)}/(.+)$",
            value,
        )
        if own is None:
            raise PluginError(
                f"{plugin_id} 的 {label} 写的是外部网址；请把图片放进插件目录，写相对路径（如 icon.svg）"
            )
        value = own.group(1)
    path = (directory / value).resolve()
    if directory.resolve() not in path.parents or not path.is_file():
        raise PluginError(f"{plugin_id} 的 {label} {value!r} 须是插件目录里的文件")
    suffix = path.suffix.lower()
    if suffix not in IMAGE_TYPES:
        raise PluginError(f"{plugin_id} 的 {label} 只能是 {'、'.join(IMAGE_TYPES)}")
    data = path.read_bytes()
    if len(data) > ICON_MAX_BYTES:
        raise PluginError(f"{plugin_id} 的 {label} 有 {len(data)} 字节，上限 {ICON_MAX_BYTES}")
    if suffix == ".svg" and re.search(rb"<script|\son\w+\s*=|javascript:", data, re.IGNORECASE):
        raise PluginError(f"{plugin_id} 的 {label} 里有脚本；SVG 图标只能是纯图形")
    digest = hashlib.sha256(data).hexdigest()
    name = f"{plugin_id}-{label}-{digest[:16]}{suffix}"
    return _asset(catalog_url(repo, name), data), name, data


# ---------------------------------------------------------------------- versions
def _snapshot_from_tag(git: Git, repo: str, plugin_id: str, version: str) -> dict | None:
    """Everything a version declared, read from its tag. None when the tag is not here."""
    tag = f"{plugin_id}-v{version}"
    base = f"plugins/{plugin_id}"
    raw = git.show(tag, f"{base}/{MANIFEST}")
    if raw is None:
        return None
    data = parse_toml(raw.decode("utf-8"), f"{tag}:{base}/{MANIFEST}")
    table = table_of(data)
    if table["id"] != plugin_id or table["version"] != version:
        raise PluginError(f"tag {tag} 里的清单是 {table['id']} {table['version']}，与 tag 不一致")
    dependencies = None
    lock = lock_path_of(data)
    if lock is not None:
        text = git.show(tag, f"{base}/{lock}")
        if text is None:
            raise PluginError(f"tag {tag} 的清单写了锁文件 {lock}，但 tag 里没有这个文件")
        dependencies = {
            "lock": _asset(_raw_url(repo, tag, f"{base}/{lock}"), text),
            "packages": lock_packages(text.decode("utf-8")),
        }
    readme = git.show(tag, f"{base}/README.md")
    changelog = git.show(tag, f"{base}/{CHANGELOG}")
    notes = changelog_section(changelog.decode("utf-8"), version) if changelog else None
    return {
        "name": table["title"],
        "description": table["description"],
        "runtime": table["runtime"],
        "sdk": table["sdk"],
        "requires": requires_of(data),
        "platforms": _platforms(table),
        "permissions": permissions_from(data),
        "web": git.has_tree(tag, f"{base}/web"),
        "dependencies": dependencies,
        "readme": _asset(_raw_url(repo, tag, f"{base}/README.md"), readme) if readme else None,
        "notes": notes or "",
    }


_SNAPSHOT_KEYS = (
    "runtime",
    "sdk",
    "requires",
    "platforms",
    "permissions",
    "web",
    "dependencies",
    "readme",
    "notes",
)


def _snapshot_from_previous(previous_plugin: dict | None, version: str) -> dict | None:
    """Schema 2 versions carry their own snapshot. Schema 1 did not, so no fallback there."""
    if not previous_plugin:
        return None
    for item in previous_plugin.get("versions") or []:
        if item.get("version") == version and all(key in item for key in _SNAPSHOT_KEYS):
            snapshot = {key: item[key] for key in _SNAPSHOT_KEYS}
            snapshot["name"] = previous_plugin.get("name", "")
            snapshot["description"] = previous_plugin.get("description", "")
            return snapshot
    return None


def _artifact(art: dict, plugin_id: str, version: str, repo: str) -> dict:
    filename = str(art.get("filename") or f"{plugin_id}-{version}.mcplugin")
    return {
        "platform": str(art.get("platform") or "any"),
        "filename": filename,
        "url": str(art.get("url") or asset_url(repo, plugin_id, version, filename)),
        "sha256": str(art.get("sha256") or ""),
        "size": int(art.get("size") or 0),
    }


def _published_from_previous(previous_plugin: dict | None, plugin_id: str, repo: str) -> dict[str, dict]:
    """version → {released_at, artifacts} from an index of any schema."""
    found: dict[str, dict] = {}
    for item in (previous_plugin or {}).get("versions") or []:
        if not isinstance(item, dict) or not isinstance(item.get("version"), str):
            continue
        version = item["version"]
        artifacts = [
            _artifact(art, plugin_id, version, repo)
            for art in item.get("artifacts") or []
            if isinstance(art, dict)
        ]
        released_at = item.get("released_at")
        found[version] = {
            "released_at": released_at if isinstance(released_at, str) else "",
            "artifacts": artifacts,
        }
    return found


def _apply_release(published: dict[str, dict], plugin_id: str, repo: str, record: dict, platforms: list[str]) -> None:
    version = str(record["version"])
    if not VERSION_RE.fullmatch(version):
        raise PluginError(f"{plugin_id} 的发布版本 {version!r} 不合法")
    sha = str(record["sha256"])
    if not SHA256_RE.fullmatch(sha):
        raise PluginError(f"{plugin_id} {version} 的 sha256 须是 64 位小写十六进制")
    size = record["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise PluginError(f"{plugin_id} {version} 的 size 须是非负整数")
    released_at = str(record["released_at"])
    if not GENERATED_AT_RE.fullmatch(released_at):
        raise PluginError(f"{plugin_id} {version} 的 released_at 须是 UTC，如 2026-10-13T00:00:00Z")
    platform = record.get("platform")
    if not platform:
        if platforms != ["any"] and len(platforms) != 1:
            raise PluginError(f"{plugin_id} 声明了多个 platforms，这条发布记录必须带 platform")
        platform = platforms[0]
    filename = str(record.get("filename") or f"{plugin_id}-{version}.mcplugin")
    current = published.get(version) or {"released_at": released_at, "artifacts": []}
    current["released_at"] = released_at
    artifact = {
        "platform": str(platform),
        "filename": filename,
        "url": asset_url(repo, plugin_id, version, filename),
        "sha256": sha,
        "size": size,
    }
    others = [item for item in current["artifacts"] if item["platform"] != artifact["platform"]]
    current["artifacts"] = sorted([*others, artifact], key=lambda item: item["platform"])
    published[version] = current


# ---------------------------------------------------------------------- plugins
def _plugin_document(
    directory: Path,
    *,
    repo: str,
    git: Git,
    previous_plugin: dict | None,
    records: list[dict],
    assets: dict[str, bytes],
) -> dict | None:
    table = plugin_table(directory)
    plugin_id = table["id"]
    store = load_store(directory)
    published = _published_from_previous(previous_plugin, plugin_id, repo)
    # The release being published is at HEAD, which is also its tag: read platforms there.
    for record in records:
        snap = _snapshot_from_tag(git, repo, plugin_id, str(record["version"]))
        platforms = snap["platforms"] if snap else _platforms(table)
        _apply_release(published, plugin_id, repo, record, platforms)
    if not published:
        return None
    unknown = sorted(set(store["yanked"]) - set(published))
    if unknown:
        raise PluginError(f"{plugin_id} 的 store.json 撤回了没发布过的版本：{'、'.join(unknown)}")

    versions: list[dict] = []
    snapshots: dict[str, dict] = {}
    for version, release in published.items():
        snap = _snapshot_from_tag(git, repo, plugin_id, version) or _snapshot_from_previous(
            previous_plugin, version
        )
        if snap is None:
            raise PluginError(
                f"读不到 {plugin_id} {version} 当时的清单：本地没有 tag {plugin_id}-v{version}"
                "（浅克隆？用 git fetch --tags），已有索引里也没有这一版的记录"
            )
        if not release["artifacts"]:
            raise PluginError(f"{plugin_id} {version} 没有下载文件")
        snapshots[version] = snap
        reason = store["yanked"].get(version)
        tag = f"{plugin_id}-v{version}"
        versions.append(
            {
                "version": version,
                "prerelease": is_prerelease(version),
                "released_at": release["released_at"],
                "yanked": {"reason": reason.strip()} if reason else None,
                "notes": snap["notes"],
                "changelog_url": f"https://github.com/{repo}/releases/tag/{tag}",
                "source_url": f"https://github.com/{repo}/tree/{tag}/plugins/{plugin_id}",
                "readme": snap["readme"],
                "runtime": snap["runtime"],
                "sdk": snap["sdk"],
                "requires": snap["requires"],
                "platforms": snap["platforms"],
                "permissions": snap["permissions"],
                "web": snap["web"],
                "dependencies": snap["dependencies"],
                "artifacts": release["artifacts"],
            }
        )
    versions.sort(key=lambda item: version_sort_key(item["version"]), reverse=True)
    usable = [item for item in versions if item["yanked"] is None]
    stable = [item for item in usable if not item["prerelease"]]
    newest = snapshots[versions[0]["version"]]

    icon = None
    if store["icon"]:
        icon, name, data = _image(directory, plugin_id, store["icon"], "icon", repo)
        assets[name] = data
    screenshots = []
    for index, value in enumerate(store["screenshots"], start=1):
        shot, name, data = _image(directory, plugin_id, value, f"screenshot{index}", repo)
        assets[name] = data
        screenshots.append(shot)
    tree = f"https://github.com/{repo}/tree/main/plugins/{plugin_id}"
    return {
        "id": plugin_id,
        "name": newest["name"],
        "summary": store["summary"],
        "description": newest["description"] or store["summary"],
        "category": store["category"] or infer_category(newest["requires"]),
        "author": {"name": store["author"], "url": store["author_url"]},
        "homepage": store["homepage"] or tree,
        "repository": tree,
        "license": store["license"],
        "origin": store["origin"],
        "icon": icon,
        "screenshots": screenshots,
        "deprecated": store["deprecated"],
        "latest": usable[0]["version"] if usable else None,
        "latest_stable": stable[0]["version"] if stable else None,
        "versions": versions,
    }


def build_index(
    *,
    plugins_dir: Path,
    repo: str,
    generated_at: str,
    previous: dict | None,
    releases: list[dict],
    signed: bool,
    key_id: str | None,
    git_root: Path | None = None,
    assets: dict[str, bytes] | None = None,
) -> dict:
    if not GENERATED_AT_RE.fullmatch(generated_at):
        raise PluginError("generated_at 须是 UTC，如 2026-10-13T00:00:00Z")
    if signed and not key_id:
        raise PluginError("签名索引必须带 key_id")
    if not signed and key_id is not None:
        raise PluginError("未签名索引的 key_id 须是 null")
    git = Git(git_root or plugins_dir.parent)
    assets = {} if assets is None else assets
    by_id: dict[str, list[dict]] = {}
    for record in releases:
        by_id.setdefault(str(record["id"]), []).append(record)
    previous_by_id = {
        item["id"]: item
        for item in (previous or {}).get("plugins") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    manifests = sorted(plugins_dir.glob(f"*/{MANIFEST}")) if plugins_dir.is_dir() else []
    known = {path.parent.name for path in manifests}
    missing = sorted(set(by_id) - known)
    if missing:
        raise PluginError(f"发布记录里的插件在 {plugins_dir} 中不存在：{'、'.join(missing)}")
    plugins: list[dict] = []
    for manifest in manifests:
        directory = manifest.parent
        plugin_id = plugin_table(directory)["id"]
        if directory.name != plugin_id:
            raise PluginError(f"{directory} 的目录名须与清单 id {plugin_id} 一致")
        document = _plugin_document(
            directory,
            repo=repo,
            git=git,
            previous_plugin=previous_by_id.get(plugin_id),
            records=by_id.get(plugin_id, []),
            assets=assets,
        )
        if document is not None:
            plugins.append(document)
    plugins.sort(key=lambda item: item["id"])
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "signed": signed,
        "key_id": key_id,
        "repository": f"https://github.com/{repo}",
        "plugins": plugins,
    }


# ---------------------------------------------------------------------- lint / check
def lint(plugins_dir: Path) -> list[str]:
    """Problems a PR should fix before it can be released. Empty list = fine."""
    problems: list[str] = []
    for manifest in sorted(plugins_dir.glob(f"*/{MANIFEST}")) if plugins_dir.is_dir() else []:
        directory = manifest.parent
        try:
            table = plugin_table(directory)
            store = load_store(directory)
            for label, value in [("icon", store["icon"]), *(
                (f"screenshot{i}", v) for i, v in enumerate(store["screenshots"], start=1)
            )]:
                if value:
                    _image(directory, table["id"], value, label, REPO)
            changelog = directory / CHANGELOG
            if not changelog.is_file():
                raise PluginError(f"{directory.name} 没有 {CHANGELOG}")
            section = changelog_section(changelog.read_text(encoding="utf-8"), table["version"])
            if not section:
                raise PluginError(
                    f"{directory.name}/{CHANGELOG} 里没有「## {table['version']}」一节（或这一节是空的）："
                    "它会成为 Release 正文和市场里的更新说明"
                )
        except PluginError as exc:
            problems.append(str(exc))
    return problems


def check_index(path: Path, schema_path: Path = SCHEMA_PATH) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise PluginError("校验索引需要 jsonschema：pip install jsonschema") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if schema.get("properties", {}).get("schema", {}).get("const") != SCHEMA:
        raise PluginError(f"{schema_path} 不是 schema v{SCHEMA}")
    document = json.loads(path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(document), key=lambda err: list(err.path))
    if errors:
        first = errors[0]
        where = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise PluginError(f"{path} 不符合 {schema_path.name}：{where}：{first.message}")


def sign_bytes(raw: bytes, pem: str) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise PluginError("RELEASE_SIGNING_KEY 须是 Ed25519 私钥 PEM")
    return base64.b64encode(key.sign(raw)) + b"\n"


def _load_previous(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.is_file():
        raise PluginError(f"找不到已有索引 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PluginError(f"{path} 不是 JSON 对象")
    return data


def _load_releases(path: Path | None, inline: list[list[str]]) -> list[dict]:
    records: list[dict] = []
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("releases", data)
        if not isinstance(data, list):
            raise PluginError("--releases 须是数组，或 {releases: [...]}")
        for item in data:
            if not isinstance(item, dict):
                raise PluginError("--releases 的每一项须是对象")
            records.append(item)
    for item in inline:
        plugin_id, version, sha, size, released_at = item
        records.append(
            {
                "id": plugin_id,
                "version": version,
                "sha256": sha,
                "size": int(size),
                "released_at": released_at,
            }
        )
    return records


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate or check the plugin catalog index.")
    parser.add_argument("--plugins", type=Path, default=ROOT / "plugins")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", REPO))
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--releases", type=Path, default=None)
    parser.add_argument(
        "--release",
        action="append",
        nargs=5,
        metavar=("ID", "VERSION", "SHA256", "SIZE", "RELEASED_AT"),
        default=[],
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--assets", type=Path, default=None, help="write catalog images here")
    parser.add_argument("--sign", action="store_true")
    parser.add_argument(
        "--key-id",
        default=os.environ.get("RELEASE_SIGNING_KEY_ID") or "catalog-1",
    )
    parser.add_argument("--sig", type=Path, default=None)
    parser.add_argument("--check", type=Path, default=None)
    parser.add_argument("--lint", action="store_true")
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate ROOT/index.json from itself (after editing store.json)",
    )
    args = parser.parse_args(argv)
    try:
        if args.lint:
            problems = lint(args.plugins)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            if not problems:
                print("ok plugins")
            return 1 if problems else 0
        if args.check is not None:
            check_index(args.check)
            committed = json.loads(args.check.read_text(encoding="utf-8"))
            again = build_index(
                plugins_dir=args.plugins,
                repo=args.repo,
                generated_at=committed["generated_at"],
                previous=committed,
                releases=[],
                signed=False,
                key_id=None,
            )
            again["signed"], again["key_id"] = committed["signed"], committed["key_id"]
            if _dumps(again) != args.check.read_text(encoding="utf-8"):
                raise PluginError(
                    f"{args.check} 与插件目录不一致：改了 store.json / 图标后请运行 "
                    "python3 scripts/generate_index.py --update 并提交 index.json"
                )
            print(f"ok {args.check}")
            return 0
        if args.update:
            target = ROOT / "index.json"
            current = _load_previous(target)
            document = build_index(
                plugins_dir=args.plugins,
                repo=args.repo,
                generated_at=_now(),
                previous=current,
                releases=[],
                signed=False,
                key_id=None,
            )
            if current is not None:
                # signed / key_id describe the published copy; keep what main already says
                document["signed"], document["key_id"] = current["signed"], current["key_id"]
                if _dumps(dict(document, generated_at=current["generated_at"])) == _dumps(current):
                    print("index.json 没有变化")
                    return 0
            target.write_text(_dumps(document), encoding="utf-8")
            check_index(target)
            print(f"wrote {target}")
            return 0
        generated_at = args.generated_at or _now()
        signed = bool(args.sign)
        key_id = args.key_id if signed else None
        assets: dict[str, bytes] = {}
        document = build_index(
            plugins_dir=args.plugins,
            repo=args.repo,
            generated_at=generated_at,
            previous=_load_previous(args.previous),
            releases=_load_releases(args.releases, args.release),
            signed=signed,
            key_id=key_id,
            assets=assets,
        )
        text = _dumps(document)
        raw = text.encode("utf-8")
        if args.output is None:
            sys.stdout.buffer.write(raw)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(raw)
            print(f"wrote {args.output}", file=sys.stderr)
        if args.assets is not None:
            args.assets.mkdir(parents=True, exist_ok=True)
            for name, data in sorted(assets.items()):
                (args.assets / name).write_bytes(data)
                print(f"wrote {args.assets / name}", file=sys.stderr)
        if signed:
            pem = os.environ.get("RELEASE_SIGNING_KEY", "")
            if not pem.strip():
                raise PluginError("签了名却没有环境变量 RELEASE_SIGNING_KEY")
            signature = sign_bytes(raw, pem)
            sig_path = args.sig
            if sig_path is None:
                if args.output is None:
                    raise PluginError("签名请同时给出 --output 与 --sig")
                sig_path = args.output.with_name(args.output.name + ".sig")
            sig_path.write_bytes(signature)
            print(f"wrote {sig_path}", file=sys.stderr)
        if args.output is not None and SCHEMA_PATH.is_file():
            check_index(args.output)
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: JSON：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
