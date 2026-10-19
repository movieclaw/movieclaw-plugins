"""把旧版文本平台列表迁移成独立开关；不处理或重写任何凭据。"""
import re

PLATFORM_IDS = ("iqiyi", "tencent", "youku", "mango", "bilibili", "netflix", "hbo", "disney")
DOMESTIC_IDS = set(PLATFORM_IDS[:5])


def migrate_platform_settings(value):
    if not isinstance(value, dict):
        return value
    settings = dict(value)
    keys = ["enable_" + platform for platform in PLATFORM_IDS]
    if not all(key in settings for key in keys):
        legacy = settings.get("enabled_platforms")
        if legacy is None:
            selected = set(PLATFORM_IDS)
        else:
            entries = [legacy] if isinstance(legacy, str) else legacy
            if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
                raise ValueError("旧版平台设置格式不正确，请用平台开关选择")
            selected = {part.lower() for entry in entries for part in re.split(r"[,，、\s]+", entry.strip()) if part}
            if selected - set(PLATFORM_IDS):
                raise ValueError("旧版设置包含未知平台，请用平台开关重新选择")
        domestic = settings.get("enable_domestic", True)
        if domestic is None:
            domestic = True
        elif isinstance(domestic, str):
            normalized = domestic.strip().lower()
            if normalized in {"false", "0", "no", "n", "off", "f"}:
                domestic = False
            elif normalized in {"true", "1", "yes", "y", "on", "t"}:
                domestic = True
            else:
                raise ValueError("旧版国内平台开关格式不正确，请用平台开关选择")
        elif domestic not in (True, False, 0, 1):
            raise ValueError("旧版国内平台开关格式不正确，请用平台开关选择")
        for platform, key in zip(PLATFORM_IDS, keys):
            # 显式关闭也是用户选择；不能被遗留总开关或旧列表覆盖。
            settings.setdefault(key, platform in selected and (domestic or platform not in DOMESTIC_IDS))
    settings.pop("enabled_platforms", None)
    settings.pop("enable_domestic", None)
    return settings
