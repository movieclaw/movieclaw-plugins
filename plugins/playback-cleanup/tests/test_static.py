"""静态检查：清单与代码一致、页面不注入 HTML、路由 operation_id 合规、入口能被宿主导入。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "index.html").read_text("utf-8")
ENTRY = (ROOT / "playback_cleanup" / "__init__.py").read_text("utf-8")
MANIFEST = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text("utf-8"))


def test_panel_never_injects_html():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in HTML, sink


def test_panel_uses_plugin_prefix():
    assert 'const BASE = "/api/v1/plugins/playback-cleanup"' in HTML
    assert 'credentials: "same-origin"' in HTML
    for tab in ("概览", "设置", "清理记录", "播放结束后延迟秒数", "兜底扫描", "超管"):
        assert tab in HTML


def test_operation_ids_prefixed():
    ids = re.findall(r'operation_id="([^"]+)"', ENTRY)
    assert ids and all(i.startswith("plugins.playback-cleanup.") for i in ids)
    assert len(ids) == len(set(ids))


def test_manifest_matches_code():
    import playback_cleanup as entry

    assert MANIFEST["plugin"]["id"] == entry.PLUGIN_ID
    assert MANIFEST["plugin"]["version"] == "0.1.3"
    assert tuple(MANIFEST["permissions"]["operations"]) == entry.OPERATIONS
    assert MANIFEST["requires"] == entry.REQUIRES
    assert MANIFEST["permissions"]["events"] == ["playback.stopped", "playback.completed"]
    assert "network" not in MANIFEST["permissions"] and "paths" not in MANIFEST["permissions"]
    # 危险写操作必须逐条列出
    assert "playback.sync.state" in entry.OPERATIONS


def test_event_versions_match_sdk():
    from movieclaw_sdk.events import PLAYBACK_COMPLETED, PLAYBACK_STOPPED

    import playback_cleanup as entry

    for ev in (PLAYBACK_STOPPED, PLAYBACK_COMPLETED):
        assert entry.REQUIRES[ev.name] == f"^{ev.version}"
