# OpenList 直链（openlist-direct-link）

Symedia、CloudDrive2 一类工具生成的 strm 里写的是挂载路径（如 `/cd2/CloudDrive/115/电影/a.mkv`）。默认由 MovieClaw 读挂载、再推给播放器，视频流量要经过服务器两遍。

同一个网盘也挂在 OpenList 里时，装上这个插件，播放器就能直接从网盘取：

1. 按路径规则把挂载路径换成 OpenList 里的路径；
2. 向 OpenList 要这个文件的签名下载地址；
3. MovieClaw 带着播放器的 User-Agent 跟到网盘 CDN，再把播放器 302 过去。

不在规则里的文件、OpenList 连不上、OpenList 自己出字节（开了本地代理）时，照旧由服务器出字节，播放不受影响。

## 设置

装好后在插件设置页填写：

- **OpenList 地址**：如 `http://192.168.1.10:5244`，内网地址也行。
- **OpenList 令牌**：OpenList「设置 → 其他 → 令牌」；访客能读的存储可以留空。令牌加密保存。
- **路径规则**：每行一条 `挂载路径 => OpenList 路径`，如 `/cd2/CloudDrive/115 => /115`。
- **直链复用时长（秒）**：同一文件、同一播放器多久内复用同一条直链，应短于网盘直链本身的有效期。

## 要求

- MovieClaw 0.39.0 或更新版本（用到 `playback.strm.direct-link` 钩子）。
- 不申请宿主操作，只连你填写的 OpenList 地址。
