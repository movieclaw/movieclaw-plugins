#!/usr/bin/env python3
"""Pack a plugin directory into a deterministic ``.mcplugin`` archive.

Usage:
    python3 scripts/pack.py example/hello-note
    python3 scripts/pack.py plugins/hello-note --output dist/hello-note-0.1.0.mcplugin
    PYTHONPATH=<MovieClaw>/src python3 scripts/pack.py plugins/foo --lock

Prints one line: ``<filename> SHA256 <hex> SIZE <bytes>``.
Timestamps are pinned and paths are sorted, so the same tree always hashes
the same. File modes are normalized to 0644, or 0755 when the source is
executable. This matches the file set of ``mclaw plugin pack``; it does not
replace the server's ``Manifest`` / ``check_compat`` checks.

A ``[dependencies] lock`` file must be inside the plugin directory and is packed
like any other file. ``--lock`` first regenerates it from ``requirements.in``
with MovieClaw's ``python -m movieclaw_sdk.deps lock``, so the MovieClaw SDK has
to be importable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

from pluginlib import ZIP_DATE, PluginError, dependency_lock, package_files, plugin_table

# Unix file type for a regular file, plus the permission bits the server reads.
_FILE = 0o100000
_MODE_FILE = _FILE | 0o644
_MODE_EXEC = _FILE | 0o755


def _mode(path: Path) -> int:
    bits = path.stat().st_mode
    if bits & stat.S_IXUSR:
        return _MODE_EXEC
    return _MODE_FILE


def make_lock(directory: Path, constraints: str | None) -> None:
    """requirements.in → lock file, via MovieClaw's own resolver (it knows the host packages)."""
    if importlib.util.find_spec("movieclaw_sdk") is None:
        raise PluginError("--lock 需要 MovieClaw 的 SDK：PYTHONPATH=<MovieClaw>/src 或装好 movieclaw")
    command = [sys.executable, "-m", "movieclaw_sdk.deps", "lock", str(directory)]
    if constraints:
        command += ["--constraints", constraints]
    if subprocess.run(command, check=False).returncode != 0:
        raise PluginError("生成锁文件失败（原因见上面的输出）")


def pack(directory: Path, output: Path) -> tuple[str, int]:
    plugin_table(directory)
    dependency_lock(directory)
    resolved = directory.resolve()
    target = output.resolve()
    if target == resolved or resolved in target.parents:
        raise PluginError("--output 不要放在插件目录里面")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for path in package_files(directory):
            rel = path.relative_to(directory.resolve()).as_posix()
            info = zipfile.ZipInfo(filename=rel, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so external_attr is a mode
            info.external_attr = _mode(path) << 16
            info.flag_bits |= 0x800  # UTF-8 names
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return digest, output.stat().st_size


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pack a MovieClaw plugin deterministically.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--lock", action="store_true", help="先按 requirements.in 重新生成 [dependencies] 的锁文件"
    )
    parser.add_argument(
        "--constraints", default=None, help="主程序的 host-constraints.txt（文件或网址），配合 --lock"
    )
    args = parser.parse_args(argv)
    directory = args.directory
    try:
        table = plugin_table(directory)
        output = args.output
        if output is None:
            output = Path(f"{table['id']}-{table['version']}.mcplugin")
        if args.lock:
            make_lock(directory, args.constraints)
        digest, size = pack(directory, output)
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{output.name} SHA256 {digest} SIZE {size}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
