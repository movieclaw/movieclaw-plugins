"""将转存反馈交给宿主统一通知；不保存或访问任何消息通道凭据。"""
import re

from pydantic import BaseModel, ConfigDict, Field
from movieclaw_sdk.events import plugin_event


class Notice(BaseModel):
    model_config = ConfigDict(frozen=True)
    message: str = Field(max_length=1200, description="已去除分享链接和提取码的转存进度")


NOTICE = plugin_event("p115-import.notice", Notice, title="115 转存通知")
_URL = re.compile(r"(?:https?://|magnet:\?|ed2k://)[^\s<>]+", re.IGNORECASE)
_PASSWORD = re.compile(r"((?:提取码|访问码|密码)\s*[:：=]?\s*)[A-Za-z0-9_-]{2,64}", re.IGNORECASE)


def safe_notice(text: str) -> str:
    """通知仅保留进度，不传播原始分享链接、磁力或提取码。"""
    cleaned = str(text or "").replace("【MovieClaw】", "").strip()
    cleaned = _URL.sub("[链接已隐藏]", cleaned)
    cleaned = _PASSWORD.sub(r"\1[已隐藏]", cleaned)
    return cleaned[:1200]


def tmdb_link(kind, tmdb_id) -> str:
    """只由准确的影视类型和正整数生成公共作品链接，绝不接受任意用户 URL。"""
    if kind not in ("movie", "tv") or isinstance(tmdb_id, bool):
        return ""
    raw = str(tmdb_id or "")
    if not re.fullmatch(r"[1-9][0-9]{0,11}", raw):
        return ""
    return f"https://www.themoviedb.org/{kind}/{raw}"


def notice_text(text: str, *, plan=None, result=None) -> str:
    content = safe_notice(text)
    if not content:
        return ""
    plan, result = plan or {}, result or {}
    link = tmdb_link(plan.get("kind"), plan.get("tmdb_id")) if result.get("inventory_verified") is True else ""
    # TMDB 链接由已核验的作品身份生成，在其它通知渠道可见，非原始分享链接。
    prefix = f"影视资料：{link}\n" if link else ""
    return prefix + content[:1200 - len(prefix)]


async def publish(publisher, logger, text: str, *, plan=None, result=None) -> bool:
    """发布后由宿主按通知偏好投递；推送失败不阻断转存任务。"""
    content = notice_text(text, plan=plan, result=result)
    if not content:
        return False
    try:
        await publisher.publish(NOTICE, Notice(message=content))
        return True
    except Exception as exc:  # noqa: BLE001 -- 通知异常不得影响转存
        logger.warning("115 转存通知未能交给系统（%s）；任务结果仍可在工作台查看", exc.__class__.__name__)
        return False
