"""插件配置模型：设置页由 MovieClaw 根据这些字段自动生成。

枚举值直接用中文（主程序设置表单把枚举值当标签渲染）。

目标媒体库不在这里：设置表单只支持固定的候选值，列不出服务器上有哪些库，
所以改在插件页面里按库名勾选（存库 id）。0.1.x 存在设置里的 ``libraries``
（库名或数字 id）仍会读出来，首次运行时迁移成勾选结果。
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .aliases import GITHUB_URL as NAME_TABLE_GITHUB
from .aliases import JSDELIVR_URL as NAME_TABLE_JSDELIVR

JSDELIVR_BASE = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/"
GITHUB_BASE = "https://raw.githubusercontent.com/gfriends/gfriends/master/"


class Mirror(StrEnum):
    JSDELIVR = "jsDelivr 镜像"
    GITHUB = "GitHub 原站"
    CUSTOM = "自定义地址"


class ThumbMode(StrEnum):
    """NFO 里写什么头像地址。"""

    MIRROR = "直接写镜像地址"
    HOSTED = "插件下载并托管"


class Settings(BaseModel):
    # 保留 0.1.x 存下的 ``libraries`` 等旧字段（不进表单），迁移时要读
    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(
        default=True,
        title="启用插件",
        description="关闭后不再响应入库事件和定时任务；手动运行与撤销仍可用。",
    )
    dry_run: bool = Field(
        default=True,
        title="只预览不写入",
        description=(
            "默认打开：只算出每部片会补哪些头像、写进报告，不改 NFO、不触发刷新。"
            "关掉后，入库与定时任务也要等你在插件页面亲手点过一次「写入」才会自动写。"
            "目标媒体库在插件页面里勾选。"
        ),
    )
    mirror: Mirror = Field(
        default=Mirror.JSDELIVR,
        title="头像源",
        description="gfriends 仓库的访问地址。jsDelivr 国内通常可直连；GitHub 原站需要代理。",
    )
    custom_base_url: str = Field(
        default="",
        title="自定义源地址",
        description=(
            "头像源选「自定义地址」时使用："
            "指向 gfriends 仓库根目录（其下有 Filetree.json 与 Content/）。"
        ),
    )
    ai_fix: bool = Field(
        default=True,
        title="优先 AI 修复版",
        description="gfriends 的 AI-Fix 版是放大修复后的清晰图；关闭则用原图。",
    )
    thumb_mode: ThumbMode = Field(
        default=ThumbMode.MIRROR,
        title="头像地址写法",
        description=(
            "直接写镜像地址 = NFO 里写头像源的图片地址，"
            "MovieClaw 网页端经自带图片缓存代理显示，最省事；"
            "插件下载并托管 = 图片存到插件目录，NFO 写本插件的签名公开链接，"
            "需要「外部访问地址」是公网域名 + 标准端口"
            "（主程序的图片代理拒绝内网 IP 与非标准端口）。"
        ),
    )
    public_base_url: str = Field(
        default="",
        title="外部访问地址（托管模式）",
        description=(
            "托管模式下拼链接用，如 https://movieclaw.example.com；"
            "留空读「设置 → 应用 → 外部访问地址」。"
        ),
    )
    name_table: bool = Field(
        default=True,
        title="使用中文译名表",
        description=(
            "NFO 里是中文译名（如「凉森玲梦」）时，查 MDCx 的演员译名对照表换成日文原名再找头像。"
            "表不随插件打包：运行时从 jsDelivr（头像源选 GitHub 原站时走 GitHub）"
            "下载并缓存，约 0.9 MB；"
            "来自 MDCx 项目（GPL-3.0，另附非商用条款）。"
        ),
    )
    infer_from_title: bool = Field(
        default=True,
        title="从片名推断候选",
        description="演员名查不到时，用片名末尾的日文名找候选，只放进「待确认」列表，确认后才会写入。",
    )
    process_arrivals: bool = Field(
        default=True,
        title="新片入库时处理",
        description="目标库来了新片就排一次（定时任务的周期在「设置 → 定时任务」里改）。",
    )
    max_items_per_run: int = Field(
        default=200,
        ge=1,
        le=5000,
        title="每轮最多处理",
        description="一轮最多看多少部片；没跑完的下一轮从断点接着跑。",
    )
    request_interval_ms: int = Field(
        default=800,
        ge=0,
        le=60000,
        title="请求间隔（毫秒）",
        description="两次下载头像之间至少等待多久（托管模式）；也用于刷新元数据之间的间隔，避免压垮主程序。",
    )
    filetree_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        title="索引刷新间隔（小时）",
        description=(
            "gfriends 头像索引（Filetree.json）多久检查一次更新；没变化时只发一次条件请求。"
        ),
    )

    skip_junk_names: bool = Field(
        default=True,
        title="过滤刮削器写进来的描述性名字",
        description=(
            "像「京香 大学生モデル」「あいか 23歳 …」「あおいさん」这类不是艺名的串不拿去匹配，"
            "在插件页面单独列出（可以手动给它们设别名或上传头像）。"
        ),
    )
    ai_suggest_on_run: bool = Field(
        default=False,
        title="每轮运行后用 AI 推荐别名",
        description=(
            "开启后，每轮运行结束时让 MovieClaw 内置的 AI 助手给还没匹配上的演员推荐别名"
            "（每轮最多 5 位）。推荐的名字必须在 gfriends 里查得到才收下，"
            "而且只进「待确认」，不会自动写入。"
            "要在安装时批准「AI 助手（无工具）」权限，按模型用量计费。"
        ),
    )
    ai_daily_limit: int = Field(
        default=20,
        ge=0,
        le=200,
        title="AI 推荐每日上限（位演员）",
        description="每天最多让 AI 推荐多少位演员（手动点的和每轮自动的合计）；0 表示不用 AI。",
    )

    search_enabled: bool = Field(
        default=True,
        title="按番号搜索补头像",
        description=(
            "gfriends 里配不上的演员，用片子的番号去 av-wiki / javdb / DMM 查演员表认人、取头像。"
            "结果只进「待确认」。仅限个人自用、低频访问，请自行留意各站条款。"
        ),
    )
    search_avwiki: bool = Field(
        default=True,
        title="来源：av-wiki",
        description="查番号的演员表；只有一位且名字不同时视为别名，用正名再查 gfriends。",
    )
    search_javdb: bool = Field(
        default=True, title="来源：javdb", description="查番号的女演员与演员头像。"
    )
    search_dmm: bool = Field(
        default=True,
        title="来源：DMM",
        description=(
            "没配官方接口时按罗马字猜头像地址（可能认错人，标「猜测」）；"
            "配了 api_id 与 affiliate_id 时按番号查演员表。"
        ),
    )
    dmm_api_id: SecretStr = Field(
        default=SecretStr(""),
        title="DMM API ID",
        description="DMM 官方接口的 api_id（免费注册）；留空不用接口。保存后不回显。",
    )
    dmm_affiliate_id: SecretStr = Field(
        default=SecretStr(""),
        title="DMM Affiliate ID",
        description="DMM 官方接口的 affiliate_id（形如 xxx-990）；与 api_id 一起填才生效。",
    )
    search_per_run: int = Field(
        default=5,
        ge=1,
        le=30,
        title="每轮最多搜索（位演员）",
        description="定时任务 / 新片入库那一轮结束后，最多给几位没配上的演员搜索。",
    )
    search_interval_s: int = Field(
        default=5,
        ge=2,
        le=60,
        title="搜索请求间隔（秒）",
        description="同一个站点相邻两次请求至少隔多久。",
    )
    search_cooldown_days: int = Field(
        default=30,
        ge=1,
        le=365,
        title="没搜到后冷却（天）",
        description="某位演员搜索没结果后，自动轮次多少天内不再搜她（手动点「搜索头像」不受限）。",
    )
    search_auto_write: bool = Field(
        default=False,
        title="交叉验证通过自动写入",
        description=(
            "默认关。开启后，自动轮次里演员名与该番号演员表完全同名、且来源是 av-wiki / javdb / "
            "DMM 官方接口的结果直接写入（仍受只预览与写入解锁限制）；别名与猜测永远只进待确认。"
        ),
    )

    @field_validator("public_base_url", "custom_base_url")
    @classmethod
    def _valid_http_url(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("必须是 http:// 或 https:// 开头的完整地址")
        return value

    def source_base(self) -> str:
        """头像源仓库根地址，保证以 / 结尾。"""
        if self.mirror == Mirror.GITHUB:
            base = GITHUB_BASE
        elif self.mirror == Mirror.CUSTOM and self.custom_base_url:
            base = self.custom_base_url
        else:
            base = JSDELIVR_BASE
        return base if base.endswith("/") else base + "/"

    def name_table_url(self) -> str:
        return NAME_TABLE_GITHUB if self.mirror == Mirror.GITHUB else NAME_TABLE_JSDELIVR

    def legacy_library_refs(self) -> list[str]:
        """0.1.x 设置里的目标库（库名或数字 id）；没有为空列表。"""
        raw = (self.model_extra or {}).get("libraries")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(v).strip() for v in raw if str(v).strip()]
