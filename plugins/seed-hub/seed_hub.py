"""保种管家：自动转种（本地搬运）+ IYUU 自动辅种（跨站补种），一个插件全管。

两种补种方式，共享同一套「判重 → 路径换算 → 加种 → 校验编排 → 删源 → 报告」引擎：

    转种（本地搬运）                          辅种（IYUU 跨站补种）
    ─────────────────                        ─────────────────────
    dl.tasks 取全部下载器的已完成种           dl.tasks 取已完成种（成功缓存跳过已辅）
    源侧取 .torrent：                          IYUU 匹配 → {站点, 种子 id}
      ① 磁盘种子文件目录（<hash>.torrent，     dl.submit 带站点登录态取种（插件碰不到 Cookie）
        qB BT_backup / TR torrents，任意       沿用原种保存路径（IYUU v2.18 口径）
        版本都行，TR 也能做源）
      ② qB ≥ 4.5 WebUI export 兜底
    加种目标：qB / TR（同一套直连底座）
      · 不来自站点的种：直连加种（暂停 / 跳过校验 / 标签 / 指向已有数据）
      · 恰好能从已配置站点取回的种：走 dl.submit 让宿主代劳（免密码）
    不跳校验的：watcher 每分钟看校验进度，完成即恢复做种
    删源（默认关；只删任务不删文件，走内核删种通道过保护）

全自动：两个定时任务（转种每 6 小时、辅种每 24 小时）在「设置 → 定时任务」
可调可暂停；配置不齐自动跳过并记日志，绝不半配置硬跑。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import re
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from movieclaw_api.plugins.keys import (
    HOST_OPS,
    PLUGIN_DATA,
    PLUGIN_FILES,
    PLUGIN_HEALTH,
    PLUGIN_ROUTES,
)
from movieclaw_api.services.host_ops import OpsError
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.events import (
    DOWNLOAD_COMPLETED,
    LIBRARY_FILE_DELETED,
    LIBRARY_FILE_PURGED,
    LIBRARY_INGEST_COMPLETED,
    PLUGIN_EVENTS,
    plugin_event,
)

from bencode import bdecode, bencode
from webui import (
    QB_EXPORT_MIN_VERSION,
    QbittorrentWebUI,
    TransferError,
    TransmissionRPC,
    qb_tracker_groups,
    version_tuple,
)

PLUGIN_ID = "seed-hub"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: IYUU 接口基址与版本（与其 iyuu_helper.py 保持一致）
_IYUU_API_BASE = "https://2025.iyuu.cn"
_IYUU_VERSION = "8.2.0"
#: 单次匹配请求携带的 info_hash 上限（与其实现同款分批）
_MATCH_CHUNK = 200
#: 正式提交时两条种子之间的间隔（秒），对站点温和一点
_SUBMIT_INTERVAL_SECONDS = 1.0
#: IYUU 两次接口调用之间的最小间隔（秒）：连着发会被判「访问频率过快」
_CALL_GAP_SECONDS = 2.0
#: 命中限流 / 网络抖动后的退避基数（秒），第 n 次重试等 n 倍
_RETRY_BACKOFF_SECONDS = 20.0
#: 单次接口调用的重试次数
_RETRY_ATTEMPTS = 3
#: 校验完成自动做种的 watcher 轮询间隔（秒）
_WATCH_INTERVAL_SECONDS = 60.0
#: 报告 / 明细最多携带的条数
_REPORT_DETAIL_LIMIT = 20
#: qB 任务 state：校验完、暂停做种（4.4+ 叫 stoppedUP，老版叫 pausedUP）
_QB_SEEDING_PAUSED_STATES = ("pausedUP", "stoppedUP")
#: TR torrent-get 的 error 字段：3 = 本地错误（含「数据不在了」），2 = tracker 报错，1 = tracker 警告
_TR_ERROR_LOCAL, _TR_ERROR_TRACKER, _TR_ERROR_WARNING = 3, 2, 1
#: tracker 报「种子已被站点删除 / 封禁」的判据（不区分大小写，子串匹配）：
#: 命中即该种在站点侧已无意义，继续做种只会白占任务位——参考 MP 清理QB无效做种插件的默认清单
_TRACKER_DEAD_MESSAGES = (
    "not registered with this tracker",   # 站点已删种（各站通用完整说法）
    "not registered",                      # 同上的短形式（部分 tracker 只返回这个）
    "torrent banned",                      # 站点封禁了这颗种
    "infohash not found",                  # 同上，另一写法
    "未注册",                               # 个别中文站
)
#: TR「数据不在了」的报错关键片段（error=3 时）：命中才允许按「无效任务」清
_TR_NO_DATA_SNIPPET = "no data found"
#: qB tracker status：0=伪条目/私有提示，1=未连接，2=工作中，3=工作中，4=报错
_QB_TRACKER_WORKING = (2, 3)
_QB_TRACKER_ERROR = 4

#: bool 设置被网页折成 null / 空串时的回落值
_BOOL_DEFAULTS = {
    "transfer_enabled": True,
    "skip_verify": False,
    "autostart": True,
    "delete_source": False,
    "delete_duplicate": False,
    "delete_source_managed": False,
    "verify_after_transfer": True,
    "verify_fail_cleanup": True,
    "sweep_enabled": True,
    "sweep_dead": True,
    "sweep_no_data": True,
    "sweep_managed_dead": True,
    "sweep_dry_run": True,
    "link_delete_enabled": True,
    "link_delete_files": False,
}
#: 主种识别：MovieClaw 自己投递的任务标签（订阅 / 手动 / 刷流）——正在给媒体库供数据的种
_MC_OWNED_LABELS = ("movieclaw-sub", "movieclaw-manual", "movieclaw-boost")


# ---------------------------------------------------------------------------
# 插件自己的事件（别的插件 / Webhook 能订；清单 [[events]] 里声明）
# ---------------------------------------------------------------------------


class TransferEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    planned: int = 0
    transferred: int = 0
    failed: int = 0
    deleted_source: int = 0
    dry_run: bool = False


class ReseedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    planned: int = 0
    ok: int = 0
    exists: int = 0
    failed: int = 0
    dry_run: bool = False


class SweepEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    deleted: int = 0
    warned: int = 0
    dry_run: bool = False


TRANSFER_COMPLETED = plugin_event(
    "seed-hub.transfer.completed", TransferEvent, title="转种完成",
    doc="一轮转种跑完：计划 / 成功 / 失败 / 删源的数量。",
)
RESEED_COMPLETED = plugin_event(
    "seed-hub.reseed.completed", ReseedEvent, title="辅种完成",
    doc="一轮 IYUU 辅种跑完：计划 / 成功 / 已存在 / 失败的数量。",
)
SWEEP_COMPLETED = plugin_event(
    "seed-hub.sweep.completed", SweepEvent, title="保活守护完成",
    doc="一轮保活守护跑完：清掉 / 告警的失效任务数量（只删任务，不删文件）。",
)

# ---------------------------------------------------------------------------
# IYUU 站点目录快照（sid, slug, 昵称）——2026-10-11 实测 /reseed/sites/index（免令牌）。
# 烤进来让报告能显示站点昵称；权威目录以 GET /sites（运行时拉取）为准。
# ---------------------------------------------------------------------------

_SITE_NICKNAMES: dict[str, str] = {
    "1": "朋友", "2": "铂金家", "3": "馒头", "4": "天空", "5": "北洋", "6": "猫站",
    "7": "家园", "8": "学校", "9": "我堡", "11": "他吹吹风", "14": "听听歌", "15": "南洋",
    "17": "城市", "18": "老师", "19": "52pt", "22": "吐鲁番", "23": "春天", "24": "聆音",
    "25": "彩虹岛", "27": "烧包", "29": "好大", "30": "时光", "31": "1ptba", "32": "兽",
    "33": "皇后", "36": "开心", "37": "幼儿园", "38": "好多油", "39": "奥申", "40": "蝶粉",
    "45": "北邮人", "51": "海豚", "52": "天雪", "53": "葡萄", "54": "路", "56": "海胆",
    "57": "红豆饭", "58": "龙之家", "59": "百川", "64": "海豹", "65": "普斯特", "67": "蝴蝶",
    "68": "观众", "69": "猪猪", "70": "冬樱", "72": "憨憨", "74": "高清视频", "75": "自由农场",
    "79": "铂金学院", "80": "朱雀", "81": "织梦", "82": "肉丝", "83": "莫妮卡", "84": "大青虫",
    "86": "优堡", "88": "熊猫", "89": "车站", "91": "影", "93": "末日", "94": "象岛",
    "95": "青蛙", "96": "杏坛", "97": "麒麟", "98": "爱萝莉", "99": "GTK", "101": "okpt",
    "102": "蟹黄堡", "105": "杜比", "106": "龟站", "107": "咖啡", "109": "YemaPT",
    "110": "劳改所", "111": "柠檬", "112": "雨", "113": "浦园", "114": "PTzone",
    "115": "hdclone", "116": "库非", "117": "AFun", "119": "星陨阁", "120": "财神",
    "121": "唐门", "123": "海棠", "124": "下水道", "125": "RailgunPT", "126": "GGPT",
    "127": "我的PT(CC)", "128": "LongPT", "129": "hdbao", "130": "13city", "131": "March",
    "132": "LuckPT", "133": "PTSKIT", "134": "PlayletPT", "135": "NovaHD", "136": "垃圾堆",
    "137": "好学", "138": "天枢", "139": "躺平PT", "140": "慕雪阁", "141": "自然", "142": "思齐",
    "143": "包子PT", "144": "藏宝阁",
}


# ---------------------------------------------------------------------------
# 设置：插件详情页自动生成的表单
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    # ---- 转种 ----
    transfer_enabled: bool = Field(True, title="启用自动转种", description="关掉后只保留辅种；定时任务也随之跳过")
    from_downloader: str = Field("", title="源下载器（名称或数字 id）", description="从哪台搬：填「设置 → 下载器」里的名称或数字 id。qB / TR 任意版本都能当源（读磁盘种子文件）")
    from_password: SecretStr | None = Field(None, title="源下载器 WebUI 密码", description="保险库里的凭据插件读不到，这里重填一次；加密存储、界面不回显。qB ≥ 4.5 时磁盘文件缺失会用 WebUI 导出兜底，TR 源可不填")
    to_downloader: str = Field("", title="目标下载器（名称或数字 id）", description="搬到哪台：填法同上；支持 qBittorrent / Transmission ≥ 3.0，须与源不是同一台")
    to_password: SecretStr | None = Field(None, title="目标下载器 WebUI 密码", description="同上，必填：加种、跳过校验、校验完恢复做种都要直连")
    source_torrent_dirs: str = Field("", title="源种子文件目录（每行一个）", description="容器内的挂载路径，每行一个：qB 填 …/BT_backup，TR 填 …/torrents。插件按 <info_hash>.torrent 在这些目录里找种子文件，任意版本的 qB / TR 都能当源", json_schema_extra={"multiline": True})
    # ---- 转种行为 ----
    skip_verify: bool = Field(False, title="跳过校验（仅目标 qB）", description="信任目标下载器里的已有数据、直接做种——快，但数据不完整时坏档不会被发现。目标为 Transmission 时无效（TR 添加后自动校验）")
    autostart: bool = Field(True, title="校验完成后自动开始做种", description="后台每分钟看一次校验进度，完成即开始做种；跳过校验时添加后直接开始")
    delete_source: bool = Field(False, title="转移成功后删除源任务", description="只删源下载器里的任务条目，绝不删数据文件。删源走内核通道（过 H&R 保护、订阅状态正确联动）。开启前请确认目标侧做种正常")
    delete_source_managed: bool = Field(False, title="删源时也删 MovieClaw 投递的种（不推荐）", description="movieclaw-sub / movieclaw-manual 标签的种是订阅或手动下载的任务，删掉后订阅会认为缺资源、可能重新下载。默认跳过它们")
    delete_duplicate: bool = Field(False, title="目标已有同种时删除源任务", description="同一 info_hash 已在目标下载器里时，把源任务清掉（不删文件），避免两边重复做种")
    add_tags: str = Field("转移做种,MovieClaw", title="转种标签（可留空）", description="逗号分隔；目标 qB 写 tags，目标 TR 写 labels（需 TR ≥ 3.0）")
    transfer_tag_source: bool = Field(True, title="转种时打站点标签", description="MP 风格：按种子文件里的 tracker 域名识别站点，补一条「站点/名字」标签（如 站点/我堡）；以后一眼能看出种来自哪个站")
    reseed_tags: str = Field("辅种,MovieClaw", title="辅种标签（可留空）", description="逗号分隔：通过内核 dl.submit 投递时带上，与主种（movieclaw-sub / 手动）区分开")
    exclude_tags: str = Field("", title="转种排除标签（可留空）", description="源任务带有任一标签就不转，逗号分隔。比如给不想搬的种手动打个「留」")
    min_size_gb: float = Field(0.0, title="转种最小体积（GB）", description="小于该体积的已完成种不转；0 = 不过滤")
    verify_after_transfer: bool = Field(True, title="转种后校验结果", description="加种后持续盯目标下载器：校验通过进入做种才算成功；本地报错（数据不完整 / 种子无效）自动清掉该死任务，不让它躺在那里占用清单")
    verify_fail_cleanup: bool = Field(True, title="校验失败的任务自动清理", description="只删任务条目，不删数据文件。关掉后只在报告里告警")
    verify_timeout_minutes: int = Field(90, title="转种校验超时（分钟）", description="加种后这么久还没校验完就不再等，记一条告警；0 = 不超时")
    # ---- 辅种 ----
    iyuu_token: SecretStr | None = Field(None, title="IYUU 令牌", description="iyuu.cn 用户中心的 token（站点目录免令牌，匹配接口需要）；加密存储、界面不回显。留空 = 不跑辅种")
    sid_list: list[str] = Field(default_factory=list, title="辅种站点 sid（可留空）", description="留空 = 匹配全部已知站点（推荐：没有本机映射的站会自动跳过）。只想辅种部分站点时填 sid，每行一个；GET /sites 可查目录", json_schema_extra={"multiline": True})
    reseed_save_root: str = Field("", title="辅种保存根目录（兑底）", description="辅种优先沿用原种在目标下载器里的保存目录（IYUU v2.18 口径）；拿不到原路径时落到这里。留空 = 下载器默认目录")
    reseed_max_submits: int = Field(50, title="每轮最多提交几个辅种", description="防止一次把下载器塞爆：每轮最多提交这么多条（成功或失败都算），剩下的下一轮继续。0 = 不限")
    # ---- 保活守护（智能清理）----
    sweep_enabled: bool = Field(True, title="启用保活守护（智能清理）", description="定时检查两台下载器：站点已删种、数据丢失等已无意义的种自动清理，不让下载器变成垃圾桶")
    sweep_dead: bool = Field(True, title="清理「站点已删种」", description="tracker 明确报 not registered / torrent banned 的种：站点侧已删，继续做种无意义")
    sweep_no_data: bool = Field(True, title="清理「数据丢失」", description="本地报 No data found 且校验未过的任务（数据不在了）；校验完好的不动（可能是保存路径变了）")
    sweep_protected_labels: str = Field("留,保留", title="保护标签（可留空）", description="逗号分隔：任务带有任一标签就不清理，比如手动打个「留」")
    sweep_managed_dead: bool = Field(True, title="清理「主种」里已失效的任务", description="MovieClaw 自己投递的种（movieclaw-*）数据已不在原位的，先用内核删种预演判断：如果不牵动订阅重下（那几集已经入库），才清掉这个空壳任务；会把整季退回缺资源的则一律保留")
    sweep_dry_run: bool = Field(True, title="只报告不删除", description="开着时守护只出报告不动手（推荐先跑一两个周期看报告）；关掉才真删。只删任务不删文件")
    # ---- 删片联动 ----
    link_delete_enabled: bool = Field(True, title="删片后自动停种", description="在媒体库里删片或清空回收站时，立刻把对应的下载任务从下载器里移除，不用等守护下一轮（只删任务，不删文件）")
    link_delete_files: bool = Field(False, title="删片停种时同时删数据文件", description="关闭时只移除任务、数据留在硬盘上（推荐）；开启后会连下载器里的数据一起删掉——数据若已不在媒体库里，不删就是占硬盘的孤岛文件")

    @field_validator(
        "from_downloader", "to_downloader", "source_torrent_dirs", "add_tags", "exclude_tags", "reseed_save_root", "sweep_protected_labels",
        mode="before",
    )
    @classmethod
    def _fold_text(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("sid_list", mode="before")
    @classmethod
    def _fold_list(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and not v.strip()):
            return []
        if isinstance(v, str):
            return [line.strip() for line in v.splitlines() if line.strip()]
        return v

    @field_validator("from_password", "to_password", "iyuu_token", mode="before")
    @classmethod
    def _fold_secret(cls, v: Any) -> Any:
        return None if v is None or v == "" else v

    @field_validator("transfer_enabled", "skip_verify", "autostart", "delete_source", "delete_duplicate", "delete_source_managed", "verify_after_transfer", "verify_fail_cleanup", "sweep_enabled", "sweep_dead", "sweep_no_data", "sweep_managed_dead", "sweep_dry_run", "link_delete_enabled", "link_delete_files", mode="before")
    @classmethod
    def _fold_bool(cls, v: Any, info: Any) -> Any:
        if v is None or v == "":
            return _BOOL_DEFAULTS.get(str(info.field_name), False)
        return v

    @field_validator("min_size_gb", mode="before")
    @classmethod
    def _fold_float(cls, v: Any) -> Any:
        if v is None or v == "":
            return 0.0
        return v

    @field_validator("reseed_max_submits", "verify_timeout_minutes", mode="before")
    @classmethod
    def _fold_int(cls, v: Any, info: Any) -> Any:
        fallback = 50 if str(info.field_name) == "reseed_max_submits" else 90
        if v is None or v == "":
            return fallback
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return fallback


# ---------------------------------------------------------------------------
# 纯函数（顶层，便于自测）
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    """统一路径形态：反斜杠归一成斜杠、去尾斜杠（保留根 "/"）。"""
    p = (path or "").replace("\\", "/").strip().rstrip("/")
    return p or "/"


def split_lines(text: str) -> list[str]:
    """换行分隔的设置项 → 去空后的列表。"""
    return [item.strip() for item in (text or "").splitlines() if item.strip()]


def split_tags(text: str) -> list[str]:
    """逗号分隔的标签串 → 去空后的列表。"""
    return [item.strip() for item in (text or "").split(",") if item.strip()]


def qb_tags(row: dict[str, Any]) -> list[str]:
    """qB torrents/info 行的 tags（逗号串）→ 列表。"""
    return split_tags(str(row.get("tags") or ""))


def convert_save_path(save_path: str, pairs: list[tuple[str, str]]) -> str:
    """源保存路径 → 目标保存路径。

    ``pairs`` 是（源前缀, 目标前缀）列表，按最长源前缀匹配（目录边界），
    命中即换前缀；没有任何前缀配置或未命中 → 原样返回（两台下载器视角一致）。
    """
    if not pairs or not save_path:
        return save_path
    src = _norm(save_path)
    best: tuple[str, str] | None = None
    for from_root, to_root in pairs:
        f, t = _norm(from_root), _norm(to_root)
        if not f or not t:
            continue
        if src == f or src.startswith(f + "/"):
            if best is None or len(f) > len(best[0]):
                best = (f, t)
    if best is None:
        return save_path
    f, t = best
    return t + src[len(f):] if src != f else t


def torrent_file_candidates(info_hash: str, dirs: list[str]) -> list[str]:
    """info_hash 在各源种子目录里的候选文件路径（qB/TR 都是 <hash>.torrent）。"""
    h = info_hash.lower()
    return [f"{d.rstrip('/')}/{h}.torrent" for d in dirs if d.strip()]


def _bencode_end(data: bytes, i: int) -> int:
    """从 data[i] 开始跳过一个 bencode 值，返回结束位置（不含）。"""
    c = data[i : i + 1]
    if c == b"i":
        j = data.find(b"e", i)
        if j < 0:
            raise ValueError("整数没有结尾")
        return j + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i : i + 1] != b"e":
            i = _bencode_end(data, i)
        return i + 1
    colon = data.find(b":", i)
    if colon < 0:
        raise ValueError("字符串没有长度分隔符")
    n = int(data[i:colon])
    end = colon + 1 + n
    if end > len(data):
        raise ValueError("字符串长度越界")
    return end


def info_hash_of(metainfo: bytes) -> str | None:
    """从 .torrent 原始字节算出 info_hash（v1，40 位小写 sha1）。

    直接取 info 字典的**原始字节**算哈希——重新编码会打乱键序 / 改动无关键，
    得到的 hash 就与站点 / 下载器对不上。取不到返回 None。
    """
    if not metainfo or metainfo[:1] != b"d":
        return None
    try:
        i = 1
        while i < len(metainfo) and metainfo[i : i + 1] != b"e":
            colon = metainfo.index(b":", i)
            n = int(metainfo[i:colon])
            key = metainfo[colon + 1 : colon + 1 + n]
            start = colon + 1 + n
            end = _bencode_end(metainfo, start)
            if key == b"info":
                return hashlib.sha1(metainfo[start:end]).hexdigest()
            i = end
    except (ValueError, IndexError):
        return None
    return None


def needs_trackers(metainfo: bytes) -> bool:
    """导出的 .torrent 是否缺 announce（磁力添加的种会这样）。"""
    try:
        data = bdecode(metainfo)
    except Exception:
        return False
    return isinstance(data, dict) and not data.get(b"announce")


def patch_announce(metainfo: bytes, groups: list[list[str]]) -> bytes:
    """用下载器里的 tracker 补齐缺 announce 的 .torrent 后重编码。

    announce = 第一组第一个；announce-list = 全部分组。已有 announce 原样返回。
    """
    if not groups:
        return metainfo
    data = bdecode(metainfo)
    if not isinstance(data, dict):
        raise TransferError("种子文件结构异常（顶层不是字典）")
    if data.get(b"announce"):
        return metainfo
    data[b"announce"] = groups[0][0].encode("utf-8")
    if len(groups) > 1 or len(groups[0]) > 1:
        data[b"announce-list"] = [[url.encode("utf-8") for url in group] for group in groups]
    return bencode(data)


def _announce_urls(metainfo: bytes) -> list[str]:
    """ .torrent 字节 → announce + announce-list 里的全部 tracker 地址（站点标签用）。"""
    try:
        data = bdecode(metainfo)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    urls: list[str] = []
    if isinstance(data.get(b"announce"), bytes):
        urls.append(data[b"announce"].decode("utf-8", "replace"))
    lst = data.get(b"announce-list")
    if isinstance(lst, list):
        for group in lst:
            if isinstance(group, list):
                for u in group:
                    if isinstance(u, bytes):
                        urls.append(u.decode("utf-8", "replace"))
    return urls


def qb_seeding_paused(state: str) -> bool:
    return state in _QB_SEEDING_PAUSED_STATES


def verify_cleanup_allowed(error: str) -> bool:
    """转种校验失败的任务该不该自动清掉。

    「数据不完整 / 种子无效 / 找不到数据」这类删了不可惜；
    「分片损坏」可重新校验修复，删了就找不回来了——只告警，不代劳。
    """
    msg = (error or "").lower()
    if not msg:
        return False
    if "corrupt" in msg or "verify local data" in msg:
        return False
    return True


def verify_verdict(
    row: dict[str, Any] | None, added_at: int, now: int, timeout_minutes: int,
) -> tuple[str, str]:
    """转种后置校验的单条结论 → (ok / failed / missing / pending / timeout, 说明)。

    「校验通过」以目标下载器自己的判断为准：没有本地错误 + 进度 100%。
    刚加种、还在校验的阶段返回 pending，不打扰用户。
    """
    if row is None:
        return "missing", "目标下载器里已找不到这个任务（可能被手动删除）"
    error = str(row.get("error") or "").strip()
    if error:
        return "failed", error
    if float(row.get("progress") or 0) >= 1.0:
        return "ok", "校验通过，已进入做种"
    if timeout_minutes and added_at and now - added_at > timeout_minutes * 60:
        return "timeout", f"加种后超过 {timeout_minutes} 分钟仍未校验完"
    return "pending", ""


def compact_hash_json(info_hashes: list[str]) -> str:
    """排序后的 info_hash 列表 → 紧凑 JSON（IYUU 接口口径）。"""
    hashes = sorted(h.lower() for h in info_hashes if h)
    return json.dumps(hashes, separators=(",", ":"), ensure_ascii=False)


def index_sha1(json_str: str) -> str:
    return hashlib.sha1(json_str.encode("utf-8")).hexdigest()


def parse_sites(result: Any) -> dict[str, dict[str, Any]]:
    """/reseed/sites/index 的 result.sites → {sid: 站点记录}。"""
    sites = (result or {}).get("sites") or []
    return {
        str(site["id"]): site
        for site in sites
        if isinstance(site, dict) and site.get("id") is not None
    }


def parse_matches(data: Any) -> dict[str, list[dict[str, Any]]]:
    """匹配响应 data → {info_hash: [ {sid, torrent_id, info_hash}, ... ]}。"""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(data, dict):
        return out
    for info_hash, payload in data.items():
        torrent = payload.get("torrent") if isinstance(payload, dict) else None
        if torrent is None:
            continue
        entries = torrent if isinstance(torrent, list) else [torrent]
        seeds = [
            seed for seed in entries
            if isinstance(seed, dict) and seed.get("sid") and seed.get("torrent_id")
        ]
        if seeds:
            out[info_hash.lower()] = seeds
    return out


def domain_of(url: str) -> str:
    """取小写主机名（去 www. 前缀、去端口），用于 sid ↔ 本机站点的域名匹配。"""
    if "://" not in url:
        url = f"https://{url}"
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


#: IYUU 站点 slug 与本机 site_id 的已知别名（其余同名直配；对不上再走域名兜底）
_SLUG_ALIASES = {"m-team": "mteam"}


def slug_of(site: dict[str, Any]) -> str:
    raw = str(site.get("site") or "")
    return _SLUG_ALIASES.get(raw, raw)


_DOWNLOAD_UNKNOWN_PARAMS = ("passkey", "uid", "hash", "sign", "torrent_key", "auth")
#: tracker 域名里不当作站点标识的前缀段
_TRACKER_HOST_NOISE = frozenset({"tracker", "trackers", "t", "pt", "www", "bt", "announce", "announce2"})


def extract_credentials(url: str) -> dict[str, str]:
    """取种链接 → 站点凭据（passkey/sign 等），能提到就回字典。

    这些凭据同一站点全局通用（passkey 是用户级、sign 由 id+时间算但同批
    请求可复用），拿到后入凭据库，IYUU 模板链接兑底时回填。
    """
    out: dict[str, str] = {}
    m = re.search(r"[?&](passkey|sign|torrent_key|auth)=([A-Za-z0-9_.=-]{6,})", str(url or ""))
    if m:
        out[m.group(1)] = m.group(2)
    return out


def apply_credentials(template_url: str, creds: dict[str, str]) -> str:
    """IYUU 模板链接 + 凭据库 → 回填 {passkey} 等占位符后的可用链接。

    支持两种写法：查询参数（`?passkey={passkey}`）与路径段（`/dl/{id}/{passkey}`）。
    库里有值就换成真值；没有的占位符整段剔除（多数站只差 passkey，
    其余 {uid}/{hash} 剔掉不影响取种）。
    """
    path = str(template_url or "")
    if not path:
        return ""
    for name in _DOWNLOAD_UNKNOWN_PARAMS:
        value = creds.get(name)
        if value:
            # 查询参数形态
            path = re.sub(
                r"([?&])\b" + name + r"=\{[a-zA-Z_]+\}", lambda m: m.group(1) + name + "=" + value, path
            )
            # 路径段形态（TTG: /dl/{id}/{passkey}）与裸占位符
            path = path.replace("{" + name + "}", value)
        else:
            path = re.sub(r"[?&]\b" + name + r"=\{[a-zA-Z_]+\}", "", path)
            path = re.sub(r"/\{" + name + r"\}", "", path)
            path = path.replace("{" + name + "}", "")
    return path.replace("?&", "?")


def fill_download_template(template: str, torrent_id: Any) -> str:
    """IYUU 的 download_page 模板 → 可请求路径（保留 {passkey} 等占位符）。

    只把 `{}` 换成种子 id；凭据占位符留给凭据库回填（`apply_credentials`）——
    在这里提前剥掉会让后面拿不到占位符、passkey 永远填不进去。
    """
    return (template or "").replace("{}", str(torrent_id))


def strip_placeholders(url: str) -> str:
    """把填不出的占位符整段去掉（查询参数与路径段两种形态，最后的兜底）。"""
    path = str(url or "")
    for name in _DOWNLOAD_UNKNOWN_PARAMS:
        path = re.sub(rf"[?&]\b{name}=\{{[a-zA-Z_]+\}}", "", path)
        path = re.sub(rf"/\{{{name}\}}", "", path)
        path = path.replace("{" + name + "}", "")
    path = path.replace("?&", "?")
    return path.strip("&?")


def build_download_url(
    base_url: str, torrent_id: Any, is_https: Any = 1, *, api_style: bool = False, template: str = "",
) -> str:
    """IYUU 的 sid + torrent_id → 站点框架认识的取种地址。

    API 型站点（auth_type=apikey）宿主收的是纯种子 id，拼网址反而会 500，直接透传；
    其余站点优先按 IYUU 的 download_page 模板拼，没模板时回落到 NexusPHP 的
    download.php?id=。
    """
    if api_style:
        return str(torrent_id)
    scheme = "http" if str(is_https) == "0" else "https"
    host = base_url.removeprefix("https://").removeprefix("http://").rstrip("/")
    if template:
        path = fill_download_template(template, torrent_id)
        if path:
            return f"{scheme}://{host}/{path.lstrip('/')}"
    return f"{scheme}://{host}/download.php?id={torrent_id}"


def _norm_title(title: str) -> str:
    """发布名 → 宽松比对形态：抽取「主体词」序列用于前缀比对。

    规则：小写、按非字母数字中文切段、丢弃纯数字段与编码/来源噪声词，
    拼成主体串；比对时短的一方是长一方的前缀即视为同源发布
    （IYUU 名与站点搜索结果常是同一发布的不同裁剪）。
    """
    noise = {"2160p", "1080p", "720p", "webdl", "webrip", "bluray", "remux",
             "h265", "h264", "hevc", "x264", "x265", "hdr", "hdr10", "dv", "dovi", "dolby",
             "vision", "ddp", "dd", "ddplus", "aac", "aac2", "atmos", "truehd", "dts",
             "dtshd", "ma", "10bit", "8bit", "60fps", "hfr", "complete", "hq", "uhd",
             "blu", "ray", "dvd", "hdtv", "dvdrip", "lpcm", "aac2", "0", "2", "0", "1",
             "s01", "s02", "s03", "s04", "s05", "season", "web", "dl", "rip"}
    parts: list[str] = []
    for raw in re.split(r"[^0-9a-z\u4e00-\u9fff]+", str(title or "").lower()):
        if not raw or raw.isdigit() or raw in noise:
            continue
        # 混合段再切一层：如 "s01ubweb"（组名粘在集号后）→ 丢 s01；
        # "ddp5adweb" 里的内嵌数字（DDP5.1）也要拆掉再判噪声
        while raw:
            m = re.match(r"^(s\d{1,3}e?\d{0,3})(.*)$", raw)
            if m:
                raw = m.group(2)
                continue
            m = re.match(r"^(\d{3,4}[pi]?)(.*)$", raw)
            if m:
                raw = m.group(2)
                continue
            m = re.match(r"^([a-z]+?\d+(?:[._]?\d+)?)(.*)$", raw)
            if m and (m.group(1) in noise or re.sub(r"\d+$", "", m.group(1)) in noise):
                raw = (m.group(2) or "")
                continue
            # 段尾带集数/声道号的噪声词（ddp5 → ddp）
            stem = re.sub(r"\d+$", "", raw)
            if stem != raw and stem in noise:
                raw = ""
                continue
            break
        if raw and raw not in noise:
            parts.append(raw)
    return parts


def _title_match(a: str, b: str) -> bool:
    """两个发布名是否同一主体发布。

    口径：各自抽出主体词序列（滤掉编码/来源噪声、组名尾段），存在
    一方是另一方的前缀即视为同源——IYUU 名与站点搜索结果常来自不
    同人的转载命名，可能多片头译名（如「凡人修仙传」+ 原英文名），
    所以允许带额外前缀；主体词序列一致就够可信。
    """
    pa, pb = _norm_title(a), _norm_title(b)
    if not pa or not pb:
        return False
    # 中文单段兜底：两边各只有一个词且含中文、一方是另一方的字符串前缀、
    # 长度差超过 2（凡人修仙传 vs 凡人修仙传年番）→ 不同源。
    # 中文名切不开词，无脑前缀匹配会把「X」误配「X年番」「X第二季」
    if (
        len(pa) == 1 and len(pb) == 1
        and any("一" <= ch <= "鿿" for ch in pa[0] + pb[0])
        and (pa[0].startswith(pb[0]) or pb[0].startswith(pa[0]))
        and abs(len(pa[0]) - len(pb[0])) > 2
    ):
        return False
    # 同源判定：短序列是长序列的连续子序列（含首尾）即可。
    # IYUU 名与站点结果常来自不同转载：可能多片头译名、组名不同
    # （ubweb/adweb）、或带 H&R 页专用尾巴，主体词一致就够可信；
    # 顺序不同不认（防「Dark.the.Light」误配「Light.the.Dark」）
    if len(pa) <= len(pb):
        short, long_ = pa, pb
    else:
        short, long_ = pb, pa
    if short == long_[: len(short)] or short == long_[-len(short):]:
        return True
    for i in range(1, len(long_) - len(short)):
        if long_[i : i + len(short)] == short:
            return True
    # 第二轮：完整序列上去头/去尾比对——覆盖「多译名头」（凡人修仙传 年番4
    # + 英文名 vs 纯英文名）与「组名尾巴不同」（UBWEB vs ADWeb，一方尾段
    # 是标题词一方是组名时盲目砍尾会砍错，所以在完整序列上双向裁）
    for cut_a, cut_b in [(0, c) for c in range(0, min(3, len(pb)))] + [
        (c, 0) for c in range(1, min(3, len(pa)))
    ]:
            body_a, body_b = pa[cut_a:], pb[cut_b:]
            if not body_a or not body_b:
                continue
            # 完全相等
            if body_a == body_b:
                return True
            # 主体相等（各去尾段——多半是组名，UBWEB vs ADWeb）；去尾后必须还有词
            if len(body_a) > 1 and len(body_b) > 1 and body_a[:-1] == body_b[:-1]:
                return True
            # 一方是另一方的前缀（一方多个译名头）——短方至少两个词：
            # 裁剪到只剩一个词时前缀比对等于丢掉词序约束，会把
            # 「Dark the Light」错配成「Light the Dark」
            if 2 <= len(body_a) <= len(body_b) and body_b[: len(body_a)] == body_a:
                return True
            if 2 <= len(body_b) <= len(body_a) and body_a[: len(body_b)] == body_b:
                return True
            # 主体相等（短方去掉自己的尾段组名后是长方去尾段主体词的前缀）
            if len(body_a) > 1 and len(body_b) > 1 and body_b[:-1][: len(body_a) - 1] == body_a[:-1]:
                return True
            if len(body_a) > 1 and len(body_b) > 1 and body_a[:-1][: len(body_b) - 1] == body_b[:-1]:
                return True
    # 兜底反例：两序列最终各只剩一个纯中文段且一方是另一方的前缀延伸
    # （凡人修仙传 vs 凡人修仙传年番）——中文名无法切词，长度差超过 2
    # 就不当同源，防止「X」误配「X年番」「X第二季」
    if len(pa) == 1 and len(pb) == 1 and pa[0].isascii() is False or (
        len(pa) == 1 and len(pb) == 1
        and any("\u4e00" <= ch <= "\u9fff" for ch in pa[0] + pb[0])
        and (pa[0].startswith(pb[0]) or pb[0].startswith(pa[0]))
        and abs(len(pa[0]) - len(pb[0])) > 2
    ):
        return False
    return False


def extract_download_href(html: str) -> str | None:
    """站点详情页 HTML → 真实取种链接（带 passkey/sign 的那种）。

    NexusPHP 的详情页把下载入口写成 ``href="download.php?id=..&passkey=.."``，
    部分站是 ``/dl/<id>/<key>``。取第一个匹配即可。
    """
    if not html:
        return None
    m = re.search(r"""href=["']([^"']*(?:download\.php|/dl/)[^"']*)["']""", html, re.I)
    if not m:
        return None
    href = html_lib.unescape(m.group(1)).strip()
    if href.startswith("//"):
        href = "https:" + href
    return href or None


async def resolve_real_download_url(
    call_ops, site_id: str, torrent_id: Any, search_keyword: str, log, cred_sink=None,
) -> str | None:
    """按发布名在目标站搜出真实取种链接（宿主带登录态，不需要 passkey）。

    IYUU 给的模板链接（download.php?id=x 或 /dl/x/{passkey}）在不少站直接
    404；宿主搜索结果里的 download_url 是站点适配器从页面抓的带登录态
    链接，用它能直接取种。 torrent_id 能对上才信，对不上返回 None。
    call_ops 是宿主操作调用器（apply 闭包里的 ops.call），log 是插件日志。
    """
    keyword = (search_keyword or "").strip()
    if not keyword:
        return None
    # 搜索词降级：站点搜索引擎普遍吃「短词」不吃整句发布名——
    # 整句 → 主标题（去 .分隔 的尾部编码段）→ 主标题再截短，逐级试
    clean = re.sub(r"[\[\]（）()【】]", " ", keyword)
    terms = [clean if clean != keyword else keyword]
    head = re.split(r"\.(?:19|20)\d{2}\.", clean, maxsplit=1)[0]
    if head and head != keyword:
        terms.append(head)
    words = head.split(".")
    if len(words) > 2 and words[0]:
        terms.append(words[0])
    else:
        # 空格分隔的标题（火遮眼 The Furious 2026 …）：砍到前 3 个词再试
        sp = keyword.split()
        if len(sp) > 3:
            terms.append(" ".join(sp[:3]))
    want = str(torrent_id or "")
    want_norm = _norm_title(keyword)
    exact: str | None = None
    fuzzy: str | None = None
    total_seen = 0
    for term in terms:
        if not term or (exact or fuzzy):
            break
        try:
            result = await call_ops(
                "search.torrents",
                {"keyword": term, "sites": [site_id], "page": 1},
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 -- 搜索失败不阻断，回落模板链接
            log("info", f"辅种搜索真实链接（{site_id}）失败，回落模板链接：{getattr(exc, 'message', None) or exc}")
            return None
        hits = (result or {}).get("items") or []
        total_seen += len(hits)
        for hit in hits:
            url = str(hit.get("download_url") or "")
            if not url:
                continue
            hit_id = str(hit.get("torrent_id") or "")
            hit_title = str(hit.get("title") or "")
            if cred_sink is not None:
                await cred_sink(extract_credentials(url))
            if want and hit_id == want:
                exact = exact or url
                log("info", f"  → id 精确命中：{hit_title[:50]!r} {url[:60]}")
            if not fuzzy and _title_match(hit_title, keyword):
                fuzzy = url
                log("info", f"  → 名称匹配命中：{hit_title[:50]!r} {url[:60]}")
        if not exact and not fuzzy and hits:
            log("info", f"  → 有 {len(hits)} 条但未命中（id/名称都对不上），试下一级搜索词")
    if not exact and not fuzzy:
        log("info", f"辅种搜索真实链接（{site_id}）：各搜索词共 {total_seen} 条结果均未命中，回落模板链接")
    return exact or fuzzy


# ---------------------------------------------------------------------------
# 保活守护：智能判断哪些种已经没意义了（站点删种 / 数据丢失 / 校验不过）
# ---------------------------------------------------------------------------


def tracker_dead_reason(tracker_msg: str) -> str | None:
    """tracker 报错文案 → 命中的死种原因；没命中返回 None。子串不区分大小写。"""
    msg = (tracker_msg or "").lower()
    for snippet in _TRACKER_DEAD_MESSAGES:
        if snippet in msg:
            return snippet
    return None


def link_delete_verdict(torrent: dict[str, Any]) -> str | None:
    """「片子被删了」→ 这个下载器任务能不能停；能停返回 None，不能停返回原因。

    安全线（任一命中就保留任务）：
    - 下载器配置已经没了，无从定位；
    - 这个种还供着媒体库里别的文件（季包别的集、合集里别的条目、别的库）；
    - 投递时记着有 H&R 考核风险（删了会违约）。
    """
    if torrent.get("downloader_id") is None:
        return "下载器配置已删除，无从定位"
    if torrent.get("other_file_ids"):
        return "这个种还供着媒体库里别的文件"
    if torrent.get("hit_and_run"):
        return "投递时有 H&R 考核风险"
    return None


def files_of_payload(payload: Any) -> list[int]:
    """删除类事件载荷 → 涉及的媒体库文件 id（file.deleted 是 files[]，purged 是单个 file）。"""
    ids: list[int] = []
    for item in getattr(payload, "files", None) or ():
        value = getattr(item, "id", None)
        if isinstance(value, int):
            ids.append(value)
    single = getattr(payload, "file", None)
    value = getattr(single, "id", None)
    if isinstance(value, int):
        ids.append(value)
    return ids


def tr_torrent_verdict(
    *,
    error: int,
    error_string: str,
    tracker_stats: list[dict[str, Any]],
    labels: list[str],
    protected_labels: set[str],
) -> tuple[str | None, str]:
    """TR 单个任务 →（死种原因, 人话说明）；活着返回 (None, "")。

    判据（满足其一即死，且任一标签命中保护名单则放过）：
    1. tracker 明确报「种子已被站点删除 / 封禁」（trackerStats.lastAnnounceResult）；
    2. 本地错误「No data found」且校验没到 100%——数据已经丢了，
       校验到 100% 的说明数据其实还在（多半是保存路径变了），不在此列。
    """
    if protected_labels.intersection(labels):
        return None, ""
    for ts in tracker_stats or []:
        if ts.get("isBackup"):
            continue
        reason = tracker_dead_reason(str(ts.get("lastAnnounceResult") or ""))
        if reason:
            return "tracker_deleted", f"tracker 报告：{(ts.get('lastAnnounceResult') or '')[:80]}"
    if error == _TR_ERROR_LOCAL and _TR_NO_DATA_SNIPPET in (error_string or "").lower():
        return "no_data", f"本地错误：{(error_string or '')[:80]}"
    if error == _TR_ERROR_LOCAL and any(k in (error_string or "").lower() for k in ("corrupt", "verify local data")):
        return "corrupt", f"数据损坏：{(error_string or '')[:80]}（建议重新校验或重下，不自动删）"
    return None, ""


def qb_torrent_verdict(
    *,
    trackers: list[dict[str, Any]],
    state: str,
    protected_labels: set[str],
    labels: list[str],
) -> tuple[str | None, str]:
    """qB 单个任务 →（死种原因, 说明）。

    判据：全部真实 tracker（tier ≥ 0 且非伪条目）都处于 status=4（报错）
    且报错文案命中死种清单；DHT/PeX/LSD 伪条目（url 以 * 开头）与
    「私有 torrent」提示行（status=0）不参与判断。
    """
    if protected_labels.intersection(labels):
        return None, ""
    real = [t for t in trackers or [] if not str(t.get("url") or "").startswith("*") and t.get("tier") != -1]
    if not real:
        return None, ""
    dead = 0
    note = ""
    for t in real:
        status = int(t.get("status") or 0)
        msg = str(t.get("msg") or "")
        if status == _QB_TRACKER_WORKING[0] or status in _QB_TRACKER_WORKING:
            return None, ""                      # 有一个活着就是活种
        if status == _QB_TRACKER_ERROR:
            if tracker_dead_reason(msg):
                dead += 1
                note = msg[:80]
            elif "私有" in msg or "private" in msg.lower():
                return None, ""                  # 私有提示不算报错
    if real and dead == len(real):
        return "tracker_deleted", f"全部 tracker 报告：{note}"
    return None, ""


#: 常见 tracker 域名 → 站点中文标记（打标签用；认不出的取域名主体）
_TRACKER_DOMAINS: tuple[tuple[str, str], ...] = (
    ("ourbits.club", "我堡"), ("hdsky.me", "天空"), ("m-team.io", "馒头"), ("m-team.cc", "馒头"),
    ("kp.m-team.cc", "馒头"), ("ptkeepfrds.com", "朋友"), ("ptkeepfrds.club", "朋友"),
    ("pthome.net", "铂金家"), ("tjupt.org", "北洋"), ("pterclub.net", "猫站"), ("pterclub.com", "猫站"),
    ("hdhome.org", "家园"), ("btschool.net", "学校"), ("torrentccf.com", "他吹吹风"),
    ("totheglory.im", "听听歌"), ("ttgmedia.com", "听听歌"), ("nanyangpt.com", "南洋"),
    ("hdcity.in", "城市"), ("nicept.net", "老师"), ("52pt.site", "52pt"), ("eastgame.org", "吐鲁番"),
    ("springsunday.net", "春天"), ("soulvoice.net", "聆音"), ("chdbits.co", "彩虹岛"),
    ("ptchdbits.co", "彩虹岛"), ("ptsbao.club", "烧包"), ("hdarea.club", "好大"), ("hdtime.org", "时光"),
    ("1ptba.com", "1ptba"), ("hd4fans.org", "兽"), ("opencd.cc", "皇后"), ("joyhd.net", "开心"),
    ("dmhy.org", "幼儿园"), ("upxin.net", "好多油"), ("discfan.net", "蝶粉"), ("byr.pt", "北邮人"),
    ("dicmusic.club", "海豚"), ("skyeysnow.com", "天雪"), ("hdroute.org", "路"), ("haidan.video", "海胆"),
    ("hdfans.org", "红豆饭"), ("dragonhd.xyz", "龙之家"), ("hitpt.com", "百川"),
    ("greatposterwall.com", "海豹"), ("hdpost.top", "普斯特"), ("hudbt.pt", "蝴蝶"),
    ("audiences.me", "观众"), ("piggo.me", "猪猪"), ("wintersakura.com", "冬樱"),
    ("hhanclub.top", "憨憨"), ("hhanclub.net", "憨憨"), ("hdvideo.xyz", "高清视频"),
    ("pt0ff.cc", "自由农场"), ("ptchina.org", "铂金学院"), ("zhuque.in", "朱雀"), ("zmpt.cc", "织梦"),
    ("rousi.one", "肉丝"), ("monikadesign.uk", "莫妮卡"), ("cyanbug.net", "大青虫"),
    ("ubits.club", "优堡"), ("pandapt.net", "熊猫"), ("carpt.net", "车站"), ("shadowflow.org", "影"),
    ("agsvpt.com", "末日"), ("agsvpt.trackers.work", "末日"), ("ptvicomo.net", "象岛"),
    ("qingwapt.com", "青蛙"), ("xingtan.one", "杏坛"), ("hdkyl.in", "麒麟"), ("ilolicon.com", "爱萝莉"),
    ("gtkpw.com", "GTK"), ("okpt.net", "okpt"), ("crabpt.vip", "蟹黄堡"), ("hddolby.com", "杜比"),
    ("kamept.com", "龟站"), ("ptcafe.club", "咖啡"), ("yemapt.org", "YemaPT"), ("ptlgs.xyz", "劳改所"),
    ("lemonhd.org", "柠檬"), ("raingfh.top", "雨"), ("njtupt.top", "浦园"), ("ptzone.net", "PTzone"),
    ("hdclone.org", "hdclone"), ("kufei.org", "库非"), ("ptlover.cc", "AFun"),
    ("xingyunge.fun", "星陨阁"), ("cspt.xyz", "财神"), ("tmpt.cc", "唐门"), ("htpt.cc", "海棠"),
    ("sewerpt.com", "下水道"), ("gamegamept.com", "GGPT"), ("myptcc.com", "我的PT(CC)"),
    ("longpt.xyz", "LongPT"), ("hdbao.xyz", "hdbao"), ("13city.top", "13city"),
    ("duckboobee.com", "March"), ("luckpt.net", "LuckPT"), ("ptskit.com", "PTSKIT"),
    ("playletpt.com", "PlayletPT"), ("novahd.net", "NovaHD"), ("lajidui.xyz", "垃圾堆"),
    ("hxpt.xyz", "好学"), ("dubhe.top", "天枢"), ("tangpt.com", "躺平PT"), ("muxuege.fun", "慕雪阁"),
    ("zrpt.link", "自然"), ("siqi.dev", "思齐"), ("baozi.pt", "包子PT"), ("cangbaoge.top", "藏宝阁"),
)


def site_tag_from_trackers(announce_urls: list[str]) -> str | None:
    """种子的 tracker announce 列表 → 一条「站点/名字」标签（MP 风格）。

    域名在已知表里用中文昵称；认不出的取域名主体（去掉常见的
    tracker./kp./pt. 前缀再取第一段，如 tracker.hdsky.me → hdsky），
    总能打上点什么；没有 tracker 返回 None（不打）。
    """
    for url in announce_urls or []:
        raw = url if "://" in url else f"https://{url}"
        host = (urlsplit(raw).hostname or "").lower().removeprefix("www.")
        if not host:
            continue
        for domain, mark in _TRACKER_DOMAINS:
            if host == domain or host.endswith("." + domain):
                return f"站点/{mark}"
        first = host.split(".")[0]
        if first in ("tracker", "kp", "pt", "t", "bt"):
            first = host.split(".")[1] if "." in host else first
        return f"站点/{first}"
    return None


# ---------------------------------------------------------------------------
# 插件入口
# ---------------------------------------------------------------------------


@plugin(
    PLUGIN_ID,
    title="保种管家",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_FILES, PLUGIN_ROUTES, PLUGIN_HEALTH, PLUGIN_EVENTS, DURABLE_EVENTS),
    permissions=("dl.tasks", "dl.submit", "dl.list", "dl.file-sources.list", "dl.torrent.delete", "site.list", "site.show", "search.torrents", "site.proxy.request"),
    config=Settings,
)
async def apply(ctx: Context) -> None:
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    files = ctx.use(PLUGIN_FILES).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    ops = await ctx.use(HOST_OPS).client(ctx)
    logger = ctx.logger
    health = ctx.use(PLUGIN_HEALTH).reporter(ctx)
    events = await ctx.use(PLUGIN_EVENTS).register(
        ctx, TRANSFER_COMPLETED, RESEED_COMPLETED, SWEEP_COMPLETED,
    )

    # ------------------------------------------------------------------
    # 站点凭据库：passkey/sign 等能拿到就入库（cred:<site_id>），
    # 新值覆盖旧值；IYUU 模板兑底时回填占位符，让兑底链接能用
    # ------------------------------------------------------------------

    async def learn_passkeys_from_torrents() -> dict[str, str]:
        """从本机已有种子的 tracker 地址里学各站 passkey（域名 → passkey）。

        比「浏览站点列表」可靠得多：种子文件里的 announce 本来就是你自己的
        passkey，IYUU 的 download_page 模板回填后即可正常取种。
        """
        found: dict[str, str] = {}
        for dl in await _dl_rows():
            if not dl.get("usable"):
                continue
            try:
                async with _client_for(dl) as client:
                    announces = await client.all_trackers()
            except (TransferError, HTTPException):
                continue
            for ann in announces:
                creds = extract_credentials(ann)
                if not creds.get("passkey"):
                    continue
                host = domain_of(ann)
                if not host:
                    continue
                labels = host.split(".")
                keys = {host}
                if len(labels) >= 2:
                    keys.add(".".join(labels[-2:]))
                # 前缀段（agsvpt.trackers.work → agsvpt / trackers）：站点 slug
                # 常出现在 tracker 域名里，用它兜住「IYUU 域名与本机 tracker
                # 域名不是同一后缀」的站（agsvpt.com ↔ agsvpt.trackers.work）
                for label in labels[:-2]:
                    if label and label not in _TRACKER_HOST_NOISE:
                        keys.add(label)
                for k in keys:
                    found.setdefault(k, creds["passkey"])
        return found

    async def creds_of(site_id: str) -> dict[str, str]:
        raw = await store.get(f"cred:{site_id}")
        # 存储形态有三种：dict（本插件写入）、JSON 字符串（旧版）、被再包一层的
        # JSON 字符串——统一还原成 dict，否则读回来永远是空、回填永远不生效
        for _ in range(3):
            if isinstance(raw, dict):
                return {k: str(v) for k, v in raw.items() if k != "_updated"}
            if isinstance(raw, str) and raw.strip()[:1] in ("{", '"'):
                try:
                    raw = json.loads(raw)
                    continue
                except Exception:
                    break
            break
        return {}

    async def creds_save(site_id: str, new_creds: dict[str, str], source: str) -> bool:
        """新凭据入库：与旧值比对，有变化才覆盖。返回是否真的更新了。"""
        if not site_id or not new_creds:
            return False
        old = await creds_of(site_id)
        merged = {**old, **new_creds}
        if merged == old:
            return False
        payload = {**merged, "_updated": int(time.time())}
        await store.set(f"cred:{site_id}", payload)
        changed = {k: v for k, v in new_creds.items() if old.get(k) != v}
        plog("info", f"凭据库：站点 {site_id} 更新 {list(changed)}（来源：{source}）")
        return True

    async def harvest_credentials(site_id: str) -> None:
        """浏览目标站最新种子页，从结果链接里收割凭据入库。

        keyword 留空 = 宿主的浏览模式（不打搜索接口、对站点最轻）。
        每轮辅种对计划站点做一次；订阅搜索、手动搜索命中时也会顺带入库。
        """
        try:
            result = await ops.call(
                "search.torrents", {"keyword": "", "sites": [site_id], "page": 1}, timeout=45,
            )
        except Exception as exc:
            plog("info", f"凭据收割（{site_id}）浏览失败：{getattr(exc, 'message', None) or exc}")
            return
        found: dict[str, str] = {}
        for hit in (result or {}).get("items") or []:
            found.update(extract_credentials(str(hit.get("download_url") or "")))
        if found:
            await creds_save(site_id, found, "站点浏览")

    # 插件日志环：只收本插件的关键事件，接口 /logs 直接看，不用去翻主日志
    # ------------------------------------------------------------------

    class _LogRing:
        """内存双端队列（最新在后）+ 追加写插件私有文件（重启不丢，读接口可翻历史）。"""

        def __init__(self, capacity: int = 500) -> None:
            self._items: deque[dict[str, str]] = deque(maxlen=capacity)
            self._file = files.path("plugin", "logs.jsonl")

        def write(self, level: str, message: str) -> None:
            entry = {
                "ts": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S"),
                "level": level,
                "message": message,
            }
            self._items.append(entry)
            # 文件写入必须走文件接口（本进程无权直接碰盘）：交给事件循环异步落盘，
            # 写不进去不影响主流程；内存环里总还有
            try:
                asyncio.get_running_loop().create_task(self._append(entry))
            except RuntimeError:
                pass

        async def _append(self, entry: dict[str, str]) -> None:
            line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                f = await files.open(self._file, "ab")
            except Exception:
                return
            try:
                await asyncio.to_thread(f.write, line)
            except Exception:
                pass
            finally:
                try:
                    await asyncio.to_thread(f.close)
                except Exception:
                    pass

        async def tail(self, level: str | None, limit: int, include_file: bool) -> list[dict[str, str]]:
            if include_file and not self._items:
                await self.load_history()
            rows = list(self._items)
            if level in ("info", "warning", "error"):
                order = {"info": 0, "warning": 1, "error": 2}
                rows = [r for r in rows if order.get(r["level"], 0) >= order[level]]
            return rows[-limit:]

        async def load_history(self) -> None:
            """插件重启后内存环是空的：把文件里的历史读回来（最多末尾 500 条）。"""
            try:
                if not await files.exists(self._file):
                    return
                f = await files.open(self._file, "rb")
            except Exception:
                return
            raw = b""
            try:
                raw = await asyncio.to_thread(f.read)
            except Exception:
                pass
            finally:
                try:
                    await asyncio.to_thread(f.close)
                except Exception:
                    pass
            for line in raw.decode("utf-8", "replace").splitlines()[-500:]:
                try:
                    self._items.append(json.loads(line))
                except Exception:
                    continue

        async def clear_file(self) -> None:
            try:
                f = await files.open(self._file, "wb")
            except Exception:
                return
            try:
                await asyncio.to_thread(f.close)
            except Exception:
                pass

    log_ring = _LogRing()
    await log_ring.load_history()  # 启动先读历史，再写「已加载」，日志才能跨重启接上

    def plog(level: str, message: str) -> None:
        """插件日志双写：ctx.logger（进主日志）+ 内存环/文件（接口可查）。"""
        getattr(logger, level if level != "success" else "info")(message)
        log_ring.write(level, message)

    async def publish_event(event: Any, payload: Any) -> None:
        """发一条插件事件（没人订就什么都不做）；失败不影响主流程。"""
        try:
            await events.publish(event, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 事件发不出去不该打断一轮任务
            plog("warning", f"事件发布失败（{getattr(event, 'name', event)}）：{exc}")

    async def health_ok(key: str) -> None:
        try:
            await health.ok(key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 健康上报失败不影响任务
            pass

    async def health_bad(key: str, message: str) -> None:
        """报告降级：同一条消息只进一次系统通知，好转后调 health_ok 自动消退。"""
        try:
            await health.degraded(key, message, action_href="/settings/plugins")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 通用：下载器解析与直连客户端
    # ------------------------------------------------------------------

    async def resolve_downloader(raw: str, which: str, *, allow_default: bool = False) -> dict[str, Any] | None:
        """设置里的名称 / 数字 id → dl.list 那一行；allow_default 且留空 → None（默认下载器）。"""
        raw = (raw or "").strip()
        if not raw:
            if allow_default:
                return None
            raise HTTPException(status_code=400, detail=f"还没填{which}下载器：到插件设置里选好再运行")
        try:
            rows = await _dl_rows()
        except OpsError as exc:
            raise HTTPException(status_code=502, detail=f"读取下载器列表失败（{exc.message}）") from exc
        row: dict[str, Any] | None = None
        if raw.isdigit():
            row = next((r for r in rows if r.get("id") == int(raw)), None)
        if row is None:
            lowered = raw.lower()
            matches = [r for r in rows if str(r.get("name") or "").strip().lower() == lowered]
            if len(matches) == 1:
                row = matches[0]
        if row is None:
            available = "、".join(f"{r.get('id')}={r.get('name')}[{r.get('client_type')}]" for r in rows) or "（一台都没配）"
            raise HTTPException(status_code=400, detail=f"找不到{which}下载器「{raw}」。可用：{available}")
        if not row.get("usable"):
            raise HTTPException(status_code=400, detail=f"{which}下载器「{row.get('name')}」当前不可用：{row.get('last_error') or '连接测试未通过'}")
        return row

    def _transport() -> httpx.AsyncBaseTransport:
        return net.http_transport(PLUGIN_ID)

    def _password(which: str, secret: SecretStr | None) -> str:
        value = secret.get_secret_value() if secret else ""
        if not value:
            raise HTTPException(status_code=400, detail=f"还没填{which}下载器的 WebUI 密码：到插件设置里填好再运行")
        return value

    def _is_configured(row: dict[str, Any], raw: str) -> bool:
        raw = (raw or "").strip()
        if not raw:
            return False
        if raw.isdigit() and row.get("id") == int(raw):
            return True
        return str(row.get("name") or "").strip().lower() == raw.lower()

    def _password_for(row: dict[str, Any]) -> str:
        """按下载器在配置里的角色取密码（源用 from_password，目标用 to_password）。"""
        cfg = ctx.config
        src_pwd = cfg.from_password.get_secret_value() if cfg.from_password else ""
        dst_pwd = cfg.to_password.get_secret_value() if cfg.to_password else ""
        if src_pwd and _is_configured(row, cfg.from_downloader):
            return src_pwd
        if dst_pwd and _is_configured(row, cfg.to_downloader):
            return dst_pwd
        pwd = src_pwd or dst_pwd
        if not pwd:
            raise HTTPException(status_code=400, detail=f"还没填下载器「{row.get('name')}」的 WebUI 密码")
        return pwd

    async def _data_missing(item: dict[str, Any]) -> bool:
        """源种的数据还在不在原位（被整理/进回收站后 qB 仍可能报告完成）。

        只做一次 stat：路径不在已批准的目录里时返回 False（无法核对，按存在处理）。
        """
        path = str(item.get("content_path") or "").strip()
        if not path:
            return False
        try:
            st = await files.stat(path)
            if st is None:
                return True
            # 多文件包：目录还在但里面空了（被整理走 / 进回收站）同样算数据没了
            if st.get("is_dir"):
                return not await files.listdir(path)
            return False
        except PermissionError:
            return False
        except OSError:
            return False

    _dl_cache: dict[str, Any] = {"rows": None, "at": 0.0}

    async def _dl_rows() -> list[dict[str, Any]]:
        """dl.list 的短缓存（30 秒）：一轮流程里多处分头调用，没必要重复问宿主。"""
        now = time.monotonic()
        if _dl_cache["rows"] is not None and now - _dl_cache["at"] < 30:
            return _dl_cache["rows"]
        rows = await ops.call("dl.list")
        _dl_cache.update(rows=rows, at=now)
        return rows

    @asynccontextmanager
    async def _client_for(dl: dict[str, Any]):
        """按下载器类型建客户端并保证关闭（qB 登录 / TR RPC 二选一）。"""
        pwd = _password_for(dl)
        url = str(dl.get("url") or "")
        username = str(dl.get("username") or "")
        if dl.get("client_type") == "qbittorrent":
            client: Any = await QbittorrentWebUI.login(_transport(), url, username, pwd)
        else:
            client = await TransmissionRPC.connect(_transport(), url, username, pwd)
        try:
            yield client
        finally:
            await client.close()

    async def _completed_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
        """直接问下载器要「已完成」的种（dl.tasks 会隐藏已完成的外部种，不能拿来当来源）。"""
        if row.get("client_type") == "qbittorrent":
            async with _client_for(row) as client:
                rows = await client.completed()
            return [
                {
                    "hash": str(r.get("hash") or "").lower(),
                    "name": str(r.get("name") or ""),
                    "save_path": str(r.get("save_path") or ""),
                    "content_path": str(r.get("content_path") or ""),
                    "tags": qb_tags(r),
                    "size_bytes": int(r.get("size") or 0),
                }
                for r in rows
                if r.get("hash")
            ]
        async with _client_for(row) as client:
            rows = await client.completed()
        return [
            {
                "hash": str(r.get("hashString") or "").lower(),
                "name": str(r.get("name") or ""),
                "save_path": str(r.get("downloadDir") or ""),
                "tags": [str(x) for x in (r.get("labels") or [])],
                "size_bytes": int(r.get("sizeWhenDone") or 0),
            }
            for r in rows
            if r.get("hashString")
        ]

    async def _all_completed() -> list[dict[str, Any]]:
        """所有可用下载器里的已完成种（附下载器 id / 名称）。"""
        out: list[dict[str, Any]] = []
        for dl in await _dl_rows():
            if not dl.get("usable"):
                continue
            try:
                for item in await _completed_rows(dl):
                    out.append({
                        **item,
                        "downloader_id": dl["id"],
                        "downloader_name": dl.get("name"),
                        "path_mappings": dl.get("path_mappings") or [],
                    })
            except (TransferError, HTTPException) as exc:
                msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
                plog("warning", f"读取下载器「{dl.get('name')}」已完成种失败：{msg}")
        return out

    async def _all_hashes_all() -> set[str]:
        """所有下载器里的全部 info_hash（判重用）。"""
        hashes: set[str] = set()
        for dl in await _dl_rows():
            if not dl.get("usable"):
                continue
            try:
                async with _client_for(dl) as client:
                    hashes |= await client.all_hashes()
            except (TransferError, HTTPException):
                continue
        return hashes

    async def source_client(row: dict[str, Any]) -> QbittorrentWebUI | None:
        """源下载器客户端：qB 登录（磁盘缺失兜底导出用）；TR 源不直连（只读磁盘），返回 None。"""
        if row.get("client_type") == "qbittorrent":
            return await QbittorrentWebUI.login(
                _transport(), str(row.get("url") or ""), str(row.get("username") or ""), _password("源", ctx.config.from_password)
            )
        return None

    async def target_client(row: dict[str, Any]) -> QbittorrentWebUI | TransmissionRPC:
        url = str(row.get("url") or "")
        username = str(row.get("username") or "")
        password = _password("目标", ctx.config.to_password)
        if row.get("client_type") == "qbittorrent":
            return await QbittorrentWebUI.login(_transport(), url, username, password)
        return await TransmissionRPC.connect(_transport(), url, username, password)

    async def read_torrent_from_disk(info_hash: str) -> bytes | None:
        """按 <hash>.torrent 在配置的源种子目录里找，经宿主文件服务读出字节。"""
        for candidate in torrent_file_candidates(info_hash, split_lines(ctx.config.source_torrent_dirs)):
            try:
                if not await files.exists(candidate):
                    continue
                f = await files.open(candidate, "rb")
                with f:
                    return await asyncio.to_thread(f.read)
            except PermissionError:
                logger.warning("转种：源种子目录 %s 未在插件路径授权里，跳过", candidate.rsplit("/", 1)[0])
            except OSError as exc:
                logger.warning("转种：读种子文件失败 %s：%s", candidate, exc)
        return None

    # ------------------------------------------------------------------
    # 引擎一：转种（本地搬运）
    # ------------------------------------------------------------------

    async def transfer_pass(dry_run: bool = False) -> dict[str, Any]:
        """一轮完整转种：扫描 → 过滤 → 判重 → 取种 → 加种 → 编排校验 → 删源。"""
        started = time.monotonic()
        cfg = ctx.config
        from_row = await resolve_downloader(cfg.from_downloader, "源")
        to_row = await resolve_downloader(cfg.to_downloader, "目标")
        assert from_row is not None and to_row is not None
        if from_row.get("id") == to_row.get("id"):
            raise HTTPException(status_code=400, detail="源下载器和目标下载器不能是同一台")
        to_qb = to_row.get("client_type") == "qbittorrent"

        dirs = split_lines(cfg.source_torrent_dirs)
        src_qb = from_row.get("client_type") == "qbittorrent"
        src = await source_client(from_row) if (src_qb and dirs) or (src_qb and not dirs) else None
        # 磁盘目录没配且源是 qB ≥ 4.5 时允许纯 WebUI 模式；TR 源必须有磁盘目录
        if not dirs and not src_qb:
            raise HTTPException(status_code=400, detail="Transmission 做源必须配置「源种子文件目录」（TR 的 torrents 目录）")
        if src is not None and dirs and version_tuple(await src.version()) < QB_EXPORT_MIN_VERSION and True:
            pass  # 老版 qB：只能靠磁盘文件，export 兜底不可用——不报错，取不到的记失败

        # 路径换算前缀对：优先用内核里两台下载器已配的路径映射
        pairs: list[tuple[str, str]] = []
        for m in (to_row.get("path_mappings") or []):
            local, remote = str(m.get("local") or ""), str(m.get("remote") or "")
            if local and remote:
                pairs.append((local, remote))

        exclude_tags = set(split_tags(cfg.exclude_tags))
        add_tags = split_tags(cfg.add_tags)
        min_bytes = max(cfg.min_size_gb, 0.0) * (1 << 30)

        # 1) 候选：直接问源下载器要已完成的种
        #    注意：内核的 dl.tasks 只显示未完成任务与入库收尾中的任务，
        #    已完成的外部种会被它隐藏——拿它当来源会永远扫不到东西。
        try:
            rows = await _completed_rows(from_row)
        except TransferError as exc:
            raise HTTPException(status_code=502, detail=f"读取源下载器已完成种失败：{exc}") from exc
        candidates = []
        excluded_tags = missing_data = 0
        for t in rows:
            info_hash = str(t["hash"]).lower()
            if not info_hash:
                continue
            if exclude_tags and exclude_tags.intersection(t.get("tags") or []):
                excluded_tags += 1
                continue
            if min_bytes and (t.get("size_bytes") or 0) < min_bytes:
                continue
            if await _data_missing(t):
                missing_data += 1
                # 限频：同一颗种 24 小时内只提示一次，避免每 6 小时刷一遍
                miss_key = f"missblock:{info_hash}"
                try:
                    last_miss = int(await store.get(miss_key) or 0)
                except Exception:
                    last_miss = 0
                if time.time() - last_miss > 86400:
                    try:
                        await store.set(miss_key, int(time.time()))
                    except Exception:
                        pass
                    plog("warning", f"转种跳过「{t.get('name')}」：源数据不在原位（可能已被整理或进回收站，24 小时内不再提示）")
                continue
            candidates.append({
                "hash": info_hash,
                "name": str(t.get("name") or info_hash),
                "save_path": str(t.get("save_path") or ""),
                "tags": t.get("tags") or [],
                "size_gb": round((t.get("size_bytes") or 0) / (1 << 30), 2),
            })
        scanned = len(candidates)
        if not candidates:
            report = _report(started, dry_run, kind="transfer", scanned=0,
                             note="源下载器里没有已完成的种子")
            await store.set("last_transfer_report", report)
            return report

        # 2) 判重：目标里已有同 hash
        target = await target_client(to_row)
        try:
            if to_qb:
                existing = await target.existing_hashes([c["hash"] for c in candidates])  # type: ignore[union-attr]
            else:
                existing = await target.all_hashes()  # type: ignore[union-attr]

            # 3) 逐种取种 → 加种
            planned: list[dict[str, Any]] = [
                c for c in candidates if c["hash"] not in existing
            ]
            duplicates = [c for c in candidates if c["hash"] in existing]

            if dry_run:
                return _report(
                    started, dry_run, kind="transfer",
                    scanned=scanned, exists=len(duplicates), planned=len(planned),
                    plan=[{**c, "target_path": convert_save_path(c.get("save_path") or "", pairs)} for c in planned[:_REPORT_DETAIL_LIMIT]],
                    note="以上为计划，未做任何改动" if planned else "没有需要转移的种子",
                )

            transferred: list[dict[str, Any]] = []
            failed: list[dict[str, str]] = []
            recheck_hashes: list[str] = []
            tagged = 0
            for c in planned:
                try:
                    metainfo = await read_torrent_from_disk(c["hash"])
                    if metainfo is None and src is not None:
                        metainfo = await src.export(c["hash"])
                    if metainfo is None:
                        failed.append({"hash": c["hash"], "name": c["name"], "error": "磁盘与 WebUI 都取不到种子文件（检查源种子文件目录挂载）"})
                        continue
                    if needs_trackers(metainfo) and src is not None:
                        metainfo = patch_announce(metainfo, qb_tracker_groups(await src.trackers(c["hash"])))
                    # MP 风格站点标签：按种子里的 tracker 域名识别站点，补一条「站点/名字」
                    item_tags = list(add_tags)
                    if cfg.transfer_tag_source:
                        announce = _announce_urls(metainfo)
                        site_tag = site_tag_from_trackers(announce)
                        if site_tag and site_tag not in item_tags:
                            item_tags.append(site_tag)
                            tagged += 1
                    # 目标路径：源种的保存目录经路径映射换算成目标下载器视角；
                    # 两台视角一致（映射为空）时原样使用
                    save_dir = convert_save_path(c.get("save_path") or "", pairs)
                    if to_qb:
                        paused = not (cfg.skip_verify and cfg.autostart)
                        await target.add(metainfo, save_dir, paused=paused, skip_checking=cfg.skip_verify, tags=item_tags)  # type: ignore[union-attr]
                        if not cfg.skip_verify:
                            recheck_hashes.append(c["hash"])
                    else:
                        await target.add(metainfo, save_dir, paused=not cfg.autostart, labels=item_tags)  # type: ignore[union-attr]
                    transferred.append(c)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- 单个种失败不影响其余
                    failed.append({"hash": c["hash"], "name": c["name"], "error": str(exc)})

            if recheck_hashes:
                try:
                    await target.recheck(recheck_hashes)  # type: ignore[union-attr]
                except TransferError as exc:
                    failed.append({"hash": "", "name": f"批量（{len(recheck_hashes)} 个已添加）", "error": f"触发校验失败：{exc}"})
            if pending_watch := [c["hash"] for c in transferred if to_qb and cfg.autostart and not cfg.skip_verify]:
                current = await _read_pending()
                await _write_pending(current + pending_watch)
            if cfg.verify_after_transfer and transferred:
                await _verify_enqueue(
                    [t["hash"] for t in transferred],
                    {t["hash"]: str(t.get("name") or "") for t in transferred},
                )

            # 4) 删源（走内核通道：过 H&R 保护、订阅状态正确联动；只删任务不删文件）
            deleted_source = deleted_duplicate = 0
            managed_all = set() if cfg.delete_source_managed else _managed_hashes(
                [d["hash"] for d in duplicates] + [t["hash"] for t in transferred], candidates,
            )
            if managed_all:
                plog("info", f"删源跳过 {len(managed_all)} 个 MovieClaw 投递的种（避免订阅重新下载）")
            if cfg.delete_duplicate and duplicates:
                keep = [d["hash"] for d in duplicates if d["hash"] not in managed_all]
                deleted_duplicate = await _delete_via_kernel(from_row["id"], keep, failed) if keep else 0
            if cfg.delete_source and transferred:
                keep = [t["hash"] for t in transferred if t["hash"] not in managed_all]
                deleted_source = await _delete_via_kernel(from_row["id"], keep, failed) if keep else 0

            report = _report(
                started, dry_run, kind="transfer",
                scanned=scanned, exists=len(duplicates), transferred=len(transferred),
                planned=len(planned), skipped_missing_data=missing_data or None,
                skipped_tags=excluded_tags or None,
                failed=len(failed), deleted_source=deleted_source, deleted_duplicate=deleted_duplicate,
                transferred_list=[t["name"] for t in transferred[:_REPORT_DETAIL_LIMIT]] or None,
                failures=failed[:_REPORT_DETAIL_LIMIT] or None,
            )
            await store.set("last_transfer_report", report)
            return report
        finally:
            await target.close()  # type: ignore[attr-defined]
            if src is not None:
                await src.close()

    def _managed_hashes(hashes: list[str], items: list[dict[str, Any]]) -> set[str]:
        """这批 hash 里属于 MovieClaw 投递的（订阅 / 手动），删源默认跳过它们。"""
        managed: set[str] = set()
        for t in items:
            h = str(t.get("hash") or "").lower()
            if not h:
                continue
            if set(_MC_OWNED_LABELS).intersection(t.get("tags") or []):
                managed.add(h)
        return managed.intersection({h.lower() for h in hashes})

    async def _managed_delete_safe(downloader_id: int, info_hash: str) -> tuple[bool, str]:
        """内核预演：删掉这个主种会不会牵动订阅重下？→（可安全删, 说明）。

        用 ``dl.torrent.delete`` 的 dry_run：宿主会算出这个任务还牵动着哪些季集、
        会取消哪些在跑的尝试。牵动任何一项就保留——宁可留个空壳，也不能让订阅
        把已经入库的集重下一遍。
        """
        try:
            plan = await ops.call(
                "dl.torrent.delete",
                {"downloader_id": downloader_id, "info_hash": info_hash, "dry_run": True},
            )
        except asyncio.CancelledError:
            raise
        except OpsError as exc:
            return False, f"内核预演失败（{exc.message}），先保留"
        requeued = plan.get("requeued_units") or []
        cancelled = plan.get("cancelled_attempts") or 0
        if requeued:
            return False, f"删了会让订阅重下 {len(requeued)} 个季集，保留"
        if cancelled:
            return False, f"删了会取消 {cancelled} 个在跑的下载尝试，保留"
        return True, "对应季集已入库，删任务不会触发重下"

    async def _delete_via_kernel(downloader_id: int, hashes: list[str], failed: list[dict[str, str]]) -> int:
        """内核删种通道：dl.torrent.delete 逐个删（不删文件）；订阅/H&R 保护由内核把关。"""
        ok = 0
        for h in hashes:
            try:
                await ops.call("dl.torrent.delete", {"downloader_id": downloader_id, "info_hash": h})
                ok += 1
            except OpsError as exc:
                failed.append({"hash": h, "name": "", "error": f"删源被拒（{exc.message}）"})
        return ok

    # ------------------------------------------------------------------
    # 删片联动：媒体库里删了片 / 清空回收站 → 对应的下载任务立刻停
    #
    # 事件只带媒体库的事实（删了哪些文件），种子归下载领域：用 dl.file-sources.list
    # 按文件 id 反查（文件已经删了也能查）。三条安全线见 link_delete_verdict；
    # 主种（MovieClaw 投递）再过一遍内核预演——会牵动订阅重下的一律保留。
    # ------------------------------------------------------------------

    async def _link_delete_handle(payload: Any, *, reason: str) -> None:
        cfg = ctx.config
        if not cfg.link_delete_enabled:
            return
        file_ids = files_of_payload(payload)
        if not file_ids:
            return
        media = getattr(payload, "media", None)
        title = (getattr(media, "title", "") or "") if media is not None else ""
        label_title = f"《{title}》" if title else "未知条目"
        try:
            torrents = await ops.call(
                "dl.file-sources.list", {"file_ids": ",".join(str(i) for i in file_ids)},
            ) or []
        except asyncio.CancelledError:
            raise
        except OpsError as exc:
            plog("warning", f"删片停种{label_title}：按文件查种子来源失败（{exc.message}），守护下一轮会兼底")
            return
        removed: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for torrent in torrents:
            info_hash = str(torrent.get("info_hash") or "").lower()
            if not info_hash:
                continue
            short = f"{info_hash[:12]}…"
            why = link_delete_verdict(torrent)
            if why is None and torrent.get("owned_by_movieclaw"):
                safe, note = await _managed_delete_safe(int(torrent["downloader_id"]), info_hash)
                if not safe:
                    why = note
            if why is not None:
                kept.append({"hash": info_hash, "name": str(torrent.get("title") or short), "reason": why})
                continue
            try:
                await ops.call(
                    "dl.torrent.delete",
                    {
                        "downloader_id": int(torrent["downloader_id"]),
                        "info_hash": info_hash,
                        "delete_files": bool(cfg.link_delete_files),
                    },
                )
            except asyncio.CancelledError:
                raise
            except OpsError as exc:
                # 任务已经不在了（重复投递 / 早被守护清掉）按成功算
                if info_hash in await _all_hashes_all():
                    failed.append({"hash": info_hash, "error": exc.message})
                    continue
            removed.append({
                "hash": info_hash,
                "name": str(torrent.get("title") or short),
                "downloader": str(torrent.get("downloader_name") or f"#{torrent['downloader_id']}"),
            })
        report = {
            "at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "media": title or "未知条目",
            "reason": reason,
            "delete_files": bool(cfg.link_delete_files),
            "removed": len(removed),
            "items": removed[:20],
            "kept": kept[:8],
            "failed": failed[:8],
        }
        await store.set("last_link_delete", report)
        if removed:
            plog(
                "success",
                f"删片停种{label_title}：已移除 {len(removed)} 个下载任务（"
                f"{'连数据文件一起删' if cfg.link_delete_files else '只删任务、保留数据'}）",
            )
        for item in kept:
            plog("info", f"删片停种{label_title}：保留 {item['hash'][:12]}… —— {item['reason']}")
        for item in failed:
            plog("warning", f"删片停种{label_title}：删任务 {item['hash'][:12]}… 失败（{item['error']}）")

    async def on_library_file_gone(payload: Any) -> None:
        """可靠事件：文件从磁盘删除 / 回收站彻底清除。至少一次投递，按事件 id 去重。"""
        delivery = ctx.delivery
        if delivery is not None and delivery.origin.is_plugin(PLUGIN_ID):
            return
        event_id = delivery.event_id if delivery is not None else ""
        if event_id:
            seen = await store.get("link_seen", default=[])
            seen = list(seen) if isinstance(seen, list) else []
            if event_id in seen:
                return
            await store.set("link_seen", [event_id, *seen][:300])
        await _link_delete_handle(payload, reason=str(getattr(payload, "reason", "") or "removed"))

    # ------------------------------------------------------------------
    # 引擎二：IYUU 辅种（跨站补种）
    # ------------------------------------------------------------------

    async def _iyuu_request(
        client: httpx.AsyncClient, method: str, path: str, json_body: dict[str, Any] | None = None,
        *, refresh_ts: bool = False,
    ) -> dict[str, Any]:
        """调一次 IYUU 接口（业务数据在 data 段）；限流 / 超时退避重试。

        ``refresh_ts``：每次重试都刷新请求里的 timestamp——重试会等几十秒，
        沿用旧时间戳会被判「时间戳已过期」。
        """
        last_error = "IYUU 接口持续不可用"
        for attempt in range(_RETRY_ATTEMPTS):
            body = json_body
            if refresh_ts and isinstance(body, dict) and "timestamp" in body:
                body = {**body, "timestamp": int(time.time())}
            try:
                resp = await client.request(method, path, json=body)
            except httpx.HTTPError:
                last_error = "连接 IYUU 失败，请检查网络或代理设置"
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            try:
                body = resp.json()
            except Exception:
                raise HTTPException(status_code=502, detail=f"IYUU 接口返回异常（HTTP {resp.status_code}）：{resp.text[:200]}")
            if not isinstance(body, dict):
                raise HTTPException(status_code=502, detail="IYUU 接口返回不是约定结构")
            if body.get("code") == 0:
                data = body.get("data")
                return data if isinstance(data, dict) else {}
            code, msg = body.get("code"), str(body.get("msg") or "")
            last_error = f"IYUU 接口报错（code={code}）：{msg}"
            if "频率过快" in msg or "限流" in msg or "时间戳" in msg:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise HTTPException(status_code=502, detail=last_error)
        raise HTTPException(status_code=502, detail=last_error)

    async def _http() -> httpx.AsyncClient:
        token = ctx.config.iyuu_token.get_secret_value() if ctx.config.iyuu_token else ""
        return httpx.AsyncClient(
            transport=_transport(), base_url=_IYUU_API_BASE,
            headers={"token": token}, timeout=30.0,
        )

    async def resolve_via_site_page(site_id: str, torrent_id: Any) -> str | None:
        """借宿主已认证会话打开站点详情页，解析出真实取种链接。

        IYUU 的模板链接过期/缺 sign 时用它兜底：宿主会话自带登录态，
        解析出的 download.php?id=..&passkey=..&sign=.. 可以直接取种。
        """
        tid = str(torrent_id or "").strip()
        if not tid or not site_id:
            return None
        for path in (f"details.php?id={tid}", f"detail/{tid}"):
            try:
                res = await ops.call(
                    "site.proxy.request",
                    {"site_id": site_id, "method": "GET", "path": path, "base": "web"},
                    timeout=25,
                )
            except Exception as exc:  # noqa: BLE001 -- 站点代理失败就换下一条路
                plog("info", f"辅种详情页解析失败（{site_id}）：{getattr(exc, 'message', None) or exc}")
                return None
            text = str((res or {}).get("text") or "")
            if not text:
                return None
            if "没有该ID" in text or "not found" in text.lower() or "错误" in text[:2000]:
                return None
            href = extract_download_href(text)
            if href:
                return href
        return None

    async def _site_map() -> dict[str, dict[str, Any]]:
        """sid → 本机站点（site_id + is_https + auth_type）；slug 直配 → 域名兜底，结果缓存。"""
        cached = await store.get("sid_map")
        if isinstance(cached, dict) and cached and all("auth_type" in v for v in cached.values()):
            return cached
        async with await _http() as client:
            sites = parse_sites(await _iyuu_request(client, "GET", "/reseed/sites/index"))

        by_slug = {slug_of(s): s for s in sites.values()}
        by_domain = {domain_of(str(s.get("base_url") or "")): s for s in sites.values()}
        mapping: dict[str, dict[str, Any]] = {}
        configured = await ops.call("site.list")
        for site in configured:
            site_id = str(site.get("site_id") or "")
            if not site.get("usable"):
                continue
            hit = by_slug.get(site_id)
            if hit is None:
                try:
                    detail = await ops.call("site.show", {"site_id": site_id})
                except OpsError:
                    continue
                hit = by_domain.get(domain_of(str((detail.get("profile") or {}).get("base_url") or "")))
            if hit is not None and hit.get("id") is not None:
                mapping[str(hit["id"])] = {
                    "site_id": site_id,
                    "slug": str(hit.get("site") or ""),
                    "is_https": hit.get("is_https", 1),
                    "auth_type": str(site.get("auth_type") or ""),
                }
        await store.set("sid_map", mapping)
        return mapping

    async def _match(hashes: list[str], sid_list: list[str]) -> dict[str, list[dict[str, Any]]]:
        """分批调 IYUU 匹配；sid_sha1 先报后用（报「站点哈希值」错时重报再试）。"""
        matches: dict[str, list[dict[str, Any]]] = {}
        async with await _http() as client:
            report_sids = sorted({s for s in sid_list if s}) or []
            if not report_sids:
                catalog = parse_sites(await _iyuu_request(client, "GET", "/reseed/sites/index"))
                report_sids = sorted(catalog)
                if not report_sids:
                    raise HTTPException(status_code=502, detail="IYUU 站点目录为空，无法匹配")
                await asyncio.sleep(_CALL_GAP_SECONDS)
            report = await _iyuu_request(client, "POST", "/reseed/sites/reportExisting", {"sid_list": report_sids})
            sid_sha1 = str(report.get("sid_sha1") or "")
            if not sid_sha1:
                raise HTTPException(status_code=502, detail="IYUU 未返回站点哈希值（sid_sha1）")
            await asyncio.sleep(_CALL_GAP_SECONDS)
            for start in range(0, len(hashes), _MATCH_CHUNK):
                chunk = hashes[start : start + _MATCH_CHUNK]
                json_str = compact_hash_json(chunk)
                payload = {
                    "hash": json_str, "sha1": index_sha1(json_str), "sid_sha1": sid_sha1,
                    "timestamp": int(time.time()), "version": _IYUU_VERSION,
                }
                try:
                    result = await _iyuu_request(
                        client, "POST", "/reseed/index/index", payload, refresh_ts=True,
                    )
                except HTTPException as exc:
                    if "站点哈希值" not in str(exc.detail):
                        raise
                    await asyncio.sleep(_CALL_GAP_SECONDS)
                    report = await _iyuu_request(client, "POST", "/reseed/sites/reportExisting", {"sid_list": report_sids})
                    await asyncio.sleep(_CALL_GAP_SECONDS)
                    payload["sid_sha1"] = str(report.get("sid_sha1") or "")
                    payload["timestamp"] = int(time.time())
                    result = await _iyuu_request(
                        client, "POST", "/reseed/index/index", payload, refresh_ts=True,
                    )
                matches.update(parse_matches(result))
        return matches

    async def reseed_pass(dry_run: bool = False) -> dict[str, Any]:
        """一轮完整辅种：扫描 → 成功缓存过滤 → IYUU 匹配 → 站点映射 → 判重 → 提交。"""
        started = time.monotonic()
        cfg = ctx.config
        token = cfg.iyuu_token.get_secret_value() if cfg.iyuu_token else ""
        if not token:
            raise HTTPException(status_code=400, detail="还没填 IYUU 令牌：到插件设置里填好再运行")

        completed = await _all_completed()
        all_hashes = await _all_hashes_all()
        scanned = len({c["hash"] for c in completed})
        ok_cache = {k.removeprefix("ok:") for k in (await store.items()) if k.startswith("ok:")}
        pending_hashes = sorted({
            str(t["hash"]).lower() for t in completed
            if str(t["hash"]).lower() not in ok_cache
        })
        if not pending_hashes:
            report = _report(started, dry_run, kind="reseed", scanned=scanned, note="下载器里没有可辅种的已完成种子")
            await store.set("last_reseed_report", report)
            return report

        sid_filter = sorted({str(s).strip() for s in (cfg.sid_list or []) if str(s).strip()})
        matches = await _match(pending_hashes, sid_filter)

        # 台账回填：成功缓存里还没有台账记录的（历史辅种），按 IYUU 匹配结果补录
        # 一笔；补录不投递、不重复提交
        ledger_keys = {str(k).removeprefix("re:") for k in (await store.items()) if str(k).startswith("re:")}
        completed_task = {str(t["hash"]).lower(): t for t in completed}

        site_map = await _site_map()
        async with await _http() as client:
            sites = parse_sites(await _iyuu_request(client, "GET", "/reseed/sites/index"))

        # 用本机种子的 tracker 学 passkey，按站点域名/别名入库（只补空缺）
        try:
            learned = await learn_passkeys_from_torrents()
        except Exception:
            learned = {}
        cred_saved = 0
        for sid, site_rec in sites.items():
            local = site_map.get(str(sid))
            if not local or not learned:
                continue
            host = domain_of(str(site_rec.get("base_url") or ""))
            labels = host.split(".")
            candidates_site = [host]
            if len(labels) >= 2:
                candidates_site.append(".".join(labels[-2:]))
            candidates_site.append(slug_of(site_rec))
            candidates_site.append(str(site_rec.get("site") or ""))
            candidates_site.append(local["site_id"])
            passkey = next((learned[k] for k in candidates_site if k and learned.get(k)), None)
            if not passkey:
                continue
            existing = await creds_of(local["site_id"])
            if not existing.get("passkey"):
                await creds_save(local["site_id"], {"passkey": passkey}, "本机种子 tracker")
                cred_saved += 1
        if cred_saved:
            plog("info", f"辅种：从本机种子学到 {cred_saved} 个站点的 passkey")

        plan: list[dict[str, Any]] = []
        skipped = {"not_selected": 0, "no_local_site": 0, "exists": 0, "site_cooldown": 0}
        now_ts = int(time.time())
        cooling: set[str] = set()
        for key in await store.items():
            key_s = str(key)
            if not key_s.startswith("block:"):
                continue
            sid_cool = key_s.removeprefix("block:")
            try:
                raw = await store.get(key)
                if isinstance(raw, str) and raw.startswith("{"):
                    info = json.loads(raw)
                    ts, ttl = int(info.get("ts") or 0), int(info.get("ttl") or 86400)
                else:
                    ts, ttl = int(raw or 0), 86400
            except Exception:
                ts, ttl = 0, 86400
            if now_ts - ts < ttl:
                cooling.add(sid_cool)
        by_site: dict[str, dict[str, Any]] = {}
        hash_to_task: dict[str, dict[str, Any]] = {}
        for t in completed:
            hash_to_task.setdefault(str(t["hash"]).lower(), t)
        for info_hash, seeds in matches.items():
            for seed in seeds:
                sid = str(seed.get("sid"))
                if sid_filter and sid not in sid_filter:
                    skipped["not_selected"] += 1
                    continue
                local = site_map.get(sid)
                base_url = str(sites.get(sid, {}).get("base_url") or "")
                if not local or not base_url:
                    skipped["no_local_site"] += 1
                    continue
                if sid in cooling:
                    skipped["site_cooldown"] += 1
                    continue
                target_hash = str(seed.get("info_hash") or info_hash).lower()
                if target_hash in all_hashes:
                    skipped["exists"] += 1
                    entry = by_site.setdefault(sid, {"nickname": _SITE_NICKNAMES.get(sid, ""), "planned": 0, "already_seeding": 0})
                    entry["already_seeding"] += 1
                    # 历史辅种回填：已在做种但台账没有记录的，补录一笔（不投递）
                    if target_hash not in ledger_keys:
                        src_task = completed_task.get(str(info_hash).lower())
                        ledger_keys.add(target_hash)
                        await store.set(f"re:{target_hash}", json.dumps({
                            "source_hash": info_hash,
                            "source_name": str((src_task or {}).get("name") or info_hash),
                            "target_hash": target_hash,
                            "sid": sid,
                            "site_name": _SITE_NICKNAMES.get(sid, "") or local["site_id"],
                            "site_id": local["site_id"],
                            "downloader_id": (src_task or {}).get("downloader_id"),
                            "save_path": "",
                            "ts": int(time.time()),
                            "backfilled": True,
                        }))
                    continue
                site_nickname = _SITE_NICKNAMES.get(sid, "") or str(sites.get(sid, {}).get("nickname") or "") or local["site_id"]
                entry = by_site.setdefault(sid, {"nickname": site_nickname, "planned": 0, "already_seeding": 0})
                entry["planned"] += 1
                src_row = completed_task.get(str(info_hash).lower())
                plan.append({
                    "source_hash": info_hash,
                    "source_name": str((src_row or {}).get("name") or ""),
                    "target_hash": target_hash,
                    "sid": sid,
                    "site_name": site_nickname,
                    "site_id": local["site_id"],
                    "auth_type": str(local.get("auth_type") or ""),
                    "torrent_id": seed.get("torrent_id"),
                    "download_url": build_download_url(
                        base_url, seed.get("torrent_id"), local.get("is_https", 1),
                        api_style=str(local.get("auth_type") or "") == "apikey",
                        template=str(sites.get(sid, {}).get("download_page") or ""),
                    ),
                })

        # 模板坏站（取种 404 过）排到最后：搜索很慢，先用好站跑满提交上限
        broken_sites: set[str] = set()
        now_ts2 = time.time()
        for key in await store.items():
            key_s = str(key)
            if not key_s.startswith("tmplbad:"):
                continue
            try:
                ts = int(await store.get(key_s) or 0)
            except Exception:
                ts = 0
            if now_ts2 - ts < 86400:
                broken_sites.add(key_s.removeprefix("tmplbad:"))
        plan.sort(key=lambda i: 1 if i["site_id"] in broken_sites else 0)

        submitted = ok = exists = failed = 0
        failures: list[dict[str, str]] = []
        site_failures: dict[str, int] = {}      # 站点 → 连续取种失败次数
        blocked_sites: set[str] = set()         # 连续失败两次的站点本轮不再试
        submit_cap = max(int(cfg.reseed_max_submits or 0), 0)
        attempt = 0
        # 搜索真实链接很慢（每次要打站点搜索接口）：每轮给 8 次预算，
        # 用完就跳过高成本站点，保证模板可用的站点能跑满提交上限
        search_budget = 25
        filled_logged: set[str] = set()
        if not dry_run:
            for item in plan:
                if time.monotonic() - started > 600:
                    plog("warning", "辅种本轮已跑满 10 分钟，剩余条目留到下一轮")
                    skipped["time_budget"] = 0
                    break
                if submit_cap and attempt >= submit_cap:
                    skipped["submit_cap"] = len(plan) - attempt
                    plog("info", f"辅种本轮已提交 {attempt} 条（上限 {submit_cap}），剩余留到下一轮")
                    break
                args: dict[str, Any] = {
                    "site_id": item["site_id"],
                    "download_url": item["download_url"],
                }
                # 取种链接三级策略（从便宜到贵）：
                # ① 凭据库回填 IYUU 模板（学到的 passkey 直接拼活）——最快；
                # ② 回填不干净时，按发布名搜站点拿带登录态的真实链接；
                # ③ 最后才用剥离占位符的模板（个别站能过）
                site_creds = await creds_of(item["site_id"])
                filled = apply_credentials(args["download_url"], site_creds)
                # 模板坏站（曾取种 404/坏包）：优先走「按名搜索真实链接」
                template_broken = item["site_id"] in broken_sites
                need_search = template_broken or (not filled) or ("{" in filled)
                if need_search and template_broken and search_budget <= 0:
                    skipped["search_budget"] = skipped.get("search_budget", 0) + 1
                    continue
                if filled and not need_search:
                    if filled != args["download_url"] and item["site_id"] not in filled_logged:
                        filled_logged.add(item["site_id"])
                        plog("info", f"辅种模板链接已用凭据库回填（{item['site_id']}）")
                    args["download_url"] = filled
                elif (real_detail := await resolve_via_site_page(item["site_id"], item.get("torrent_id"))):
                    args["download_url"] = real_detail
                    plog("info", f"辅种改用详情页解析的真实链接（{item['site_id']}）")
                elif item.get("source_name"):
                    search_hits: list[dict[str, str]] = []

                    async def _sink(creds: dict[str, str]) -> None:
                        if creds:
                            search_hits.append(creds)

                    search_budget -= 1
                    real = await resolve_real_download_url(
                        ops.call, item["site_id"], item.get("torrent_id"), item["source_name"], plog, _sink,
                    )
                    if search_hits:
                        merged: dict[str, str] = {}
                        for h in search_hits:
                            merged.update(h)
                        await creds_save(item["site_id"], merged, "辅种搜索")
                    if real:
                        args["download_url"] = real
                    else:
                        args["download_url"] = strip_placeholders(
                            args["download_url"] if "{" in str(filled or "") else (filled or args["download_url"])
                        )
                elif filled:
                    args["download_url"] = filled if "{" not in filled else strip_placeholders(filled)
                # 辅种沿用原种保存目录（IYUU v2.18 口径）：原种在哪台下载器、
                # 什么目录，辅种就投到同一台的同名目录；拿不到再落兜底根目录
                src_task = hash_to_task.get(item["source_hash"])
                save_path = ""
                if src_task:
                    if src_task.get("downloader_id") is not None:
                        args["downloader_id"] = src_task["downloader_id"]
                    save_path = _to_movieclaw_path(str(src_task.get("save_path") or ""), src_task)
                if not save_path and cfg.reseed_save_root:
                    save_path = cfg.reseed_save_root
                if save_path:
                    args["save_path"] = save_path
                # 注意：dl.submit 不支持自定义 tags（内核固定写 movieclaw-manual），
                # 辅种标签在设置里保留说明用，不能塞进请求
                if item["sid"] in blocked_sites:
                    skipped["site_unavailable"] = skipped.get("site_unavailable", 0) + 1
                    continue
                attempt += 1
                try:
                    # 单条提交最多等 45 秒：站点取种慢就记失败、继续下一条，
                    # 不让一个慢站拖住整轮
                    result = await ops.call("dl.submit", args, timeout=25)
                    submitted += 1
                    site_failures.pop(item["sid"], None)
                    # 成功一次就说明模板能用：清掉「模板坏」标记
                    if item["site_id"] in broken_sites:
                        broken_sites.discard(item["site_id"])
                        try:
                            await store.delete(f"tmplbad:{item['site_id']}")
                        except Exception:
                            pass
                    if result.get("already_exists"):
                        exists += 1
                    else:
                        ok += 1
                    await store.set(f"ok:{item['target_hash']}", int(time.time()))
                    # 辅种台账：成功辅过的记一笔（源种、站点、目标 hash、下载器），
                    # 网页「辅种台账」按源种分组展示，逐条可删
                    await store.set(f"re:{item['target_hash']}", json.dumps({
                        "source_hash": item["source_hash"],
                        "source_name": str((src_task or {}).get("name") or item["source_hash"]),
                        "target_hash": item["target_hash"],
                        "sid": item["sid"],
                        "site_name": item.get("site_name") or item.get("site_id"),
                        "site_id": item["site_id"],
                        "downloader_id": (src_task or {}).get("downloader_id"),
                        "save_path": args.get("save_path") or "",
                        "ts": int(time.time()),
                    }))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- 单条失败（含超时）继续跑完剩下的
                    failed += 1
                    msg = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                    ttl = 0
                    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
                        ttl = 3600          # 站点响应慢：冷却 1 小时，别整轮都耗在它身上
                    elif any(k in msg for k in ("bencode", "404", "400", "取回种子失败")):
                        ttl = 6 * 3600      # 站点要求额外凭据 / 种子已删：冷却 6 小时
                        if item.get("auth_type") != "apikey" and site_failures.get(item["sid"], 0) >= 3:
                            try:
                                await store.set(f"tmplbad:{item['site_id']}", int(time.time()))
                            except Exception:
                                pass
                    if ttl:
                        site_failures[item["sid"]] = site_failures.get(item["sid"], 0) + 1
                        if item["sid"] not in blocked_sites:
                            blocked_sites.add(item["sid"])
                            try:
                                await store.set(
                                    f"block:{item['sid']}",
                                    json.dumps({"ts": int(time.time()), "ttl": ttl}),
                                )
                            except Exception:
                                pass
                            plog(
                                "warning",
                                f"站点「{item.get('site_name') or item['sid']}」取种失败，"
                                f"{ttl // 3600} 小时内不再尝试",
                            )
                    failures.append({
                        "hash": item["source_hash"],
                        "site": item.get("site_name") or item.get("site_id"),
                        "error": msg,
                    })
                    plog(
                        "warning",
                        f"辅种提交失败（{item.get('site_name') or item.get('site_id')}）：{msg}",
                    )
                await asyncio.sleep(_SUBMIT_INTERVAL_SECONDS)

        report = _report(
            started, dry_run, kind="reseed",
            scanned=scanned, matched=len(matches), planned=len(plan),
            submitted=submitted, ok=ok, exists=exists, failed=failed,
            by_site=by_site or None, failures=failures or None, skipped=skipped or None,
        )
        await store.set("last_reseed_report", report)
        return report

    def _to_movieclaw_path(path: str, item: dict[str, Any]) -> str:
        """下载器上报的路径 → movieclaw 视角（对已配路径映射做反向换算）。"""
        if not path:
            return ""
        src = _norm(path)
        best: tuple[str, str] | None = None
        for m in item.get("path_mappings") or []:
            local = _norm(str(m.get("local") or ""))
            remote = _norm(str(m.get("remote") or ""))
            if not local or not remote:
                continue
            if src == remote or src.startswith(remote + "/"):
                if best is None or len(remote) > len(best[0]):
                    best = (remote, local)
        if best is None:
            return path
        remote, local = best
        return local + src[len(remote):] if src != remote else local

    def _report(started: float, dry_run: bool, **counts: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "dry_run": dry_run,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        }
        for key, value in counts.items():
            if value is not None:
                data[key] = value
        return data

    # ------------------------------------------------------------------
    # 保活守护：智能判断哪些种已经没意义，自动清理（站点删种 / 数据丢失）
    # ------------------------------------------------------------------

    def _classify_tr(
        row: dict[str, Any], protected: set[str], sweep_dead: bool, sweep_no_data: bool,
    ) -> tuple[str | None, str]:
        """TR 全量行 →（处置, 说明）。处置：delete / warn / None。"""
        labels = [str(x) for x in (row.get("labels") or [])]
        reason, note = tr_torrent_verdict(
            error=int(row.get("error") or 0),
            error_string=str(row.get("errorString") or ""),
            tracker_stats=row.get("trackerStats") or [],
            labels=labels,
            protected_labels=protected,
        )
        if reason is None:
            return None, ""
        return ("delete" if (reason == "tracker_deleted" and sweep_dead) or (reason == "no_data" and sweep_no_data) else "warn"), note

    async def _classify_qb(
        target_qb: QbittorrentWebUI, protected: set[str], sweep_dead: bool,
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]], list[tuple[str, str]]]:
        """qB 全量任务 →（删除名单, 告警名单, 主种空壳名单），每项 (hash, name, 说明)。"""
        rows = await target_qb.completed()
        deletions: list[tuple[str, str, str]] = []
        warnings: list[tuple[str, str, str]] = []
        managed: list[tuple[str, str]] = []
        for row in rows:
            info_hash = str(row.get("hash") or "").lower()
            if not info_hash:
                continue
            labels = qb_tags(row)
            state = str(row.get("state") or "")
            if set(_MC_OWNED_LABELS).intersection(labels):
                # 主种里的空壳（数据文件不在了）：交给内核预演判定能不能清
                if not protected.intersection(labels) and state in ("missingFiles", "error"):
                    managed.append((info_hash, str(row.get("name") or info_hash)))
                continue
            if protected.intersection(labels):
                continue
            reason, note = qb_torrent_verdict(
                trackers=await target_qb.trackers(info_hash),
                state=state,
                protected_labels=protected,
                labels=labels,
            )
            if reason == "tracker_deleted":
                (deletions if sweep_dead else warnings).append((info_hash, str(row.get("name") or info_hash), note))
        return deletions, warnings, managed

    async def sweep_pass(dry_run: bool | None = None) -> dict[str, Any]:
        """一轮保活守护：全量扫描两台下载器，把已无意义的种清出去（默认只报告）。

        永不碰的：带保护标签的种、TR 里校验完好的 No data found（可能只是路径变了）。
        主种（movieclaw-*）里的空壳不直接删：先用内核删种预演判断——删了会不会让
        订阅重下已入库的季集；会重下的一律保留。
        永不删文件：只从下载器移除任务条目。
        """
        started = time.monotonic()
        cfg = ctx.config
        if dry_run is None:
            dry_run = cfg.sweep_dry_run
        protected = set(split_tags(cfg.sweep_protected_labels))
        deletions: dict[str, list[tuple[str, str, str]]] = {}
        warnings: dict[str, list[tuple[str, str, str]]] = {}
        managed_rows: list[tuple[int, str, str, str]] = []  # (下载器 id, hash, 名字, 本地错误)

        # TR：trackerStats 一次全量拿回，不用逐种问
        dl_rows = await _dl_rows()
        for dl in dl_rows:
            if not dl.get("usable"):
                continue
            name = f"#{dl['id']} {dl['name']}"
            try:
                async with _client_for(dl) as client:
                    if dl.get("client_type") == "transmission":
                        args = await client._rpc("torrent-get", {"fields": ["hashString", "name", "error", "errorString", "trackerStats", "labels"]})
                        d_list: list[tuple[str, str, str]] = []
                        w_list: list[tuple[str, str, str]] = []
                        for row in args.get("torrents") or []:
                            labels = [str(x) for x in (row.get("labels") or [])]
                            info_hash = str(row.get("hashString") or "").lower()
                            if set(_MC_OWNED_LABELS).intersection(labels):
                                # 主种里的死种（数据丢了 / 站点已删）：交给内核预演判定能不能清
                                if info_hash and not protected.intersection(labels):
                                    reason, note = tr_torrent_verdict(
                                        error=int(row.get("error") or 0),
                                        error_string=str(row.get("errorString") or ""),
                                        tracker_stats=row.get("trackerStats") or [],
                                        labels=labels,
                                        protected_labels=protected,
                                    )
                                    if reason in ("tracker_deleted", "no_data"):
                                        managed_rows.append((
                                            int(dl["id"]), info_hash, str(row.get("name") or info_hash), note,
                                        ))
                                continue
                            if protected.intersection(labels):
                                continue
                            action, note = _classify_tr(row, protected, cfg.sweep_dead, cfg.sweep_no_data)
                            if action == "delete":
                                d_list.append((info_hash, str(row.get("name") or ""), note))
                            elif action == "warn":
                                w_list.append((info_hash, str(row.get("name") or ""), note))
                        if d_list:
                            deletions[name] = d_list
                        if w_list:
                            warnings[name] = w_list
                    else:
                        d_list, w_list, m_list = await _classify_qb(client, protected, cfg.sweep_dead)
                        if d_list:
                            deletions[name] = d_list
                        if w_list:
                            warnings[name] = w_list
                        managed_rows.extend((int(dl["id"]), h, n, "数据文件缺失") for h, n in m_list)
            except (TransferError, HTTPException) as exc:
                msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
                warnings.setdefault(name, []).append(("", f"连接失败：{msg}", ""))

        # 主种空壳：内核预演判定能不能安全清（结果缓存 12 小时，别每轮都问）
        managed_hashes: set[str] = set()
        if managed_rows and cfg.sweep_managed_dead:
            audit = await store.get("managed_audit")
            audit = dict(audit) if isinstance(audit, dict) else {}
            changed = False
            for dl_id, info_hash, torrent_name, raw_note in managed_rows[:120]:
                key = f"{dl_id}:{info_hash}"
                cached = audit.get(key)
                if isinstance(cached, dict) and time.time() - int(cached.get("at") or 0) < 43200:
                    safe, verdict_note = bool(cached.get("safe")), str(cached.get("note") or "")
                else:
                    safe, verdict_note = await _managed_delete_safe(dl_id, info_hash)
                    audit[key] = {"safe": safe, "note": verdict_note, "at": int(time.time())}
                    changed = True
                    await asyncio.sleep(0.05)
                dl_name = next((f"#{d['id']} {d['name']}" for d in dl_rows if int(d['id']) == dl_id), f"#{dl_id}")
                if safe:
                    managed_hashes.add(info_hash)
                    deletions.setdefault(dl_name, []).append((info_hash, torrent_name, f"主种空壳：{raw_note}；{verdict_note}"))
                else:
                    warnings.setdefault(dl_name, []).append((info_hash, torrent_name, f"主种空壳：{raw_note}；{verdict_note}"))
            if changed:
                # 只留最近 3000 条，防止越积越大
                if len(audit) > 3000:
                    ordered = sorted(audit.items(), key=lambda kv: int(kv[1].get("at") or 0), reverse=True)[:3000]
                    audit = dict(ordered)
                await store.set("managed_audit", audit)

        total_delete = sum(len(v) for v in deletions.values())
        total_warn = sum(len(v) for v in warnings.values())
        if not dry_run and total_delete:
            for dl_name, items in deletions.items():
                dl_id = int(dl_name.split("#")[1].split(" ")[0])
                for info_hash, _name, _note in items:
                    if not info_hash:
                        continue
                    try:
                        await ops.call("dl.torrent.delete", {"downloader_id": dl_id, "info_hash": info_hash})
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 -- 单条删除失败继续
                        plog("warning", f"保活守护：删任务 {info_hash} 失败（{getattr(exc, 'message', None) or exc}）")
            plog("success", f"保活守护：已清理 {total_delete} 个无意义任务（不删文件）")

        if total_delete or total_warn:
            plog(
                "warning" if (total_delete and not dry_run) else "info",
                "保活守护扫描：可清 {} 告警 {}（{}）".format(
                    total_delete, total_warn, "已删除" if not dry_run else "只报告未动",
                ),
            )
        # 全量候选落盘：网页「待清理」要完整表格（报告里的 detail 只留 20 条）
        candidates = [
            {
                "downloader": dl_name,
                "hash": info_hash,
                "name": name,
                "reason": note,
                "kind": "delete",
                "managed": info_hash in managed_hashes,
            }
            for dl_name, items in deletions.items()
            for info_hash, name, note in items
        ] + [
            {
                "downloader": dl_name,
                "hash": info_hash,
                "name": name,
                "reason": note,
                "kind": "warn",
                "managed": info_hash in managed_hashes,
            }
            for dl_name, items in warnings.items()
            for info_hash, name, note in items
        ]
        try:
            payload = {
                "scanned_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                "dry_run": dry_run,
                "deletable": len(candidates) - sum(len(v) for v in warnings.values()),
                "warned": sum(len(v) for v in warnings.values()),
                "items": candidates,
            }
            f = await files.open(files.path("plugin", "sweep-candidates.json"), "wb")
            with f:
                await asyncio.to_thread(f.write, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (OSError, PermissionError) as exc:
            plog("warning", f"保活守护：候选清单写入失败（网页看不到完整列表）：{exc}")
        report = _report(
            started, dry_run, kind="sweep",
            scanned_deletable=total_delete, warned=total_warn,
            managed_dead=len(managed_hashes),
            detail={k: [{"name": n, "note": note} for _h, n, note in v[:_REPORT_DETAIL_LIMIT]] for k, v in {**deletions, **warnings}.items()} or None,
            note=("以上为扫描结果，未删除任何东西（设置里的「只报告不删除」开着）" if dry_run and total_delete else None),
        )
        await store.set("last_sweep_report", report)
        return report

    async def _announce(kind: str, report: dict[str, Any]) -> None:
        """一轮跑完：发插件事件（Webhook / 别的插件能订）+ 把健康状态抬回正常。"""
        if kind == "transfer":
            await publish_event(TRANSFER_COMPLETED, TransferEvent(
                planned=int(report.get("planned") or 0),
                transferred=int(report.get("transferred") or 0),
                failed=int(report.get("failed") or 0),
                deleted_source=int(report.get("deleted_source") or 0),
                dry_run=bool(report.get("dry_run")),
            ))
        elif kind == "reseed":
            await publish_event(RESEED_COMPLETED, ReseedEvent(
                planned=int(report.get("planned") or 0),
                ok=int(report.get("ok") or 0),
                exists=int(report.get("exists") or 0),
                failed=int(report.get("failed") or 0),
                dry_run=bool(report.get("dry_run")),
            ))
        else:
            await publish_event(SWEEP_COMPLETED, SweepEvent(
                deleted=int(report.get("scanned_deletable") or 0),
                warned=int(report.get("warned") or 0),
                dry_run=bool(report.get("dry_run")),
            ))
        await health_ok(str(kind))

    async def scheduled_sweep() -> None:
        if not ctx.config.sweep_enabled:
            return
        if _runs["sweep"]["running"]:
            plog("info", "保活守护定时运行跳过：手动执行还在进行")
            return
        try:
            report = await sweep_pass()
        except Exception as exc:  # noqa: BLE001 -- 单轮失败不影响下一轮
            plog("error", "保活守护运行失败：" + traceback.format_exc(limit=3))
            await health_bad("sweep", f"保活守护最近一轮失败（{type(exc).__name__}），详情见插件日志")
            return
        plog(
            "success" if not report.get("dry_run") else "info",
            "保活守护完成：可清 {} 告警 {}（{}）".format(
                report.get("scanned_deletable"), report.get("warned"),
                "已删" if not report.get("dry_run") else "只报告",
            ),
        )
        await _announce("sweep", report)


    # ------------------------------------------------------------------
    # 转种后置校验：加种只是开始，校验通过、进入做种才算成功
    #
    # 加种后目标下载器会自己校验；这里每轮盯着结果：
    #   · 100% 且无本地错误 → 成功
    #   · 报「数据不完整 / 种子无效」→ 失败，按设置把这个死任务清掉（不删文件）
    #   · 任务不见了 / 超时还没校验完 → 告警，不再等
    # ------------------------------------------------------------------

    _verify: dict[str, Any] = {"items": None, "loaded": False}
    _verify_warn_at = {"t": 0.0}

    async def _read_verify(force: bool = False) -> list[dict[str, Any]]:
        if _verify["loaded"] and not force:
            return list(_verify["items"] or [])
        try:
            raw = await store.get("verify_pending")
            _verify["items"] = [
                x for x in (raw or [])
                if isinstance(x, dict) and x.get("hash")
            ]
            _verify["loaded"] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            if _verify["items"] is None:
                _verify["items"] = []
        return list(_verify["items"] or [])

    async def _write_verify(items: list[dict[str, Any]]) -> None:
        _verify["items"] = list(items)
        _verify["loaded"] = True
        try:
            await store.set("verify_pending", list(items))
        except asyncio.CancelledError:
            raise
        except Exception:
            now = time.monotonic()
            if now - _verify_warn_at["t"] > 600:
                _verify_warn_at["t"] = now
                plog("warning", "转种校验队列暂时无法持久化（不影响当前运行，稍后自动重试）")

    async def _verify_enqueue(hashes: list[str], names: dict[str, str]) -> None:
        """新加种的 hash 入校验队列（已在队列里的不重复加）。"""
        if not hashes:
            return
        items = await _read_verify()
        seen = {str(x.get("hash") or "").lower() for x in items}
        now = int(time.time())
        for h in hashes:
            h = h.lower()
            if h and h not in seen:
                items.append({"hash": h, "name": names.get(h, ""), "at": now})
                seen.add(h)
        await _write_verify(items[-1000:])

    async def _verify_round() -> None:
        cfg = ctx.config
        if not cfg.verify_after_transfer:
            return
        items = await _read_verify()
        if not items:
            return
        to_row = await resolve_downloader(cfg.to_downloader, "目标")
        if to_row is None:
            return
        hashes = [str(x.get("hash") or "").lower() for x in items if x.get("hash")]
        target = await target_client(to_row)
        try:
            snap = await target.inspect(hashes)  # type: ignore[union-attr]
        finally:
            await target.close()  # type: ignore[attr-defined]

        now = int(time.time())
        keep: list[dict[str, Any]] = []
        outcomes: list[tuple[str, str, str, str]] = []  # (state, hash, name, note)
        cleaned = 0
        for item in items:
            h = str(item.get("hash") or "").lower()
            if not h:
                continue
            state, note = verify_verdict(
                snap.get(h), int(item.get("at") or 0), now, cfg.verify_timeout_minutes,
            )
            if state == "pending":
                keep.append(item)
                continue
            name = str(item.get("name") or h)
            outcomes.append((state, h, name, note))
            if state == "failed" and cfg.verify_fail_cleanup and verify_cleanup_allowed(note):
                try:
                    await ops.call(
                        "dl.torrent.delete",
                        {"downloader_id": to_row["id"], "info_hash": h},
                    )
                    cleaned += 1
                except asyncio.CancelledError:
                    raise
                except OpsError as exc:
                    plog("warning", f"转种校验：清理失败任务被拒（{exc.message}）")

        if len(keep) != len(items):
            await _write_verify(keep)
        if not outcomes:
            return

        summary = {
            "ok": sum(1 for o in outcomes if o[0] == "ok"),
            "failed": sum(1 for o in outcomes if o[0] == "failed"),
            "missing": sum(1 for o in outcomes if o[0] == "missing"),
            "timeout": sum(1 for o in outcomes if o[0] == "timeout"),
        }
        report = {
            "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            **summary,
            "cleaned": cleaned,
            "pending": len(keep),
            "items": [
                {"hash": h, "name": name, "result": st, "note": note}
                for st, h, name, note in outcomes[-_REPORT_DETAIL_LIMIT:]
            ],
        }
        await store.set("last_verify_report", report)
        if summary["ok"]:
            plog("success", f"转种校验通过 {summary['ok']} 个（已在目标下载器做种）")
        if summary["failed"]:
            plog(
                "warning",
                "转种校验失败 {} 个{}".format(
                    summary["failed"], f"，已清掉 {cleaned} 个空壳任务（未删文件）" if cleaned else "（未清理）",
                ),
            )
        if summary["missing"]:
            plog("warning", f"转种校验：{summary['missing']} 个加种后任务不在了")
        if summary["timeout"]:
            plog("warning", f"转种校验：{summary['timeout']} 个超时仍未校完（不再等）")

    # ------------------------------------------------------------------
    # watcher：校验完成自动开始做种（qB 目标 + 不跳校验时）
    #
    # 待做种队列以内存为准、落库只为重启恢复：watcher 每分钟要读一次队列，
    # 每次都走宿主 IPC 会在宿主繁忙/重启时超时刷警告，也没必要。
    # ------------------------------------------------------------------

    _pending: dict[str, Any] = {"items": None, "loaded": False}
    _watch_warn_at = {"t": 0.0}

    async def _read_pending(force_reload: bool = False) -> list[str]:
        """读待做种队列：内存命中直接返回；首次（或要求重载）才读宿主，失败不炸。"""
        if _pending["loaded"] and not force_reload:
            return list(_pending["items"] or [])
        try:
            _pending["items"] = [
                h for h in (await store.get("pending")) or [] if isinstance(h, str)
            ]
            _pending["loaded"] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            # 宿主繁忙 / 刚重启：先按空队列跑，下一轮再读；不刷错误日志
            if _pending["items"] is None:
                _pending["items"] = []
        return list(_pending["items"] or [])

    async def _write_pending(items: list[str]) -> None:
        _pending["items"] = list(items)
        _pending["loaded"] = True
        try:
            await store.set("pending", list(items))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 落库失败只影响重启恢复，内存仍正确；限频提示
            now = time.monotonic()
            if now - _watch_warn_at["t"] > 600:
                _watch_warn_at["t"] = now
                plog("warning", "待做种队列暂时无法持久化（稍后自动重试，不影响当前运行）")

    async def _resume_ready(pending: list[str]) -> None:
        to_row = await resolve_downloader(ctx.config.to_downloader, "目标")
        if to_row is None or to_row.get("client_type") != "qbittorrent":
            await _write_pending([])
            return
        target = await target_client(to_row)
        try:
            states = await target.states(pending)  # type: ignore[union-attr]
            ready = [h for h, state in states.items() if qb_seeding_paused(state)]
            if ready:
                await target.resume(ready)  # type: ignore[union-attr]
                plog("success", f"保种管家：{len(ready)} 个任务校验完成，已开始做种")
        except TransferError as exc:
            plog("warning", f"保种管家：恢复做种失败（下轮重试）：{exc}")
            return
        finally:
            await target.close()  # type: ignore[attr-defined]
        remaining = [h for h in pending if h not in ready and h in states]
        await _write_pending(remaining)

    async def _watcher() -> None:
        streak = 0
        while True:
            try:
                pending = await _read_pending()
                if pending:
                    await _resume_ready(pending)
                await _verify_round()
                if streak >= 3:
                    plog("success", "watcher 恢复：校验监视已正常工作")
                streak = 0
                await health_ok("watcher")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                streak += 1
                now = time.monotonic()
                if now - _watch_warn_at["t"] > 600:
                    _watch_warn_at["t"] = now
                    plog("warning", f"watcher 本轮跳过（{type(exc).__name__}，稍后自动重试）")
                if streak >= 3:
                    await health_bad("watcher", f"转种后校验监视连续 {streak} 轮出错（{type(exc).__name__}），新加的种子可能不会被自动开始做种")
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # 手动执行：后台跑（长任务不堵请求，页面轮询状态）
    # ------------------------------------------------------------------

    _KIND_LABEL = {"transfer": "转种", "reseed": "辅种", "sweep": "保活守护"}
    _runs: dict[str, dict[str, Any]] = {
        k: {"running": False, "dry_run": None, "started_at": None, "finished_at": None,
            "error": None, "summary": None}
        for k in _KIND_LABEL
    }

    async def _run_in_background(kind: str, dry_run: bool) -> None:
        state = _runs[kind]
        label = _KIND_LABEL[kind]
        state.update({
            "running": True, "dry_run": dry_run, "error": None, "summary": None,
            "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "finished_at": None,
        })
        plog("info", f"{label}开始手动执行（{'只出计划' if dry_run else '实际执行'}）")
        try:
            if kind == "transfer":
                report = await transfer_pass(dry_run=dry_run)
                summary = "扫 {} 计划 {} 转移 {} 失败 {}".format(
                    report.get("scanned", 0), report.get("planned", 0),
                    report.get("transferred", 0), report.get("failed", 0))
            elif kind == "reseed":
                report = await reseed_pass(dry_run=dry_run)
                summary = "扫 {} 匹配 {} 计划 {} 成功 {}".format(
                    report.get("scanned", 0), report.get("matched", 0),
                    report.get("planned", 0), report.get("ok", 0))
            else:
                report = await sweep_pass(dry_run=dry_run)
                summary = "可清 {} 告警 {}".format(
                    report.get("scanned_deletable", 0), report.get("warned", 0))
            state["report"] = report
            state["summary"] = summary
            plog("success", f"{label}手动执行完成：{summary}")
            await _announce(kind, report)
        except HTTPException as exc:
            state["error"] = str(exc.detail)
            plog("warning", f"{label}手动执行跳过：{exc.detail}")
        except Exception:
            state["error"] = "执行失败，详情见日志"
            plog("error", f"{label}手动执行失败：" + traceback.format_exc(limit=3))
            await health_bad(kind, f"{label}手动执行失败，详情见插件日志")
        finally:
            state["running"] = False
            state["finished_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


    async def scheduled_transfer() -> None:
        if not ctx.config.transfer_enabled:
            return
        if _runs["transfer"]["running"]:
            plog("info", "转种定时运行跳过：手动执行还在进行")
            return
        try:
            report = await transfer_pass()
        except HTTPException as exc:
            plog("warning", f"转种定时运行跳过：{exc.detail}")
            await health_bad("transfer", f"转种跑不起来：{exc.detail}")
            return
        except Exception as exc:  # noqa: BLE001
            plog("error", "转种定时运行失败：" + traceback.format_exc(limit=3))
            await health_bad("transfer", f"转种最近一轮失败（{type(exc).__name__}），详情见插件日志")
            return
        plog(
            "success" if (report.get("transferred") or 0) > 0 else "info",
            "转种定时运行完成：扫 {} 计划 {} 转移 {} 失败 {} 删源 {}".format(
                report.get("scanned", 0), report.get("planned", 0), report.get("transferred", 0),
                report.get("failed", 0), report.get("deleted_source", 0),
            ),
        )
        await _announce("transfer", report)

    async def scheduled_reseed() -> None:
        if not ctx.config.iyuu_token:
            return
        if _runs["reseed"]["running"]:
            plog("info", "辅种定时运行跳过：手动执行还在进行")
            return
        try:
            report = await reseed_pass()
        except HTTPException as exc:
            plog("warning", f"辅种定时运行跳过：{exc.detail}")
            await health_bad("reseed", f"辅种跑不起来：{exc.detail}")
            return
        except Exception as exc:  # noqa: BLE001
            plog("error", "辅种定时运行失败：" + traceback.format_exc(limit=3))
            await health_bad("reseed", f"辅种最近一轮失败（{type(exc).__name__}），详情见插件日志")
            return
        plog(
            "success" if (report.get("ok") or 0) > 0 else "info",
            "辅种定时运行完成：扫 {} 匹配 {} 计划 {} 提交 {}（成功 {} 已在做种 {} 失败 {}）".format(
                report.get("scanned", 0), report.get("matched", 0), report.get("planned", 0),
                report.get("submitted", 0), report.get("ok", 0), report.get("exists", 0),
                report.get("failed", 0),
            ),
        )
        await _announce("reseed", report)

    # ------------------------------------------------------------------
    # 事件驱动：下载完成 / 入库完成 → 去抖后立刻补跑一轮转种
    # 定时任务仍是兜底（事件丢了、插件重启了都不影响）；这里只是把「等最多 6 小时」
    # 变成「半分钟内」。
    # ------------------------------------------------------------------
    _kick: dict[str, Any] = {"at": 0.0, "task": None}

    async def _kick_transfer(reason: str) -> None:
        cfg = ctx.config
        if not cfg.transfer_enabled or _runs["transfer"]["running"]:
            return
        now = time.monotonic()
        if now - _kick["at"] < 120:          # 2 分钟内只踢一次，避免一批下载连踢
            return
        _kick["at"] = now
        plog("info", f"收到「{reason}」，立即补跑一轮转种")
        await scheduled_transfer()

    async def on_download_completed(payload: Any) -> None:
        await _kick_transfer("下载完成")

    async def on_ingest_completed(payload: Any) -> None:
        await _kick_transfer("入库完成")

    ctx.on(DOWNLOAD_COMPLETED, on_download_completed)
    ctx.on(LIBRARY_INGEST_COMPLETED, on_ingest_completed)
    # 删片联动：文件从磁盘删掉 / 回收站彻底清除 → 对应的下载任务立刻停（可靠事件，至少一次投递）
    ctx.on(LIBRARY_FILE_DELETED, on_library_file_gone, id="library-file-deleted", durable=True)
    ctx.on(LIBRARY_FILE_PURGED, on_library_file_gone, id="library-file-purged", durable=True)

    ctx.contribute(SCHEDULED_TASKS, "transfer", TaskDefinition(
        key=f"{PLUGIN_ID}.transfer", title="自动转种",
        description="把源下载器里已完成的种搬到目标下载器继续做种（磁盘种子文件 + WebUI 兜底）",
        handler=scheduled_transfer,
        default_trigger_type=TriggerType.INTERVAL, default_interval_seconds=6 * 3600,
        default_enabled=True,
    ))
    ctx.contribute(SCHEDULED_TASKS, "reseed", TaskDefinition(
        key=f"{PLUGIN_ID}.reseed", title="IYUU 自动辅种",
        description="拿已完成的种去 IYUU 匹配其他站点并自动辅种（成功缓存保证不重复）",
        handler=scheduled_reseed,
        default_trigger_type=TriggerType.INTERVAL, default_interval_seconds=24 * 3600,
        default_enabled=True,
    ))
    ctx.contribute(SCHEDULED_TASKS, "sweep", TaskDefinition(
        key=f"{PLUGIN_ID}.sweep", title="保活守护（智能清理）",
        description="检查两台下载器：站点已删种 / 数据丢失的种自动清理或告警（默认只报告，设置里可改）",
        handler=scheduled_sweep,
        default_trigger_type=TriggerType.INTERVAL, default_interval_seconds=12 * 3600,
        default_enabled=True,
    ))

    # ------------------------------------------------------------------
    # 接口（自动成为 mclaw 命令：mclaw plugins seed-hub …）
    # ------------------------------------------------------------------

    admin = APIRouter()

    @admin.post("/verify/check", operation_id=f"plugins.{PLUGIN_ID}.verify.check", summary="立即检查转种后置校验：不带参数＝跑一轮队列；带 hashes＝查看这些种在目标下载器里的校验状态；enqueue=true 则把它们加入监管队列")
    async def verify_check(hashes: str = "", enqueue: bool = False) -> dict:
        if hashes.strip():
            wanted = [h.strip().lower() for h in hashes.replace(",", " ").split() if h.strip()]
            to_row = await resolve_downloader(ctx.config.to_downloader, "目标")
            if to_row is None:
                raise HTTPException(status_code=400, detail="先在插件设置里配好目标下载器")
            target = await target_client(to_row)
            try:
                snap = await target.inspect(wanted)  # type: ignore[union-attr]
            finally:
                await target.close()  # type: ignore[attr-defined]
            now = int(time.time())
            if enqueue:
                await _verify_enqueue(wanted, {h: str((snap.get(h) or {}).get("name") or "") for h in wanted})
            return {
                "target": f"#{to_row['id']} {to_row['name']}",
                "enqueued": enqueue,
                "items": [
                    {"hash": h, "exists": h in snap,
                     "verdict": verify_verdict(snap.get(h), 0, now, 0)[0],
                     **(snap.get(h) or {})}
                    for h in wanted
                ],
            }
        await _verify_round()
        return {"pending": len(await _read_verify()), "last_report": await store.get("last_verify_report")}

    @admin.get("/sweep/candidates", operation_id=f"plugins.{PLUGIN_ID}.sweep.candidates", summary="保活守护最近一次扫描的完整候选清单（只读；网页「待清理」用）")
    async def sweep_candidates(limit: int = 5000) -> dict:
        path = files.path("plugin", "sweep-candidates.json")
        try:
            if not await files.exists(path):
                return {"scanned_at": None, "deletable": 0, "warned": 0, "items": [], "total": 0,
                        "hint": "还没有扫描过：点「重新扫描」或等守护定时跑一轮"}
            f = await files.open(path, "rb")
            with f:
                raw = await asyncio.to_thread(f.read)
            data = json.loads(raw.decode("utf-8"))
        except (OSError, PermissionError, ValueError):
            return {"scanned_at": None, "deletable": 0, "warned": 0, "items": [], "total": 0,
                    "hint": "候选清单读不出来，点「重新扫描」重生成"}
        items = data.get("items") or []
        cap = min(max(limit, 1), 10000)
        return {**{k: v for k, v in data.items() if k != "items"}, "items": items[:cap], "total": len(items),
                "truncated": len(items) > cap}

    @admin.get("/logs", operation_id=f"plugins.{PLUGIN_ID}.logs", summary="插件日志：只看本插件的关键事件（level=info/warning/error 过滤，limit 条数，all=true 连历史文件一起读）")
    async def logs(level: str = "info", limit: int = 100, all: bool = False) -> dict:
        if level not in ("info", "warning", "error"):
            raise HTTPException(status_code=400, detail="level 只能是 info / warning / error")
        rows = await log_ring.tail(level, min(max(limit, 1), 500), include_file=all)
        return {"count": len(rows), "logs": rows, "hint": "all=true 读历史文件（含重启前的）；level=warning 只看警告及以上"}

    @admin.post("/logs/clear", operation_id=f"plugins.{PLUGIN_ID}.logs.clear", summary="清空插件日志（内存环 + 历史文件）")
    async def logs_clear() -> dict:
        log_ring._items.clear()
        await log_ring.clear_file()
        plog("info", "日志已清空")
        return {"ok": True}

    @admin.get("/status", operation_id=f"plugins.{PLUGIN_ID}.status", summary="保种管家状态：配置自检、上次报告、待做种队列")
    async def status() -> dict:
        cfg = ctx.config
        result: dict[str, Any] = {
            "transfer": {
                "enabled": cfg.transfer_enabled,
                "from": cfg.from_downloader or "（未配置）",
                "to": cfg.to_downloader or "（未配置）",
                "source_torrent_dirs": split_lines(cfg.source_torrent_dirs),
                "skip_verify": cfg.skip_verify, "autostart": cfg.autostart,
                "delete_source": cfg.delete_source, "delete_duplicate": cfg.delete_duplicate,
                "add_tags": cfg.add_tags, "exclude_tags": cfg.exclude_tags,
                "min_size_gb": cfg.min_size_gb,
                "last_report": await store.get("last_transfer_report"),
            },
            "reseed": {
                "configured": bool(cfg.iyuu_token),
                "sid_list": cfg.sid_list, "reseed_save_root": cfg.reseed_save_root,
                "reseed_tags": split_tags(cfg.reseed_tags),
                "ok_cache_size": len([k for k in await store.items() if k.startswith("ok:")]),
                "last_report": await store.get("last_reseed_report"),
            },
            "sweep": {
                "enabled": cfg.sweep_enabled, "dead": cfg.sweep_dead, "no_data": cfg.sweep_no_data,
                "managed_dead": cfg.sweep_managed_dead,
                "dry_run": cfg.sweep_dry_run, "protected_labels": split_tags(cfg.sweep_protected_labels),
                "never_touch": "带保护标签的种永不清理；主种（movieclaw-*）里的空壳先用内核预演判断，会被订阅重下的一律保留；只删任务不删文件",
                "last_report": await store.get("last_sweep_report"),
            },
            "verify": {
                "enabled": cfg.verify_after_transfer,
                "fail_cleanup": cfg.verify_fail_cleanup,
                "timeout_minutes": cfg.verify_timeout_minutes,
                "pending": len(await _read_verify()),
                "last_report": await store.get("last_verify_report"),
            },
            "link_delete": {
                "enabled": cfg.link_delete_enabled,
                "delete_files": cfg.link_delete_files,
                "policy": "删片 / 清空回收站时按文件反查下载任务；还供着别的文件、有 H&R 风险、删了会让订阅重下的一律保留",
                "last_report": await store.get("last_link_delete"),
            },
            "runs": {
                k: {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in _runs.items()
            },
            "pending_watch": len(await _read_pending()),
            "hint": "三个定时任务在「设置 → 定时任务」调周期；POST /run?dry_run=false 立即执行（background=true 后台跑）",
        }
        for which, raw in (("source_ready", cfg.from_downloader), ("target_ready", cfg.to_downloader)):
            try:
                row = await resolve_downloader(raw, "源" if which == "source_ready" else "目标")
                result[which] = f"#{row['id']} {row['name']} [{row['client_type']}]" if row else "默认下载器"
            except HTTPException as exc:
                result[which] = f"未就绪：{exc.detail}"
        return result

    @admin.get("/transfer/scan", operation_id=f"plugins.{PLUGIN_ID}.transfer-scan", summary="转种扫描明细：源下载器已完成种逐个给出结论（待转移 / 目标已有 / 排除标签 / 数据不在原位 / 体积不足）")
    async def transfer_scan() -> dict:
        """只读盘点：源下载器里每个已完成种为什么转 / 为什么不转，一目了然。"""
        cfg = ctx.config
        from_row = await resolve_downloader(cfg.from_downloader, "源")
        to_row = await resolve_downloader(cfg.to_downloader, "目标")
        if from_row is None or to_row is None:
            raise HTTPException(status_code=400, detail="先在插件设置里把源 / 目标下载器配好")
        if from_row.get("id") == to_row.get("id"):
            raise HTTPException(status_code=400, detail="源下载器和目标下载器是同一台")
        to_qb = to_row.get("client_type") == "qbittorrent"
        exclude = set(split_tags(cfg.exclude_tags))
        min_bytes = max(cfg.min_size_gb, 0.0) * (1 << 30)
        try:
            rows = await _completed_rows(from_row)
        except TransferError as exc:
            raise HTTPException(status_code=502, detail=f"读取源下载器失败：{exc}")
        target = await target_client(to_row)
        try:
            hashes = [str(r.get("hash") or "").lower() for r in rows if r.get("hash")]
            existing = set()
            if hashes:
                existing = await (target.existing_hashes(hashes) if to_qb else target.all_hashes())  # type: ignore[union-attr]
        finally:
            await target.close()  # type: ignore[attr-defined]
        items: list[dict[str, Any]] = []
        counts = {"transfer": 0, "exists": 0, "excluded": 0, "missing": 0, "small": 0}
        for r in rows:
            h = str(r.get("hash") or "").lower()
            if not h:
                continue
            tags = r.get("tags") or []
            size_gb = round((r.get("size_bytes") or 0) / (1 << 30), 2)
            hit = exclude.intersection(tags)
            # 判定顺序与 transfer_pass 一致：排除标签 → 体积 → 已在目标 → 数据在不在；
            # 只有走到最后才 stat 文件，避免给已经判死的种白做检查
            if hit:
                verdict, reason = "excluded", "命中排除标签：" + "、".join(sorted(hit))
            elif min_bytes and (r.get("size_bytes") or 0) < min_bytes:
                verdict, reason = "small", f"体积 {size_gb} GB 低于阈值 {cfg.min_size_gb:g} GB"
            elif h in existing:
                verdict, reason = "exists", "目标下载器已有同种，无需转移"
            elif await _data_missing(r):
                verdict, reason = "missing", "源数据不在原位（可能已被整理或进回收站）"
            else:
                verdict, reason = "transfer", "满足条件，待转移"
            counts[verdict] += 1
            items.append({
                "hash": h, "name": str(r.get("name") or h), "size_gb": size_gb,
                "tags": tags, "verdict": verdict, "reason": reason,
            })
        return {
            "source": f"#{from_row['id']} {from_row['name']} [{from_row['client_type']}]",
            "target": f"#{to_row['id']} {to_row['name']} [{to_row['client_type']}]",
            "total": len(items), "counts": counts, "items": items,
        }

    @admin.post("/cooldown/clear", operation_id=f"plugins.{PLUGIN_ID}.cooldown-clear", summary="清除所有站点的取种冷却（被冷却的站点立即重试）")
    async def cooldown_clear() -> dict:
        cleared = 0
        for key in await store.items():
            key_s = str(key)
            if not key_s.startswith("block:"):
                continue
            try:
                await store.delete(key_s)
                cleared += 1
            except Exception:
                continue
        plog("info", f"已清除 {cleared} 个站点的取种冷却")
        return {"cleared": cleared}

    @admin.get("/credentials", operation_id=f"plugins.{PLUGIN_ID}.credentials", summary="站点凭据库：各站已学到的 passkey/sign（打码显示）与更新时间")
    async def credentials_list() -> dict:
        import time as _t
        out: list[dict[str, Any]] = []
        for key, raw in (await store.items()).items():
            key_s = str(key)
            if not key_s.startswith("cred:"):
                continue
            try:
                if isinstance(raw, dict):
                    info = raw
                elif isinstance(raw, str):
                    info = json.loads(raw)
                else:
                    info = {}
            except Exception:
                continue
            if not isinstance(info, dict):
                continue
            site_id = key_s.removeprefix("cred:")
            masked = {
                k: (v[:4] + "…" + v[-4:] if len(v) > 10 else "…")
                for k, v in info.items() if k != "_updated"
            }
            out.append({
                "site_id": site_id,
                "credentials": masked,
                "updated_at": info.get("_updated"),
                "updated_human": datetime.fromtimestamp(info.get("_updated") or 0, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M") if info.get("_updated") else "—",
            })
        out.sort(key=lambda x: x["site_id"])
        return {"count": len(out), "sites": out}

    @admin.post("/credentials/refresh", operation_id=f"plugins.{PLUGIN_ID}.credentials-refresh", summary="立即收割：浏览各配置站点最新种子页，学取 passkey/sign 入库")
    async def credentials_refresh() -> dict:
        try:
            rows = await ops.call("site.list")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读站点列表失败：{exc}")
        updated = 0
        checked = 0
        for site in rows or []:
            site_id = str(site.get("site_id") or "")
            if not site_id or not site.get("usable"):
                continue
            checked += 1
            before = await creds_of(site_id)
            await harvest_credentials(site_id)
            after = await creds_of(site_id)
            if after != before:
                updated += 1
        return {"checked": checked, "updated": updated}

    @admin.get("/reseed/ledger", operation_id=f"plugins.{PLUGIN_ID}.reseed-ledger", summary="辅种台账：成功辅过的种按源种分组（站点、时间、在做种状态）")
    async def reseed_ledger() -> dict:
        entries: list[dict[str, Any]] = []
        for key, raw in (await store.items()).items():
            key_s = str(key)
            if not key_s.startswith("re:"):
                continue
            try:
                info = json.loads(raw) if isinstance(raw, str) else None
            except Exception:
                info = None
            if not isinstance(info, dict):
                continue
            info["target_hash"] = key_s.removeprefix("re:")
            entries.append(info)
        if not entries:
            return {"groups": [], "total": 0, "alive": 0}
        alive_hashes = await _all_hashes_all()
        for e in entries:
            e["alive"] = str(e["target_hash"]).lower() in alive_hashes
        groups: dict[str, dict[str, Any]] = {}
        for e in entries:
            g = groups.setdefault(str(e.get("source_hash") or ""), {
                "source_hash": e.get("source_hash") or "",
                "source_name": str(e.get("source_name") or e.get("source_hash") or ""),
                "entries": [], "latest": 0,
            })
            g["entries"].append(e)
            g["latest"] = max(g["latest"], int(e.get("ts") or 0))
        out = sorted(groups.values(), key=lambda g: g["latest"], reverse=True)
        return {
            "groups": out,
            "total": len(entries),
            "alive": sum(1 for e in entries if e["alive"]),
        }

    @admin.post("/reseed/ledger/delete", operation_id=f"plugins.{PLUGIN_ID}.reseed-ledger-delete", summary="删除一条辅种：从下载器移除该辅种任务（不删文件），并从台账去掉；不会再自动加回")
    async def reseed_ledger_delete(target_hash: str = "") -> dict:
        target_hash = target_hash.strip().lower()
        if not target_hash:
            raise HTTPException(status_code=400, detail="缺 target_hash")
        raw = await store.get(f"re:{target_hash}")
        info: dict[str, Any] = {}
        if isinstance(raw, str) and raw.startswith("{"):
            try:
                info = json.loads(raw)
            except Exception:
                info = {}
        dl_id = info.get("downloader_id")
        removed_task = False
        if dl_id is not None:
            try:
                await ops.call("dl.torrent.delete", {"downloader_id": int(dl_id), "info_hash": target_hash})
                removed_task = True
            except asyncio.CancelledError:
                raise
            except OpsError as exc:
                # 任务已经不在下载器里时按成功处理（早被守护 / 手动清掉了）
                if target_hash in await _all_hashes_all():
                    raise HTTPException(status_code=502, detail=f"删除辅种任务失败：{exc.message}")
        elif target_hash in await _all_hashes_all():
            raise HTTPException(status_code=400, detail="台账里缺这台辅种的下载器信息，请到下载器里手动删除该任务后再试")
        await store.delete(f"re:{target_hash}")
        plog(
            "warning" if removed_task else "info",
            f"辅种台账：已移除辅种（{info.get('site_name') or '未知站点'}）{target_hash[:12]}…，"
            "只删任务不删文件，之后也不会再自动加回",
        )
        return {"ok": True, "removed_task": removed_task, "target_hash": target_hash}

    @admin.post("/run", operation_id=f"plugins.{PLUGIN_ID}.run", summary="手动跑一轮：kind=transfer/reseed/sweep；dry_run=false 实际执行；background=true 时后台跑并立即返回（页面用）")
    async def run(kind: str = "transfer", dry_run: bool = True, background: bool = False) -> dict:
        if kind not in _KIND_LABEL:
            raise HTTPException(status_code=400, detail="kind 只能是 transfer / reseed / sweep")
        if background:
            if _runs[kind]["running"]:
                raise HTTPException(status_code=409, detail=f"{_KIND_LABEL[kind]}上一轮还在运行，等它跑完")
            ctx.task(_run_in_background(kind, dry_run), name=f"{PLUGIN_ID}-run-{kind}")
            return {"started": True, "kind": kind, "dry_run": dry_run,
                    "message": f"{_KIND_LABEL[kind]}已开始后台执行"}
        if kind == "transfer":
            report = await transfer_pass(dry_run=dry_run)
        elif kind == "reseed":
            report = await reseed_pass(dry_run=dry_run)
        else:
            report = await sweep_pass(dry_run=dry_run)
        await _announce(kind, report)
        return report

    @admin.get("/sites", operation_id=f"plugins.{PLUGIN_ID}.sites", summary="IYUU 站点目录（免令牌）与本机映射情况")
    async def sites() -> dict:
        async with await _http() as client:
            result = await _iyuu_request(client, "GET", "/reseed/sites/index")
        site_map = await _site_map()
        rows = []
        for site in (result.get("sites") or []):
            sid = str(site.get("id") or "")
            local = site_map.get(sid)
            rows.append({
                "sid": sid, "site": site.get("site"), "nickname": site.get("nickname"),
                "local_site_id": local["site_id"] if local else None,
            })
        return {"count": result.get("count", len(rows)), "sites": rows}

    routes.mount(ctx, admin)
    ctx.task(_watcher(), name="seed-hub-watcher")

    missing = []
    if ctx.config.transfer_enabled and not (ctx.config.from_downloader and ctx.config.to_downloader):
        missing.append("转种（缺源 / 目标下载器）")
    if not ctx.config.iyuu_token:
        missing.append("辅种（缺 IYUU 令牌）")
    if missing:
        plog("info", "保种管家已加载：" + "、".join(missing) + " 待配置——到插件设置里填好即全自动")
    else:
        plog("success", "保种管家已加载：转种每 6 小时、辅种每 24 小时、守护每 12 小时自动运行")
