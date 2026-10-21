#!/usr/bin/env python3
"""Check that TAG is ``<id>-v<version>``, matches ``plugins/<id>``, and has CHANGELOG notes.

Writes ``id`` and ``version`` to stdout and, when set, to ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pluginlib import ID_RE, PluginError, plugin_table, split_tag
from release_notes import notes

ROOT = Path(__file__).resolve().parents[1]


def emit(key: str, value: str) -> None:
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def main() -> int:
    tag = os.environ.get("TAG", "")
    try:
        plugin_id, version = split_tag(tag)
        if not ID_RE.fullmatch(plugin_id):
            raise PluginError(f"tag 里的 id {plugin_id!r} 不合法")
        directory = ROOT / "plugins" / plugin_id
        table = plugin_table(directory)
        if table["id"] != plugin_id:
            raise PluginError(f"清单 id {table['id']} 与 tag 里的 {plugin_id} 不一致")
        if table["version"] != version:
            raise PluginError(
                f"清单 version 是 {table['version']}，tag {tag} 是 {version}；两边必须相同"
            )
        notes(plugin_id, version, ROOT)
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    emit("id", plugin_id)
    emit("version", version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
