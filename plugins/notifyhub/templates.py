"""MoviePilot-like Jinja2 context, backed by MovieClaw's actual data."""
import re
from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment

ENV = SandboxedEnvironment(undefined=ChainableUndefined, autoescape=False)

PREVIOUS_TITLE = "{{ event_icon }} {{ title_year }}{% if season_fmt %} {{ season_fmt }}{% endif %} {{ action }}"
PREVIOUS_TEXT = """{% if vote_average %}✨ 评分：{{ vote_average }}/10{% endif %}
{% if category %}📽 类型：{{ category }}{% endif %}
{% if username %}👾 用户：{{ username }}{% endif %}
{% if episode_fmt %}🎬 季集：{{ episode_fmt }}{% endif %}
{% if quality %}📺 规格：{{ quality }}{% endif %}
{% if resource %}📦 资源：{{ resource }}{% endif %}
{% if size %}💾 大小：{{ size }}{% endif %}
{% if created_at %}🕒 时间：{{ created_at }}{% endif %}
{% if device %}📱 设备：{{ device }}{% endif %}
{% if progress %}⏱ 进度：{{ progress }}{% endif %}
{% if actors %}🤵 参演：{{ actors }}{% endif %}
{% if overview %}
🍿 简介：
{{ overview }}{% endif %}
{% if __mediainfo__.detail_link %}🔗 链接：{{ __mediainfo__.detail_link }}{% endif %}"""
DEFAULT_TITLE = "{{ event_icon }} {{ action }}｜{{ title_year }}{% if season_fmt %} · {{ season_fmt }}{% endif %}"
DEFAULT_TEXT = """{% if media_label %}{{ media_label }}{% if category %} · {{ category }}{% endif %}{% if vote_average %}  ⭐ {{ vote_average }}/10{% endif %}{% elif category %}{{ category }}{% endif %}
{% if episode_fmt %}🎬 {{ episode_fmt }}{% endif %}
{% if quality or size %}📀 {% if quality %}{{ quality }}{% endif %}{% if quality and size %} · {% endif %}{% if size %}{{ size }}{% endif %}{% endif %}
{% if resource and not quality %}📦 {{ resource }}{% endif %}
{% if username %}👤 {{ username }}{% endif %}
{% if device %}📱 {{ device }}{% endif %}
{% if progress %}⏱ {{ progress }}{% endif %}
{% if actors %}主演：{{ actors }}{% endif %}
{% if overview %}🍿 {{ overview }}{% endif %}
{% if created_at %}🕒 {{ created_at }}{% endif %}"""

ALERT_TITLE = "⚠️ {{ title or server_name }}"
ALERT_TEXT = "{{ message }}"
EVENTS = {
    "drive.upload.completed": ("☁️", "网盘上传完成", "upload"),
    "subscription.created": ("🎞", "已添加订阅", "subscription"),
    "subscription.download_started": ("📥", "开始下载", "download"),
    "download.completed": ("✅", "下载完成", "download"),
    "subscription.fulfilled": ("🍟", "已入库", "import"),
    "library.items.arrived": ("🎞", "媒体库新增", "import"),
    "library.ingest.completed": ("🍟", "已入库", "import"),
    "subscription.upgraded": ("✨", "洗版完成", "import"),
    "playback.started": ("▶️", "开始播放", "playback"),
    "playback.stopped": ("⏹️", "停止播放", "playback"),
    "playback.completed": ("🎉", "观看完成", "playback"),
    "system.alert.raised": ("⚠️", "系统通知", "alert"),
}


def render(name, context, cfg):
    _, _, group = EVENTS.get(name, ("📢", "通知", "alert"))
    title_template = getattr(cfg, group + "_title")
    text_template = getattr(cfg, group + "_text")
    # Existing saved defaults adopt the new design; user-edited templates stay intact.
    if title_template == PREVIOUS_TITLE: title_template = DEFAULT_TITLE
    if text_template == PREVIOUS_TEXT: text_template = DEFAULT_TEXT
    if name == "drive.upload.completed" and text_template == PREVIOUS_UPLOAD_TEXT:
        text_template = DEFAULT_UPLOAD_TEXT
    title = ENV.from_string(title_template).render(context).strip()
    text = ENV.from_string(text_template).render(context).strip()
    # Preserve the overview paragraph, remove gaps caused by absent conditional fields.
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return title or "MovieClaw 通知", text

PREVIOUS_UPLOAD_TEXT = "☁️ 已上传：{{ uploaded }} 个文件\n⚡ 秒传：{{ rapid }} 个{% if failed %}\n⚠️ 未成功：{{ failed }} 个{% endif %}{% if created_at %}\n🕒 {{ created_at }}{% endif %}"
DEFAULT_UPLOAD_TEXT = "☁️ 已上传：{{ uploaded }} 个文件\n⚡ 秒传：{{ rapid }} 个{% if failed %}\n⚠️ 未成功：{{ failed }} 个{% endif %}{% if filenames %}\n\n📄 上传成功的文件：{% for filename in filenames %}\n• {{ filename }}{% endfor %}{% endif %}{% if filenames_remaining %}\n另有 {{ filenames_remaining }} 个文件未展开{% endif %}{% if filenames_missing %}\n{{ filenames_missing }} 个成功文件的名称未在任务记录中确认{% endif %}{% if created_at %}\n🕒 {{ created_at }}{% endif %}"
