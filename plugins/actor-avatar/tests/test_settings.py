from __future__ import annotations

import pytest
from actor_avatar.settings import GITHUB_BASE, JSDELIVR_BASE, Mirror, Settings, ThumbMode
from pydantic import ValidationError


def test_defaults():
    s = Settings()
    assert s.legacy_library_refs() == []
    assert s.dry_run is True
    assert s.name_table is True
    assert s.mirror == Mirror.JSDELIVR
    assert s.ai_fix is True
    assert s.thumb_mode == ThumbMode.MIRROR
    assert s.source_base() == JSDELIVR_BASE


def test_mirrors():
    assert Settings(mirror=Mirror.GITHUB).source_base() == GITHUB_BASE
    custom = Settings(mirror=Mirror.CUSTOM, custom_base_url="https://m.example.com/gfriends")
    assert custom.source_base() == "https://m.example.com/gfriends/"
    # 自定义但没填地址：回落 jsDelivr
    assert Settings(mirror=Mirror.CUSTOM).source_base() == JSDELIVR_BASE


def test_enum_values_are_labels():
    assert Settings.model_validate({"mirror": "GitHub 原站"}).mirror == Mirror.GITHUB


@pytest.mark.parametrize("bad", ["ftp://x", "example.com", "http://"])
def test_rejects_bad_urls(bad):
    with pytest.raises(ValidationError):
        Settings(custom_base_url=bad)
    with pytest.raises(ValidationError):
        Settings(public_base_url=bad)


def test_bounds_and_legacy_library_refs():
    with pytest.raises(ValidationError):
        Settings(max_items_per_run=0)
    # 0.1.x 存在设置里的 libraries（不再是表单字段）照样读得出来，迁移用
    legacy = Settings.model_validate({"libraries": [" 小姐姐 ", "", "9"], "dry_run": False})
    assert legacy.legacy_library_refs() == ["小姐姐", "9"]
    assert Settings.model_validate({"libraries": "9"}).legacy_library_refs() == ["9"]
    assert Settings.model_validate({"libraries": {"x": 1}}).legacy_library_refs() == []


def test_form_schema_has_no_library_field_and_dry_run_defaults_on():
    """主程序设置表单按 Schema 子集画：库选择不在表单里，只预览默认开。"""
    from movieclaw_sdk.config_schema import config_schema

    schema = config_schema(Settings)
    props = schema["properties"]
    assert "libraries" not in props
    assert props["dry_run"] == {**props["dry_run"], "type": "boolean", "default": True}
    assert props["name_table"]["default"] is True
    # 每个字段都带默认值：表单打开就是默认状态，不会出现空白项被当成 false / 空列表存下
    # 敏感字段（writeOnly）例外：主程序不回显也不带默认值
    assert all("default" in p for p in props.values() if not p.get("writeOnly"))
    for key in ("dmm_api_id", "dmm_affiliate_id"):
        assert props[key]["writeOnly"] is True and props[key]["format"] == "password"
        assert "default" not in props[key]


def test_name_table_url_follows_mirror():
    assert "cdn.jsdelivr.net" in Settings().name_table_url()
    assert "raw.githubusercontent.com" in Settings(mirror=Mirror.GITHUB).name_table_url()
    assert "cdn.jsdelivr.net" in Settings(mirror=Mirror.CUSTOM).name_table_url()
