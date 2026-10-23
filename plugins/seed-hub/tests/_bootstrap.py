"""让纯函数单测在两种环境里都能跑。

装好了插件运行时的环境（上游 CI、本机开发）直接导入真实模块；没装 ``pydantic`` /
``fastapi`` / ``movieclaw_*`` 的机器上，用 AST 把纯函数（连同依赖的模块级常量）抽出来，
塞进一个桩模块执行。这样单测只依赖标准库，跑得动就是真的跑过。
"""

import ast
import sys
import types
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent


def _multi_targets(*names: str) -> set[str]:
    """一行多赋值（``a, b = 3, 2``）里的名字：抽整句赋值即可。"""
    return set(names)


def _real_available() -> bool:
    try:
        import pydantic  # noqa: F401,PLC0415
        import movieclaw_sdk  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001 -- 装不上就走下面的抽取路径
        return False
    return True


if _real_available():
    sys.path.insert(0, str(SOURCES))
else:
    SOURCE = SOURCES / "seed_hub.py"

    # 要抽出来的东西：纯函数 + 它们依赖的模块级常量。
    WANTED_FUNCS = {
        "remember_site_nicknames", "_norm", "split_lines", "split_tags", "qb_tags",
        "convert_save_path", "is_excluded_path", "torrent_file_candidates",
        "needs_trackers", "patch_announce", "info_hash_of", "_announce_urls",
        "is_completed", "qb_seeding_paused", "tr_verify_done", "compact_hash_json",
        "index_sha1", "parse_sites", "parse_matches", "domain_of", "slug_of",
        "extract_credentials", "apply_credentials", "fill_download_template",
        "build_download_url", "_norm_title", "_title_match", "tracker_dead_reason",
        "tr_torrent_verdict", "qb_torrent_verdict", "sitename_from_labels",
        "site_tag_from_trackers",
    }
    WANTED_ASSIGNS = {
        "_SITE_NICKNAMES", "_SLUG_ALIASES", "_DOWNLOAD_UNKNOWN_PARAMS",
        "_QB_SEEDING_PAUSED_STATES", "_TR_ERROR_LOCAL", "_TR_ERROR_TRACKER",
        "_TR_ERROR_WARNING", "_TR_NO_DATA_SNIPPET", "_QB_TRACKER_WORKING",
        "_QB_TRACKER_ERROR", "_TRACKER_DEAD_MESSAGES", "_TRACKER_DOMAINS",
        "_BOOL_DEFAULTS", "_MC_OWNED_LABELS",
    }
    WANTED_ASSIGNS |= _multi_targets(*["_TR_ERROR_LOCAL", "_TR_ERROR_TRACKER", "_TR_ERROR_WARNING"])
    WANTED_CLASSES = ("TransferError",)

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    stub = types.ModuleType("seed_hub_stub")
    stub.__dict__["__name__"] = "seed_hub_stub"
    # 被抽出的函数体引用标准库与 typing：塞能通过编译/执行的桩
    import json as _json  # noqa: PLC0415
    import re as _re  # noqa: PLC0415
    import hashlib as _hashlib  # noqa: PLC0415
    from urllib.parse import urlsplit as _urlsplit  # noqa: PLC0415
    stub.__dict__["Any"] = object
    stub.__dict__["json"] = _json
    stub.__dict__["re"] = _re
    stub.__dict__["urlsplit"] = _urlsplit
    stub.__dict__["hashlib"] = _hashlib
    # bencode.py 是纯 Python 且无依赖，桩路径下也直接用真的
    sys.path.insert(0, str(SOURCES))
    import bencode as _benc  # noqa: PLC0415
    stub.__dict__["bdecode"] = _benc.bdecode
    stub.__dict__["bencode"] = _benc.bencode
    wanted: dict[str, ast.AST] = {}

    def _assign_names(node: ast.Assign) -> set[str]:
        out: set[str] = set()
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.add(target.id)
            elif isinstance(target, ast.Tuple):
                out |= {elt.id for elt in target.elts if isinstance(elt, ast.Name)}
        return out

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCS:
            wanted[node.name] = node
        elif isinstance(node, ast.Assign):
            if _assign_names(node) & WANTED_ASSIGNS:
                wanted[_assign_names(node).pop()] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in WANTED_ASSIGNS:
                wanted[node.target.id] = node
        elif isinstance(node, ast.ClassDef) and node.name in WANTED_CLASSES:
            wanted[node.name] = ast.ClassDef(
                name=node.name, bases=[ast.Name(id="Exception", ctx=ast.Load())],
                keywords=[], body=[ast.Pass()], decorator_list=[],
            )
    missing = WANTED_FUNCS - set(wanted)
    if missing:
        raise RuntimeError(f"抽取失败，模块里找不到：{sorted(missing)}")
    module = ast.Module(body=list(wanted.values()), type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), stub.__dict__)  # noqa: S102
    sys.modules["seed_hub"] = stub
