# 2.3.0 中文通知模板

默认标题：

```jinja2
{{ event_icon }} {{ action }}｜{{ title_year }}{% if season_fmt %} · {{ season_fmt }}{% endif %}
```

默认正文：

```jinja2
{% if media_label %}{{ media_label }}{% if category %} · {{ category }}{% endif %}{% if vote_average %}  ⭐ {{ vote_average }}/10{% endif %}{% elif category %}{{ category }}{% endif %}
{% if episode_fmt %}🎬 {{ episode_fmt }}{% endif %}
{% if quality or size %}📀 {% if quality %}{{ quality }}{% endif %}{% if quality and size %} · {% endif %}{% if size %}{{ size }}{% endif %}{% endif %}
{% if resource and not quality %}📦 {{ resource }}{% endif %}
{% if username %}👤 {{ username }}{% endif %}
{% if device %}📱 {{ device }}{% endif %}
{% if progress %}⏱ {{ progress }}{% endif %}
{% if actors %}主演：{{ actors }}{% endif %}
{% if overview %}🍿 {{ overview }}{% endif %}
{% if created_at %}🕒 {{ created_at }}{% endif %}
```

新增变量：media_kind、media_label、is_movie、is_tv。电影的 season 为 None，season_fmt 和 episode_fmt 为空；电视剧第 0 季有效集显示特别篇。episode_fmt 压缩连续集数，去重逻辑仍逐集处理。

只有精确匹配上一版默认文本的模板自动更新；自定义模板保持不变。正文是纯文本，插件负责安全转义和企业微信消息长度限制。

上传通知使用独立的 upload_title / upload_text，提供 uploaded（成功数）、rapid（秒传数）、failed（未成功数）；title 为媒体库名，created_at 为任务完成时间。有失败文件时默认 action 为“网盘上传部分完成”。新增 filenames、filenames_remaining、filenames_missing；文件名由时间线与成功计数确认。默认模板自动更新，自定义保持。
