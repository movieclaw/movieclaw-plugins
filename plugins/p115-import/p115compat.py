"""v0.44 的公开接口适配与纯判定：网盘范围、命名语言和轮询节流。"""
import posixpath

from movieclaw_sdk.jobs import JobBlocked


def cloud_scan_body(plan):
    """只扫已确认作品目录；非法/残缺检查点绝不退成整库扫描。"""
    root = str(plan.get("cloud_root_path") or "").replace("\\", "/").rstrip("/")
    entry = str(plan.get("entry_dir_name") or "")
    if not root or not root.strip("/") or not entry.strip() or entry in (".", "..") or "/" in entry or "\\" in entry:
        raise JobBlocked("作品的网盘目录信息不完整，未发起整库扫描；请核对目标目录")
    if any(p in (".", "..") for p in root.split("/")) or entry.startswith("."):
        raise JobBlocked("网盘扫描范围越界或指向暂存目录，未执行扫描")
    return {"cloud_paths": [posixpath.join(root, entry)]}


def poll_interval(unchanged, *, cloud=False):
    """短查询、逐步退让；变化时调用方将计数清零，不占长轮询。"""
    schedule = (15, 30, 45, 60) if cloud else (1, 2, 4, 8, 10)
    return schedule[min(max(int(unchanged), 0), len(schedule) - 1)]


def library_languages(scrape, overrides=None):
    # 库级空列表表示跟随全局，不是抹掉全局设置。
    return list((overrides or {}).get("language_priority")
                or (scrape.get("setting") or {}).get("language_priority")
                or (scrape.get("effective") or {}).get("language_priority") or ["zh-CN", "en-US"])


def naming_title(details, languages, reader_locale="zh-Hans", *, needs_title=True):
    """只消费公开详情里的中/英/原名，不把读者译名当作库语言原文。

    公开详情目前只提供界面语言、中英与原名；其它库语言不能从该接口精确取得，
    使用 {title} 时明确停止，避免假装支持。原名模板不需要翻译，照常使用。
    """
    title = details.get("title") or {}
    metadata = details.get("metadata") or {}
    display = title.get("title") or ""
    original = title.get("original_title") or ""
    primary = str((languages or ["zh-CN"])[0]).lower()
    if not (primary.startswith("zh-cn") or primary.startswith("en")):
        if needs_title:
            raise JobBlocked("当前媒体库语言的命名译名尚未由公开接口提供；请用原名模板，或先确认中英文媒体库语言。未转存、未改名")
        return original or display
    chinese = metadata.get("chinese_title") or (display if str(reader_locale).startswith("zh") else "")
    english = metadata.get("english_title") or (display if str(reader_locale).startswith("en") else "")
    for language in languages:
        tag = str(language).lower()
        value = chinese if tag.startswith("zh-cn") else english if tag.startswith("en") else ""
        if value:
            return value
    # TMDB 无翻译时原名是唯一语言无关的可靠回落，不用来路不明的显示译名。
    if original:
        return original
    raise JobBlocked("无法取得媒体库命名语言的片名或原名，未生成可能错误的文件名")
