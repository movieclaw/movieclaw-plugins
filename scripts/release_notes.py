#!/usr/bin/env python3
"""Print one version's CHANGELOG section: the Release body and the catalog's notes.

    python3 scripts/release_notes.py cloud-115 0.1.0-beta.2 > notes.md
"""

from __future__ import annotations

import sys
from pathlib import Path

from pluginlib import CHANGELOG, PluginError, changelog_section

ROOT = Path(__file__).resolve().parents[1]


def notes(plugin_id: str, version: str, root: Path = ROOT) -> str:
    path = root / "plugins" / plugin_id / CHANGELOG
    if not path.is_file():
        raise PluginError(f"plugins/{plugin_id} 没有 {CHANGELOG}")
    section = changelog_section(path.read_text(encoding="utf-8"), version)
    if not section:
        raise PluginError(
            f"plugins/{plugin_id}/{CHANGELOG} 里没有「## {version}」一节（或这一节是空的）"
        )
    return section


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：release_notes.py <id> <version>", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(notes(argv[0], argv[1]))
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
