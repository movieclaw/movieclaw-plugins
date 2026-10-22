"""WeCom app notifications. Legacy IDs are retained for seamless upgrades."""

import asyncio
import hashlib
import logging
import re
from enum import Enum
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator
from movieclaw_sdk import plugin, net
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_api.plugins.keys import PLUGIN_DATA
from movieclaw_sdk.ops import HOST_OPS
from wecom_client import WecomClient, valid_link
from enrichment import Enricher, detail_link, OPERATIONS
import templates
from dedupe import Ledger
from uploads import UploadMonitor
from arrivals import FastArrivals
from fastapi import APIRouter
from movieclaw_api.plugins.keys import PLUGIN_ROUTES
from movieclaw_sdk.events import (
    SUBSCRIPTION_CREATED, SUBSCRIPTION_DOWNLOAD_STARTED, SUBSCRIPTION_FULFILLED,
    DOWNLOAD_COMPLETED, LIBRARY_INGEST_COMPLETED, SUBSCRIPTION_UPGRADED, LIBRARY_ITEMS_ARRIVED, LIBRARY_FILES_ADDED,
)
from movieclaw_sdk.callbacks import PLUGIN_CALLBACKS, CallbackResponse
from movieclaw_sdk.channels import IM_CHANNELS, ChannelDriver, Binding, BindResult, Capabilities

PLUGIN_ID = "notifyhub"
EVENTS = (SUBSCRIPTION_CREATED, SUBSCRIPTION_DOWNLOAD_STARTED, SUBSCRIPTION_FULFILLED,
          DOWNLOAD_COMPLETED, LIBRARY_INGEST_COMPLETED, SUBSCRIPTION_UPGRADED, LIBRARY_ITEMS_ARRIVED)
IMPORTS = {"subscription.fulfilled", "download.imported", "library.ingest.completed", "library.items.arrived"}


class MessageType(str, Enum):
    auto = "auto"
    news = "news"
    textcard = "textcard"
    text = "text"


class Settings(BaseModel):
    corp_id: str = Field("", title="企业 ID（CorpID）")
    agent_id: int = Field(0, ge=0, title="应用 ID（AgentID）")
    corp_secret: SecretStr = Field(default_factory=lambda: SecretStr(""), title="应用 Secret")
    to_user: str = Field("", title="接收成员 UserID", description="多个用 | 分隔；@all 表示应用可见范围内全部成员，需明确填写")
    api_base: str = Field("https://qyapi.weixin.qq.com", title="企业微信 API 地址", description="直连保持默认；自建代理才需要修改")
    movieclaw_url: str = Field("", title="MovieClaw 访问地址", description="统一用于详情跳转和海报地址；企业微信需能匿名访问此地址的图片。留空使用原图片来源")
    message_type: MessageType = Field(MessageType.auto, title="消息样式", description="auto：有海报和链接发图文，无海报发文本卡片；没有链接回退纯文本")
    enrich_metadata: bool = Field(True, title="读取媒体详情", description="补充真实评分、类型、演员、简介和海报；失败时隐藏缺失字段")
    overview_length: int = Field(160, ge=0, le=1000, title="简介最多字符数")
    resource_length: int = Field(120, ge=30, le=500, title="资源名称最多字符数")
    subscription_title: str = Field(templates.DEFAULT_TITLE, title="订阅标题模板", json_schema_extra={"multiline": True})
    subscription_text: str = Field(templates.DEFAULT_TEXT, title="订阅正文模板", json_schema_extra={"multiline": True})
    download_title: str = Field(templates.DEFAULT_TITLE, title="下载标题模板", json_schema_extra={"multiline": True})
    download_text: str = Field(templates.DEFAULT_TEXT, title="下载正文模板", json_schema_extra={"multiline": True})
    import_title: str = Field(templates.DEFAULT_TITLE, title="入库标题模板", json_schema_extra={"multiline": True})
    import_text: str = Field(templates.DEFAULT_TEXT, title="入库正文模板", json_schema_extra={"multiline": True})
    playback_title: str = Field(templates.DEFAULT_TITLE, title="播放标题模板", json_schema_extra={"multiline": True})
    playback_text: str = Field(templates.DEFAULT_TEXT, title="播放正文模板", json_schema_extra={"multiline": True})
    alert_title: str = Field(templates.ALERT_TITLE, title="告警标题模板", json_schema_extra={"multiline": True})
    alert_text: str = Field(templates.ALERT_TEXT, title="告警正文模板", json_schema_extra={"multiline": True})
    subscriptions: bool = Field(True, title="订阅通知")
    downloads: bool = Field(True, title="下载开始通知")
    download_completed: bool = Field(False, title="下载完成通知", description="入库前的独立提醒；默认关闭以减少重复")
    uploads: bool = Field(True, title="网盘上传完成通知")
    upload_poll_seconds: int = Field(30, ge=15, le=300, title="上传结果检查间隔（秒）")
    upload_title: str = Field("{{ event_icon }} {{ action }}｜{{ title }}", title="上传标题模板", json_schema_extra={"multiline": True})
    upload_text: str = Field(templates.DEFAULT_UPLOAD_TEXT, title="上传正文模板", json_schema_extra={"multiline": True})
    imports: bool = Field(True, title="入库通知")
    library_merge_seconds: int = Field(5, ge=1, le=15, title="新增媒体合并等待（秒）", description="通常约此间隔后通知，持续入库最多等待三倍间隔；大批量扫描走汇总")
    library_arrivals: bool = Field(True, title="媒体库新增通知", description="媒体库发现新电影或新剧集时通知，与入库通知合并去重")
    upgrades: bool = Field(True, title="洗版完成通知")
    playback: bool = Field(False, title="播放通知", description="需配置内置 Webhook 到本插件回调；不发送进度")
    alerts: bool = Field(True, title="告警通道转发", description="需额外绑定企业微信直推通道；通过通知通道转发系统告警")
    staging: bool = Field(False, title="暂存整理通知", description="暂存完成不代表媒体已入库")
    dedupe_minutes: int = Field(35, ge=0, le=1440, title="入库合并窗口（分钟）", description="同一作品同一季集跨事件只提醒一次；0 关闭合并")
    playback_cooldown: int = Field(60, ge=0, le=3600, title="同类播放提醒冷却（秒）")
    timeout: int = Field(10, ge=1, le=30, title="请求超时（秒）")
    server_name: str = Field("MovieClaw", min_length=1, max_length=60, title="通知显示的服务器名称")
    timezone: str = Field("Asia/Shanghai", title="通知时间的时区")

    @field_validator("corp_id", "to_user")
    @classmethod
    def trim_identifiers(cls, value):
        return "|".join(v.strip() for v in value.split("|") if v.strip())

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("请输入有效时区，例如 Asia/Shanghai") from None
        return value

    @field_validator("api_base", "movieclaw_url")
    @classmethod
    def valid_url(cls, value):
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("请输入不带账号密码、查询参数和片段的 HTTP/HTTPS 地址")
        return value.rstrip("/")

    @field_validator("subscription_title", "subscription_text", "download_title", "download_text", "import_title", "import_text", "playback_title", "playback_text", "alert_title", "alert_text", "upload_title", "upload_text")
    @classmethod
    def valid_template(cls, value):
        if len(value) > 20000:
            raise ValueError("模板长度不能超过 20000 字符")
        try:
            templates.ENV.parse(value)
        except Exception:
            raise ValueError("Jinja2 模板语法错误，请检查括号和 if/endif") from None
        return value

    def configured(self):
        return bool(self.corp_id.strip() and self.agent_id > 0 and self.corp_secret.get_secret_value() and self.to_user.strip())


def enabled(name, cfg):
    if name == "drive.upload.completed":
        return cfg.uploads
    if name == "library.items.arrived":
        return cfg.library_arrivals
    if name in IMPORTS:
        return cfg.imports
    if name == "subscription.upgraded":
        return cfg.upgrades
    if name == "subscription.download_started":
        return cfg.downloads
    if name == "download.completed":
        return cfg.download_completed
    if name.startswith("playback."):
        return cfg.playback
    if name.startswith("system.") or name == "subscription.download_stuck":
        return cfg.alerts
    return cfg.subscriptions


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def units_of(item):
    # Movies may carry sentinel units in dict, tuple or file snapshots.
    if str((item.get("media") or {}).get("kind") or "").lower() == "movie":
        return []
    rows = item.get("units") if "units" in item else [item["unit"]] if item.get("unit") else item.get("files", [])
    result = set()
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            season, episode = row
        elif isinstance(row, dict):
            season, episode = row.get("season"), row.get("episode")
        else:
            continue
        # Season 0 with a real episode is a TV special; episode 0 is a sentinel.
        if type(season) is int and type(episode) is int and season >= 0 and episode > 0:
            result.add((season, episode))
    return [{"season": s, "episode": e} for s, e in sorted(result)]


def episode_display(units):
    groups = {}
    for u in units: groups.setdefault(u['season'], set()).add(u['episode'])
    labels = []
    for season, episodes in sorted(groups.items()):
        numbers = sorted(episodes); spans = []; first = last = numbers[0]
        for n in numbers[1:]:
            if n == last + 1: last = n
            else:
                spans.append((first, last)); first = last = n
        spans.append((first, last))
        labels.extend(f"S{season:02d}E{first:02d}" + (f"–E{last:02d}" if first != last else "") for first, last in spans)
    return "、".join(labels)


def unit_tokens(units):
    return sorted({f"S{u['season']:02d}E{u['episode']:02d}" for u in units})


def timestamp(milliseconds):
    seconds = max(0, int(milliseconds) // 1000)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def render(name, data, cfg):
    if name not in templates.EVENTS or not enabled(name, cfg):
        return []
    if name == "playback.stopped" and data.get("completed"):
        return []
    if name == "library.ingest.completed" and data.get("staging") and not cfg.staging:
        return []
    notices = []
    for item in data.get("items", [data]):
        media, meta = item.get("media") or {}, data.get("_meta") or {}
        kind = str(media.get("kind") or "").lower()
        units = units_of(item)
        seasons = [] if kind == "movie" else sorted({u["season"] for u in units} or {v for v in meta.get("seasons", []) or [] if type(v) is int and v > 0})
        season_fmt = " / ".join("特别篇" if season == 0 else f"第 {season} 季" for season in seasons)
        title = media.get("title") or data.get("title") or cfg.server_name
        title_year = title + (f" ({media['year']})" if media.get("year") else "")
        icon, action, _ = templates.EVENTS[name]
        if name == "drive.upload.completed" and data.get("failed"):
            icon, action = "⚠️", "网盘上传部分完成"
        if data.get("staging"):
            action = "暂存整理完成"
        size = ""
        sizes = [f.get("size_bytes") for f in data.get("files", [])]
        if not data.get("_hide_size") and sizes and all(type(v) is int and v >= 0 for v in sizes):
            total = float(sum(sizes))
            for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
                if total < 1024 or suffix == "TiB": break
                total /= 1024
            size = f"{total:.2f} {suffix}"
        created_at = ""
        if data.get("_occurred_at"):
            try:
                when = datetime.fromisoformat(str(data["_occurred_at"]).replace("Z", "+00:00"))
                if when.tzinfo: created_at = when.astimezone(ZoneInfo(cfg.timezone)).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError: pass
        link = detail_link(name, {**data, "media": media}, cfg)
        client = data.get("client") or {}
        device = " · ".join(dict.fromkeys(v for v in (client.get("device_name"), client.get("name")) if v))
        progress = ""
        if data.get("position_ms") is not None:
            progress = timestamp(data["position_ms"])
            if data.get("duration_ms") and data["duration_ms"] > 0:
                progress += " / " + timestamp(data["duration_ms"])
        overview = str(meta.get("overview") or "").strip()
        if cfg.overview_length == 0: overview = ""
        elif len(overview) > cfg.overview_length: overview = overview[:cfg.overview_length - 1] + "…"
        torrent = data.get("torrent") or {}
        resource = " ".join(str(torrent.get("title") or data.get("torrent_title") or data.get("download_name") or "").split())
        if len(resource) > cfg.resource_length:
            resource = resource[:cfg.resource_length - 1] + "…"
        quality = data.get("new_label") or torrent.get("spec") or torrent.get("resolution") or " / ".join(dict.fromkeys(re.findall(r"(?i)\b(?:2160p|1080p|720p|HDR10\+?|HDR|DV|HEVC|H\.?265|H\.?264|WEB-DL|BluRay)\b", resource)))
        context = {"event": name, "event_icon": icon, "action": action, "title": title, "title_year": title_year,
                   "media_kind": kind, "is_movie": kind == "movie", "is_tv": kind in ("tv", "episode", "series"),
                   "media_label": "电影" if kind == "movie" else "电视剧" if kind in ("tv", "episode", "series") else "",
                   "release_year": media.get("year"), "season": seasons[0] if len(seasons) == 1 else seasons or None,
                   "season_fmt": season_fmt, "episode_fmt": episode_display(units),
                   "vote_average": meta.get("vote_average"), "category": " / ".join(meta.get("genres") or []),
                   "genres": " / ".join(meta.get("genres") or []), "username": meta.get("username") or "",
                   "actors": " / ".join(meta.get("actors") or []), "overview": overview, "intro": overview,
                   "server_name": cfg.server_name, "created_at": created_at, "size": size, "device": device, "progress": progress,
                   "quality": quality, "message": data.get("message") or "",
                   "resource": resource, "filenames": data.get("filenames") or [], "filenames_remaining": data.get("filenames_remaining", 0), "filenames_missing": data.get("filenames_missing", 0), "uploaded": data.get("uploaded"), "rapid": data.get("rapid"), "failed": data.get("failed"),
                   "__mediainfo__": {"detail_link": link, "poster_path": meta.get("poster") or "", "backdrop_path": meta.get("backdrop") or ""}}
        rendered_title, text = templates.render(name, context, cfg)
        body = {"title": rendered_title, "content": text}
        if link and name != "drive.upload.completed": body["push_link_url"] = link
        image = jellyfin_image(media, units, cfg) or meta.get("backdrop") or meta.get("poster")
        if image: body["push_img_url"] = image
        notices.append((body, media, units))
    return notices


def jellyfin_image(media, units, cfg):
    """v0.40.0 structured Jellyfin GUID; only real library item IDs are used."""
    item_id = media.get("id")
    if not cfg.movieclaw_url or type(item_id) is not int or not 0 < item_id < 2**64:
        return ""
    kind, season, episode = 2, 0, 0
    if media.get("kind") == "tv" and len(units) == 1:
        season, episode = units[0].get("season"), units[0].get("episode")
        if type(season) is int and type(episode) is int and 0 <= season < 65536 and 0 < episode < 65536:
            kind = 4
        else:
            season, episode = 0, 0
    guid = (b"MC" + bytes([kind, 0]) + item_id.to_bytes(8, "big") + season.to_bytes(2, "big") + episode.to_bytes(2, "big")).hex()
    return valid_link(cfg.movieclaw_url.rstrip("/") + f"/Items/{guid}/Images/Primary?maxWidth=1068")


class Sender:
    def __init__(self, ctx, store, cfg):
        self.ctx, self.store, self.cfg = ctx, store, cfg
        self.lock = asyncio.Lock()
        self.ledger = Ledger(store)
        self.wecom = WecomClient(cfg, ctx.logger)
        self.enricher = Enricher(ctx, HOST_OPS)

    async def handle(self, name, payload, external_id=None):
        if not self.cfg.configured():
            return
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        if name == "library.items.arrived" and "items" in data:
            parent_id = external_id or (self.ctx.delivery.event_id if self.ctx.delivery else digest([name, data]))
            for index, item in enumerate(data["items"]):
                await self.handle(name, {**{k: v for k, v in data.items() if k != "items"}, **item},
                                  external_id=digest([parent_id, index]))
            return
        if self.ctx.delivery is not None:
            data = {**data, "_occurred_at": self.ctx.delivery.occurred_at}
        if not enabled(name, self.cfg):
            return
        data = {**data, "_meta": await self.enricher.get(name, data)}
        notices = render(name, data, self.cfg)
        if not notices:
            return
        async with self.lock:
            now = time.time()
            await self.ledger.migrate()
            delivery = self.ctx.delivery
            event_id = external_id or (delivery.event_id if delivery else digest([name, data]))
            for index, (body, media, units) in enumerate(notices):
                exact = "event:" + digest([event_id, index])
                if await self.ledger.contains(exact):
                    continue
                semantic = []
                ttl = self.cfg.dedupe_minutes * 60
                if name in IMPORTS and media.get("id") is not None and not data.get("staging") and ttl:
                    # Per-unit keys handle batched and partially overlapping episodes.
                    semantic = ["import:" + digest([media['id'], media.get('kind'), u]) for u in (unit_tokens(units) or ["movie"])]
                    fresh = [k for k in semantic if not await self.ledger.contains(k)]
                    if not fresh:
                        continue
                    if units and len(fresh) < len(semantic):
                        keep = [u for u, k in zip(unit_tokens(units), semantic) if k in fresh]
                        fresh_units = [u for u in units if f"S{u['season']:02d}E{u['episode']:02d}" in keep]
                        body = render(name, {**data, "units": fresh_units, "_hide_size": True}, self.cfg)[0][0]
                    semantic = fresh
                elif name.startswith("playback.") and self.cfg.playback_cooldown:
                    ttl = self.cfg.playback_cooldown
                    semantic = ["play:" + digest([name, media.get("id"), units, data.get("member_id"), data.get("file_id"), data.get("client")])]
                    if await self.ledger.contains(semantic[0]):
                        continue
                try:
                    await self.wecom.send(body)
                except Exception as exc:
                    self.ctx.logger.warning("企业微信推送失败（%s，%s），交由宿主重试", name, type(exc).__name__)
                    raise
                await self.ledger.remember({exact: now + 7 * 86400,
                                            **{key: now + ttl for key in semantic}})
                self.ctx.logger.info("企业微信已接受推送：%s", name)


class WecomChannel(ChannelDriver):
    title = "企业微信直推"
    description = "转发 MovieClaw 通道推送；建议只开启系统告警以免与原生事件重复"
    capabilities = Capabilities(receive=False, max_text_len=6000)
    binding = Binding.form((), hint="使用插件设置中的企业微信自建应用凭据；绑定不发送测试消息")

    def __init__(self, sender):
        self.sender = sender

    async def validate(self, fields):
        return BindResult(account_id="notifyhub", display_name="企业微信直推", credentials={}, bound_user="notifyhub")

    async def send(self, account, reply, text):
        if not self.sender.cfg.alerts:
            return
        eid = "channel:" + digest([text, int(time.time() // 60)])
        await self.sender.handle("system.alert.raised", {"title": "MovieClaw 通道通知", "message": text, "source": "通知通道"}, external_id=eid)


@plugin(PLUGIN_ID, title="企业微信直推", config=Settings,
        inject=(PLUGIN_DATA, DURABLE_EVENTS, PLUGIN_CALLBACKS, PLUGIN_ROUTES, HOST_OPS), permissions=OPERATIONS)
async def apply(ctx):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    sender = Sender(ctx, store, ctx.config)
    if ctx.config.uploads:
        ctx.task(UploadMonitor(sender).run(), name="upload-notifications")
    if ctx.config.library_arrivals:
        fast_arrivals = FastArrivals(sender)
        ctx.on(LIBRARY_FILES_ADDED, fast_arrivals.handle, id="notifyhub-library-files-added", durable=True)
    for event in EVENTS:
        name = event.name
        if not enabled(name, ctx.config):
            continue

        async def notify(payload, name=event.name):
            await sender.handle(name, payload)

        ctx.on(event, notify, id="notifyhub-" + event.name.replace(".", "-"), durable=True)

    async def playback_callback(request):
        if request.method != "POST":
            return CallbackResponse.json({"error": "method"}, status=405)
        try:
            envelope = request.json()
            if not isinstance(envelope, dict):
                raise ValueError("事件必须为对象")
            name = envelope.get("type")
            if name not in {"playback.started", "playback.stopped", "playback.completed"}:
                return CallbackResponse.json({"ignored": True})
            if not enabled(name, ctx.config):
                return CallbackResponse.json({"ignored": True})
            raw = envelope.get("data", {})
            if not isinstance(raw, dict):
                raise ValueError("data 必须为对象")
            from movieclaw_sdk.events.playback import Playback, PlaybackStopped
            model = PlaybackStopped if name == "playback.stopped" else Playback
            data = model.model_validate(raw).model_dump(mode="json")
            data["_occurred_at"] = envelope.get("occurred_at")
            eid = envelope.get("id")
            if not isinstance(eid, str) or not eid:
                raise ValueError("id 必须为非空字符串")
        except (TypeError, ValueError):
            return CallbackResponse.json({"error": "invalid event"}, status=400)
        try:
            await sender.handle(name, data, external_id=eid)
        except Exception:
            return CallbackResponse.json({"error": "WeCom delivery failed"}, status=503)
        return CallbackResponse.json({"success": True})

    callbacks = ctx.use(PLUGIN_CALLBACKS)
    callbacks.endpoint(ctx, "playback", playback_callback, methods=("POST",), timeout=45)
    router = APIRouter()

    @router.get("/integration", operation_id="plugins.notifyhub.integration", summary="企业微信：查看播放 Webhook 接入地址")
    async def integration():
        async with sender.lock:
            issued = await store.get("playback-callback-v1")
            if not issued:
                key = await callbacks.issue(ctx, "playback")
                issued = {"url": key.url, "key_id": key.id}
                await store.set("playback-callback-v1", issued, secret=True)
        return {"webhook_url": issued["url"], "format": "movieclaw", "events": ["playback.started", "playback.completed"],
                "note": "相对地址需补上 MovieClaw 服务地址；此密钥地址仅用于你的 MovieClaw 内置 Webhook"}

    @router.get("/preview", operation_id="plugins.notifyhub.preview", summary="企业微信：模板效果预览（不发送）")
    async def preview():
        sample = {"media": {"id": 1, "kind": "tv", "title": "示例剧集", "year": 2026, "tmdb_id": 1},
                  "units": [[1, 1]], "_meta": {"vote_average": 8.5, "genres": ["剧情", "冒险"], "actors": ["示例演员"],
                  "overview": "这是模板演示数据，不是真实影视资料。", "username": "示例用户"}}
        return {"notice": render("subscription.created", sample, ctx.config)[0][0], "configured": ctx.config.configured()}

    @router.post("/test", operation_id="plugins.notifyhub.test", summary="企业微信：向配置的接收成员发送测试消息")
    async def test_send():
        if not ctx.config.configured():
            return {"success": False, "error": "请先填写企业 ID、应用 ID、Secret 和接收成员"}
        notice = {"title": "✅ MovieClaw 企业微信直推测试", "content": "自建应用直推已连接。\n此消息来自插件设置中指定的接收成员配置。", "push_link_url": ctx.config.movieclaw_url}
        try:
            await sender.wecom.send(notice)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True}

    ctx.use(PLUGIN_ROUTES).mount(ctx, router)
    if ctx.config.alerts:
        ctx.contribute(IM_CHANNELS, "notifyhub", WecomChannel(sender))
    if not ctx.config.configured():
        ctx.logger.info("企业微信直推已加载；请在插件设置填写企业 ID、应用 ID、Secret 和接收成员。未配置期间不发送、不补发。")
    else:
        ctx.logger.info("企业微信直推已加载")
