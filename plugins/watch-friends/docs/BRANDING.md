# 映迹 · 品牌图标

![彩色图标、浅深主题导航与小尺寸预览](brand-preview.png)

标志将一条带齿孔的胶片与冰蓝折带汇成字母 **Y**，呼应「映迹」的名字、观影足迹和人与影片的交汇。尾部的播放留白强化影视识别。图形由原生 SVG 路径构建，无文字字体、远程图片或图标库依赖；源码采用插件的 GPL-3.0-only 许可证。

| 用途 | 文件 | 处理 |
| --- | --- | --- |
| 插件市场、网页品牌与现代浏览器 | `web/icon.svg` | 512 坐标矢量母版，深墨底、银白胶片与冰蓝折面 |
| MovieClaw 侧边栏与单色图标 | `web/icon-mask.svg` | 透明背景的独立字形，随宿主文字颜色着色 |
| PWA 普通图标 | `web/icon-192.png`、`web/icon-512.png` | 192 / 512 像素，使用母版完整图形 |
| PWA 可裁切图标 | `web/icon-maskable-512.png` | 满版背景，标志位于系统的中央安全区 |
| iOS 桌面快捷方式 | `web/apple-touch-icon.png` | 180 像素满版背景，由系统裁切外形 |

配色：深墨 `#080D15`～`#202C3D`，银白 `#F6F8FC`～`#C8D6E8`，冰蓝 `#A9D3FF`～`#6E9FD6`。导航单色版不带背景卡片，避免在深浅主题中显示方块。

`movieclaw_friends/static/` 为唯一源目录。修改 `icon.svg` 后，在已安装开发依赖及 Chromium 的环境中执行：

```sh
python plugins/watch-friends/tools/build_icons.py
python plugins/watch-friends/tools/build_web.py
python plugins/watch-friends/tools/build_web.py --check
```

PNG 只在开发时从 SVG 导出；运行插件不需要 Playwright 或图像处理库。更新市场图标后按仓库流程运行 `python scripts/generate_index.py --update`，由内容哈希刷新市场资源，避免继续使用旧缓存。市场图标随 catalog 发布，已安装网页资源随插件下一次打包发布更新。
