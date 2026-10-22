#!/usr/bin/env python3
"""Choose which plugin directories a pull request must validate.

Relevant changes are ``plugins/<id>/``, ``example/``, ``scripts/``,
``index.schema.json``, and this repo's CI workflow. ``example/hello-note``
is always included when anything relevant changed. Script or schema changes
also validate every plugin that is already in ``plugins/``.

Reference copies listed in ``example/UPSTREAM.json`` are included too.
``check_plugin.py`` against the MovieClaw pin in ``ci.yml`` (v0.42.0,
``bd325adde24e5e035878d3fb668cd0b004c96760``) accepts all eight copied from
MovieClaw ``779712c4d8d2c2c320021ab0e31cde95821dd493``: exit code 0, warnings
only (``watch-presence`` is inline; bundled ids warn that a package replaces
the copy shipped with the app; ``strm-prefetch`` uses contracts it does not
declare). Their upstream tests do not run standalone and are not in these
directories, so the workflow's unittest step finds no ``tests/`` and skips
it. Do not glob ``example/*`` instead of this list. If a later sync fails
the pinned checker, stop emitting that id and say why here — leave the
upstream source unchanged.

Reads ``BASE_SHA`` and writes ``run``, ``dirs`` and ``locks`` to
``$GITHUB_OUTPUT`` when that variable is set. ``dirs`` is a space-separated
list; ``locks`` lists the ``[dependencies]`` lock files of those directories
(repo-relative), which the dependency jobs check and install on both Linux
architectures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pluginlib import ID_RE, PluginError, dependency_lock

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = "example/hello-note"


def reference_example_dirs(root: Path) -> list[str]:
    """Plugin dirs recorded by scripts/sync_examples.py, in file order."""
    record = root / "example" / "UPSTREAM.json"
    if not record.is_file():
        return []
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    items = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    dirs: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plugin_id = item.get("id")
        if not isinstance(plugin_id, str) or not ID_RE.fullmatch(plugin_id):
            continue
        if plugin_id == "hello-note":
            continue
        manifest = root / "example" / plugin_id / "movieclaw-plugin.toml"
        if manifest.is_file():
            dirs.append(f"example/{plugin_id}")
    return dirs


def select(changed: list[str], root: Path = ROOT) -> tuple[bool, list[str]]:
    force_all = False
    plugin_ids: list[str] = []
    saw_relevant = False
    for path in changed:
        if (
            path.startswith("scripts/")
            or path == ".github/workflows/ci.yml"
            or path == "index.schema.json"
        ):
            force_all = True
            saw_relevant = True
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "plugins":
            plugin_ids.append(parts[1])
            saw_relevant = True
        if parts and parts[0] == "example":
            saw_relevant = True
    if not saw_relevant:
        return False, []
    dirs: list[str] = []
    if force_all:
        plugins = root / "plugins"
        if plugins.is_dir():
            for manifest in sorted(plugins.glob("*/movieclaw-plugin.toml")):
                dirs.append(manifest.parent.relative_to(root).as_posix())
    else:
        for plugin_id in sorted(set(plugin_ids)):
            manifest = root / "plugins" / plugin_id / "movieclaw-plugin.toml"
            if manifest.is_file():
                dirs.append(f"plugins/{plugin_id}")
    example = root / EXAMPLE / "movieclaw-plugin.toml"
    if example.is_file() and EXAMPLE not in dirs:
        dirs.append(EXAMPLE)
    for ref in reference_example_dirs(root):
        if ref not in dirs:
            dirs.append(ref)
    return True, dirs


def lock_files(dirs: list[str], root: Path = ROOT) -> list[str]:
    """Repo-relative lock files of the selected plugins that declare ``[dependencies]``."""
    locks: list[str] = []
    for item in dirs:
        try:
            lock = dependency_lock(root / item)
        except PluginError as exc:
            raise PluginError(f"{item}: {exc}") from exc
        if lock is not None:
            locks.append(lock.relative_to(root.resolve()).as_posix())
    return locks


def changed_files(base: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", base, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def emit(key: str, value: str) -> None:
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def main() -> int:
    base = os.environ.get("BASE_SHA", "")
    if not base:
        print("error: BASE_SHA is required", file=sys.stderr)
        return 1
    run, dirs = select(changed_files(base))
    try:
        locks = lock_files(dirs)
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    emit("run", "true" if run else "false")
    emit("dirs", " ".join(dirs))
    emit("locks", " ".join(locks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
