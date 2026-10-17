"""Shared helpers for packing plugins and building the catalog index.

The skip rules match ``mclaw plugin pack`` (``cli/internal/overlay/plugin.go``)
and the plugin-dev ``check_plugin.py``. Zip timestamps and permission bits are
normalized here so the archive hash does not depend on the build machine.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

MANIFEST = "movieclaw-plugin.toml"
STORE = "store.json"

# Same shape as movieclaw_api.plugins.packages._ID / _VERSION.
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# Split on the last ``-v`` so ids may contain hyphens.
TAG_RE = re.compile(rf"^(?P<id>.+)-v(?P<version>{VERSION_RE.pattern[1:-1]})$")
SDK_RE = re.compile(r"^\^?\d+\.\d+$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

# Zip has no dates before 1980. Pin every entry so hashes stay stable.
ZIP_DATE = (2026, 10, 13, 0, 0, 0)
REPO = "movieclaw/movieclaw-plugins"
CATALOG_TAG = "catalog"

STORE_REQUIRED = ("author", "homepage", "license", "origin")
STORE_OPTIONAL = ("icon", "screenshots", "summary", "min_app", "max_app", "app")


class PluginError(ValueError):
    """A plugin directory or release record is not usable. The message is for humans."""


def skip_dir(name: str) -> bool:
    return name in {"__pycache__", ".git", ".venv", "node_modules"} or name.startswith(".")


def skip_file(name: str) -> bool:
    return (
        name.endswith((".pyc", ".mcplugin"))
        or name == ".DS_Store"
        or name.startswith("._")
    )


def package_files(directory: Path) -> list[Path]:
    """Files that belong in the archive, sorted by repo-relative posix path."""
    directory = directory.resolve()
    found: list[Path] = []
    for root, dirs, names in os.walk(directory):
        root_path = Path(root)
        dirs[:] = sorted(name for name in dirs if not skip_dir(name))
        for name in sorted(names):
            if skip_file(name):
                continue
            path = root_path / name
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink():
                raise PluginError(f"插件目录里不能有软链接：{rel}")
            found.append(path)
    found.sort(key=lambda path: path.relative_to(directory).as_posix())
    return found


def load_toml(directory: Path) -> dict:
    path = directory / MANIFEST
    if not path.is_file():
        raise PluginError(f"{directory} 下没有 {MANIFEST}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"{path} 不是合法的 TOML：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugin"), dict):
        raise PluginError(f"{path} 缺少 [plugin] 表")
    return data


def plugin_table(directory: Path) -> dict:
    table = dict(load_toml(directory)["plugin"])
    table.setdefault("runtime", "process")
    table.setdefault("sdk", "^1.0")
    table.setdefault("description", "")
    table.setdefault("entry", "")
    table.setdefault("platforms", [])
    for key in ("id", "title", "version"):
        if not isinstance(table.get(key), str) or not table[key]:
            raise PluginError(f"[plugin].{key} 必填")
    if not ID_RE.fullmatch(table["id"]):
        raise PluginError(
            f"id {table['id']!r} 须小写字母开头，只用小写字母、数字和连字符，3～40 位，不含点"
        )
    if not VERSION_RE.fullmatch(table["version"]):
        raise PluginError(f"version {table['version']!r} 须是 主.次.修订")
    if not SDK_RE.fullmatch(str(table["sdk"])):
        raise PluginError(f"sdk {table['sdk']!r} 只能写成 ^主.次（例如 ^1.0）")
    runtime = table["runtime"]
    if runtime not in {"process", "inline", "exec"}:
        raise PluginError(f"runtime {runtime!r} 只能是 process、inline 或 exec")
    return table


def dependency_lock(directory: Path) -> Path | None:
    """The lock file named by ``[dependencies] lock``, or None when there is no such table.

    It must sit inside the plugin directory and survive the pack skip rules, or the
    server would see a manifest pointing at a file the archive does not carry.
    """
    data = load_toml(directory)
    deps = data.get("dependencies")
    if deps is None:
        return None
    if not isinstance(deps, dict) or not isinstance(deps.get("lock"), str) or not deps["lock"]:
        raise PluginError('[dependencies] 须写成 lock = "requirements.lock"')
    if data["plugin"].get("runtime", "process") != "process":
        raise PluginError('第三方依赖只给 runtime = "process" 的插件')
    root = directory.resolve()
    path = (root / deps["lock"]).resolve()
    if root not in path.parents:
        raise PluginError(f"锁文件 {deps['lock']!r} 须在插件目录里")
    if not path.is_file():
        raise PluginError(f"清单写了锁文件 {deps['lock']!r}，但插件目录里没有这个文件")
    if path not in package_files(directory):
        raise PluginError(f"锁文件 {deps['lock']!r} 会被打包规则跳过（点开头的文件或目录）")
    return path


def load_store(directory: Path) -> dict:
    path = directory / STORE
    if not path.is_file():
        raise PluginError(f"{directory} 下没有 {STORE}（商店元数据不能写进清单）")
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PluginError(f"{path} 不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise PluginError(f"{path} 须是 JSON 对象")
    unknown = set(data) - set(STORE_REQUIRED) - set(STORE_OPTIONAL)
    if unknown:
        names = "、".join(sorted(unknown))
        raise PluginError(f"{path} 有索引不认识的字段：{names}")
    for key in STORE_REQUIRED:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise PluginError(f"{path} 的 {key} 须是非空字符串")
    if data["origin"] != "official":
        raise PluginError(f"{path} 的 origin 在本仓库只能是 official")
    icon = data.get("icon", None)
    if icon is not None and not isinstance(icon, str):
        raise PluginError(f"{path} 的 icon 须是字符串或 null")
    shots = data.get("screenshots", [])
    if not isinstance(shots, list) or not all(isinstance(item, str) for item in shots):
        raise PluginError(f"{path} 的 screenshots 须是字符串数组")
    for key in ("min_app", "max_app", "app", "summary"):
        value = data.get(key, None)
        if value is not None and not isinstance(value, str):
            raise PluginError(f"{path} 的 {key} 须是字符串或 null")
    data.setdefault("icon", None)
    data.setdefault("screenshots", [])
    data.setdefault("summary", "")
    data.setdefault("min_app", None)
    data.setdefault("max_app", None)
    data.setdefault("app", None)
    return data


def permissions_of(directory: Path) -> dict:
    raw = load_toml(directory).get("permissions") or {}
    if not isinstance(raw, dict):
        raise PluginError("[permissions] 须是表")
    operations = raw.get("operations") or []
    paths = raw.get("paths") or []
    callbacks = raw.get("callbacks") or []
    events = raw.get("events") or []
    network = bool(raw.get("network", False))
    for label, value in (
        ("operations", operations),
        ("callbacks", callbacks),
        ("events", events),
    ):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PluginError(f"[permissions].{label} 须是字符串数组")
    if not isinstance(paths, list):
        raise PluginError("[permissions].paths 须是数组")
    norm_paths = []
    for item in paths:
        if not isinstance(item, dict):
            raise PluginError("[permissions].paths 的每一项须是 {path, mode}")
        norm_paths.append(
            {
                "path": str(item.get("path", "")),
                "mode": str(item.get("mode", "read")),
            }
        )
    return {
        "operations": list(operations),
        "paths": norm_paths,
        "network": network,
        "callbacks": list(callbacks),
        "events": list(events),
    }


def split_tag(tag: str) -> tuple[str, str]:
    match = TAG_RE.fullmatch(tag)
    if match is None or not ID_RE.fullmatch(match.group("id")):
        raise PluginError(
            f"tag {tag!r} 须是 <id>-v<版本>，例如 hello-note-v0.1.0；"
            "id 不含点，版本是 主.次.修订"
        )
    version = match.group("version")
    if not VERSION_RE.fullmatch(version):
        raise PluginError(f"tag 里的版本 {version!r} 不合法")
    return match.group("id"), version


def version_sort_key(version: str) -> tuple:
    """Newest-first key. A plain release sorts ahead of a prerelease with the same core."""
    match = VERSION_RE.fullmatch(version)
    if match is None:
        return (0, 0, 0, False, version)
    core, _, rest = version.partition("-")
    if "+" in core and not rest:
        core, _, rest = version.partition("+")
        rest = "+" + rest
    major, minor, patch = (int(part) for part in core.split("."))
    return (major, minor, patch, rest == "", rest)


def asset_url(repo: str, plugin_id: str, version: str, filename: str) -> str:
    tag = f"{plugin_id}-v{version}"
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def catalog_url(repo: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{CATALOG_TAG}/{name}"


def public_url(repo: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value.startswith(("https://", "http://")):
        return value
    return catalog_url(repo, value.lstrip("/"))
