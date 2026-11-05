"""面板与入口的静态检查：输出一律转义、路由 operation_id 合规、清单与权限一致。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "index.html").read_text("utf-8")
ENTRY = (ROOT / "actor_avatar" / "__init__.py").read_text("utf-8")


def test_panel_never_injects_html():
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert sink not in HTML, sink


def test_panel_uses_same_origin_and_plugin_prefix():
    assert 'credentials: "same-origin"' in HTML
    assert 'const BASE = "/api/v1/plugins/actor-avatar"' in HTML
    # 远程图片一律经主程序图片代理，不直接从面板访问外网
    assert "/api/v1/images/proxy" in HTML
    assert re.search(r"img\.src\s*=\s*proxied\(", HTML)


def test_operation_ids_prefixed():
    ids = re.findall(r'operation_id="([^"]+)"', ENTRY)
    assert ids and all(i.startswith("plugins.actor-avatar.") for i in ids)
    assert len(ids) == len(set(ids))


def test_manifest_matches_code():
    manifest = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text("utf-8"))
    plugin = manifest["plugin"]
    assert plugin["id"] == "actor-avatar" and plugin["entry"] == "actor_avatar"
    ops = re.search(r"OPERATIONS = \(([^)]*)\)", ENTRY).group(1)
    declared = re.findall(r'"([a-z.:\-]+)"', ops)
    assert sorted(declared) == sorted(manifest["permissions"]["operations"])
    assert re.search(r'PLUGIN_ID = "actor-avatar"', ENTRY)
    assert manifest["permissions"]["network"] is True
    changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert f"## {plugin['version']}" in changelog


def test_panel_calls_only_declared_routes():
    routes = set(re.findall(r'@router\.(?:get|put|post|delete)\(\s*"(/[^"]*)"', ENTRY))
    called = set(re.findall(r'(?:api|act)\(\s*"(/[^"?]*)"', HTML))
    assert {"/libraries", "/write/disarm", "/names/refresh"} <= called
    assert called <= routes, called - routes


def test_library_picker_is_not_a_typed_field():
    # 库选择在插件页面勾选，主程序设置表单里不再出现手填的库名 / 编号
    settings = (ROOT / "actor_avatar" / "settings.py").read_text("utf-8")
    assert "libraries:" not in settings
    assert 'method: "PUT"' in HTML or "'PUT'" in HTML or '"PUT"' in HTML


def test_agent_permission_and_injection_agree():
    manifest = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text("utf-8"))
    ops = manifest["permissions"]["operations"]
    # 只要最低档：无工具；不要 mclaw / full
    assert "agent:none" in ops and not any(o in ops for o in ("agent:mclaw", "agent:full"))
    assert re.search(r"inject=\([^)]*\bAGENT\b", ENTRY, re.S)
    assert 'tools="none"' in (ROOT / "actor_avatar" / "ai.py").read_text("utf-8")


def test_panel_has_v03_controls():
    called = set(re.findall(r'(?:api|act)\(\s*"(/[^"?]*)"', HTML))
    assert {"/unmatched", "/manual", "/manual/alias", "/manual/upload", "/manual/remove"} <= called
    assert {"/ai", "/ai/suggest"} <= called and 'api("/search?q="' in HTML
    assert "AI 推荐别名" in HTML and "innerHTML" not in HTML


def test_v03_settings_defaults():
    from actor_avatar.settings import Settings

    s = Settings()
    assert s.ai_suggest_on_run is False and s.skip_junk_names is True
    assert 0 < s.ai_daily_limit <= 200


def test_v04_tabs_and_paged_lists():
    for tab in ("overview", "pending", "unmatched", "history", "settings"):
        assert f'data-tab="{tab}"' in HTML and f'id="tab-{tab}"' in HTML
    for label in ("概览", "待确认", "没配上的演员", "写入记录", "设置"):
        assert f">{label}<" in HTML
    # 长列表都分页 + 搜索
    for key in ("pending", "actors", "junk", "manual", "ledger", "events"):
        assert f'id="pager-{key}"' in HTML and f'id="{key}-list"' in HTML
    for btn in ("立即预览", "立即写入一轮", "停止自动写入"):
        assert btn in HTML
    # 托管模式的地址提醒不再是顶部大横幅
    assert 'id="thumb-warning"' not in HTML


def test_v04_settings_use_host_generic_settings_api():
    # 插件页面以当前管理员身份调主程序的「插件通用设置」接口（与设置页同一份存储，
    # 主程序负责校验并重启插件）：GET/PUT /api/v1/app/plugins/{id}/settings，body 为 {values}
    assert 'const HOST_SETTINGS = "/api/v1/app/plugins/actor-avatar/settings"' in HTML
    assert 'request(HOST_SETTINGS, { values }, "PUT")' in HTML
    host_paths = set(re.findall(r'"(/api/v1/app/[^"]*)"', HTML))
    assert host_paths == {"/api/v1/app/plugins/actor-avatar/settings"}, host_paths
    # 页面不能自己解锁自动写入，也碰不到权限 / 批准接口
    for forbidden in ("/write/arm", "/approve", "/packages", "/grants", "write_armed:"):
        assert forbidden not in HTML, forbidden


def test_v04_every_setting_has_a_place_in_the_page():
    from actor_avatar.settings import Settings

    groups = re.search(r"const GROUPS = \[(.*?)\n    \];", HTML, re.S).group(1)
    placed = set(re.findall(r'"([a-z_]+)"', groups))
    meta = re.search(r"const META = \{(.*?)\n    \};", HTML, re.S).group(1)
    labelled = set(re.findall(r"^\s+([a-z_]+): \[", meta, re.M))
    fields = set(Settings.model_fields)
    assert fields <= placed, fields - placed
    assert fields <= labelled, fields - labelled
    for name in ("基本", "头像来源", "写入", "AI", "高级"):
        assert f'title: "{name}"' in groups


def test_v04_page_talks_only_to_known_endpoints():
    # 只有一个 fetch：全部请求都经 request()（同源 Cookie + 统一的中文出错说明）
    assert HTML.count("fetch(") == 1
    api_paths = set(re.findall(r'"(/api/v1/[^"?$]*)', HTML))
    assert api_paths == {
        "/api/v1/plugins/actor-avatar",
        "/api/v1/app/plugins/actor-avatar/settings",
    }, api_paths
    assert set(re.findall(r"`(/api/v1/[^`?$]*)", HTML)) == {"/api/v1/images/proxy"}
    # 模板字符串里拼的插件路径也必须是声明过的路由
    routes = set(re.findall(r'@(?:router|public)\.(?:get|put|post|delete)\(\s*"(/[^"]*)"', ENTRY))
    for path in re.findall(r"\$\{BASE\}(/[a-z/-]+)/", HTML):
        assert any(r.startswith(path + "/{") for r in routes), path
    # 设置只提交改动过的字段
    assert "S.view = await hostSettings(changed)" in HTML


def test_v04_errors_shown_in_chinese():
    # 主程序 / 插件 / 浏览器的英文兜底（"request failed"、"Failed to fetch"）不直接显示
    assert "function errorText(" in HTML and "function zh(" in HTML
    assert "HAS_CJK.test(detail)" in HTML
    assert "err.message || err" not in HTML and "e.message || e" not in HTML
    for text in ("连不上服务器", "没有权限（需要管理员）", "登录已过期"):
        assert text in HTML
