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
CHANGELOG = "CHANGELOG.md"

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

STORE_REQUIRED = ("author", "license", "origin", "summary")
STORE_OPTIONAL = (
    "author_url",
    "homepage",
    "category",
    "icon",
    "screenshots",
    "yanked",
    "deprecated",
)
# Schema 1 fields the server never checked. Say so instead of "unknown field".
STORE_REMOVED = ("min_app", "max_app", "app")
SUMMARY_MAX = 60
ICON_MAX_BYTES = 256 * 1024
IMAGE_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}

# Fixed category ids. The app owns the labels. Order is the inference precedence:
# the first contract in ``requires`` that maps to a category wins.
CATEGORIES = ("storage", "downloader", "messaging", "sites", "library", "tools")
CATEGORY_BY_CONTRACT = {
    "drive-backends": "storage",
    "downloader-adapters": "downloader",
    "im-channels": "messaging",
    "site-classes": "sites",
    "site-data-packs": "sites",
    "ingest-steps": "library",
}


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


def parse_toml(text: str, where: str) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginError(f"{where} 不是合法的 TOML：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("plugin"), dict):
        raise PluginError(f"{where} 缺少 [plugin] 表")
    return data


def load_toml(directory: Path) -> dict:
    path = directory / MANIFEST
    if not path.is_file():
        raise PluginError(f"{directory} 下没有 {MANIFEST}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PluginError(f"{path} 读不出来：{exc}") from exc
    return parse_toml(text, str(path))


def plugin_table(directory: Path) -> dict:
    return table_of(load_toml(directory))


def table_of(data: dict) -> dict:
    """``[plugin]`` with defaults filled in, validated like the server's Manifest."""
    table = dict(data["plugin"])
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
    """``store.json``: what the catalog shows that the server's manifest cannot carry.

    Image fields stay as written (a path inside the plugin directory or an https URL);
    ``generate_index`` turns paths into catalog assets.
    """
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
    # 旧字段服务器从不检查，兼容性改由清单的 sdk 和 [requires] 判断：照旧写着也不报错，直接忽略
    for key in STORE_REMOVED:
        data.pop(key, None)
    unknown = set(data) - set(STORE_REQUIRED) - set(STORE_OPTIONAL)
    if unknown:
        names = "、".join(sorted(unknown))
        raise PluginError(f"{path} 有索引不认识的字段：{names}")
    for key in STORE_REQUIRED:
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise PluginError(f"{path} 的 {key} 须是非空字符串")
    if data["origin"] != "official":
        raise PluginError(f"{path} 的 origin 在本仓库只能是 official")
    summary = data["summary"].strip()
    if len(summary) > SUMMARY_MAX:
        raise PluginError(
            f"{path} 的 summary 有 {len(summary)} 个字，市场卡片上放不下；"
            f"控制在 {SUMMARY_MAX} 字以内，详细说明写清单的 description"
        )
    data["summary"] = summary
    for key in ("author_url", "homepage"):
        value = data.get(key)
        if value is not None and not (isinstance(value, str) and value.startswith("https://")):
            raise PluginError(f"{path} 的 {key} 须是 https 地址或不写")
    category = data.get("category")
    if category is not None and category not in CATEGORIES:
        raise PluginError(f"{path} 的 category 只能是 {'、'.join(CATEGORIES)} 之一，或不写（按接口推断）")
    icon = data.get("icon", None)
    if icon is not None and not isinstance(icon, str):
        raise PluginError(f"{path} 的 icon 须是字符串或 null")
    shots = data.get("screenshots") or []
    if not isinstance(shots, list) or not all(isinstance(item, str) for item in shots):
        raise PluginError(f"{path} 的 screenshots 须是字符串数组")
    yanked = data.get("yanked", {})
    if not isinstance(yanked, dict) or not all(
        isinstance(k, str) and VERSION_RE.fullmatch(k) and isinstance(v, str) and v.strip()
        for k, v in yanked.items()
    ):
        raise PluginError(f"{path} 的 yanked 须是 {{\"版本\": \"撤回原因\"}}")
    deprecated = data.get("deprecated")
    if deprecated is not None:
        if (
            not isinstance(deprecated, dict)
            or set(deprecated) - {"reason", "replaced_by"}
            or not isinstance(deprecated.get("reason"), str)
            or not deprecated["reason"].strip()
        ):
            raise PluginError(f"{path} 的 deprecated 须是 {{\"reason\": …, \"replaced_by\": 插件 id 或 null}}")
        replaced = deprecated.get("replaced_by")
        if replaced is not None and not (isinstance(replaced, str) and ID_RE.fullmatch(replaced)):
            raise PluginError(f"{path} 的 deprecated.replaced_by 须是插件 id 或 null")
        data["deprecated"] = {"reason": deprecated["reason"].strip(), "replaced_by": replaced}
    data.setdefault("author_url", None)
    data.setdefault("homepage", None)
    data.setdefault("category", None)
    data.setdefault("icon", None)
    data["screenshots"] = shots
    data.setdefault("yanked", {})
    data.setdefault("deprecated", None)
    return data


def infer_category(requires: dict) -> str:
    for name in requires:
        if name in CATEGORY_BY_CONTRACT:
            return CATEGORY_BY_CONTRACT[name]
    return "tools"


def requires_of(data: dict) -> dict:
    raw = data.get("requires") or {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise PluginError("[requires] 须是「契约名 = 版本要求」")
    return dict(raw)


def lock_path_of(data: dict) -> str | None:
    """``[dependencies] lock`` as written, or None."""
    deps = data.get("dependencies")
    if deps is None:
        return None
    if not isinstance(deps, dict) or not isinstance(deps.get("lock"), str) or not deps["lock"]:
        raise PluginError('[dependencies] 须写成 lock = "requirements.lock"')
    return deps["lock"]


def lock_packages(text: str) -> list[dict]:
    """``name==version`` lines of a lock file, in file order, plus whether any wheel is native.

    A lighter reading than ``movieclaw_sdk.deps.parse_lock`` (scripts do not import
    MovieClaw). CI's lock check already validated the file.
    """
    packages: list[dict] = []
    wheels: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#>"):
            parts = line[2:].split()
            if parts:
                wheels.append(parts[0])
            continue
        if not line or line.startswith(("#", "--")):
            continue
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s\\;]+)", line)
        if match is None:
            continue
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        native = any(not w.endswith("-none-any.whl") for w in wheels)
        packages.append({"name": name, "version": match.group(2), "native": native})
        wheels = []
    return packages


def permissions_of(directory: Path) -> dict:
    return permissions_from(load_toml(directory))


def permissions_from(data: dict) -> dict:
    raw = data.get("permissions") or {}
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


def is_prerelease(version: str) -> bool:
    return "-" in version.split("+", 1)[0]


def version_sort_key(version: str) -> tuple:
    """Semver precedence as a sort key (larger is newer); build metadata is ignored.

    A release sorts ahead of its prereleases, and prerelease identifiers compare
    numerically when both are numbers (``beta.10`` is newer than ``beta.2``).
    """
    if VERSION_RE.fullmatch(version) is None:
        return (0, 0, 0, 0, ())
    core, _, pre = version.split("+", 1)[0].partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not pre:
        return (major, minor, patch, 1, ())
    idents = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")
    )
    return (major, minor, patch, 0, idents)


def changelog_section(text: str, version: str) -> str | None:
    """The body under ``## <version>`` in a CHANGELOG, or None when there is no such heading.

    Accepts ``## 1.2.0``, ``## v1.2.0``, ``## [1.2.0]`` and a trailing date
    (``## 1.2.0 - 2026-10-21``). The body runs to the next ``##`` heading.
    """
    pattern = re.compile(
        rf"^##[ \t]+\[?v?{re.escape(version)}\]?(?:[ \t]+.*)?$", re.MULTILINE
    )
    match = pattern.search(text)
    if match is None:
        return None
    rest = text[match.end():]
    end = re.search(r"^#{1,2}[ \t]", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    return body.strip("\n").rstrip() + "\n" if body.strip() else ""


def asset_url(repo: str, plugin_id: str, version: str, filename: str) -> str:
    tag = f"{plugin_id}-v{version}"
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def catalog_url(repo: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{CATALOG_TAG}/{name}"
