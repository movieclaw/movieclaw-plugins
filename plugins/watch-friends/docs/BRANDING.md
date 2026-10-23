# 映迹 · 品牌图标

![彩色图标、浅深主题导航与小尺寸预览](brand-preview.png)

标志由奶油金色的圆角播放画格和薄荷绿色的回转轨迹组成，呼应「映迹」记录观影足迹、汇总影片与片单的功能。三个画格留白强化影视识别，轨迹末端的圆点表示观看进程。整体采用柔和的几何形状与留白。图形由原生 SVG 路径构建，无文字字体、远程图片或图标库依赖；源码采用插件的 GPL-3.0-only 许可证。

| 用途 | 文件 | 处理 |
| --- | --- | --- |
| 插件市场、网页品牌与现代浏览器 | `web/icon.svg` | 512 坐标矢量母版，深青绿底、奶油金播放画格与薄荷绿轨迹 |
| MovieClaw 侧边栏与单色图标 | `web/icon-mask.svg` | 透明背景的独立字形，随宿主文字颜色着色 |
| PWA 普通图标 | `web/icon-192.png`、`web/icon-512.png` | 192 / 512 像素，使用母版完整图形 |
| PWA 可裁切图标 | `web/icon-maskable-512.png` | 满版背景，标志位于系统的中央安全区 |
| iOS 桌面快捷方式 | `web/apple-touch-icon.png` | 180 像素满版背景，由系统裁切外形 |

配色：深青绿 `#173234`，奶油金 `#FFF3D4`～`#EDCD92`，薄荷绿 `#89C5B4`。导航单色版不带背景卡片，避免在深浅主题中显示方块。

`movieclaw_friends/static/` 为唯一源目录。修改 `icon.svg` 后，在已安装开发依赖及 Chromium 的环境中执行：

```sh
python plugins/watch-friends/tools/build_icons.py
python plugins/watch-friends/tools/build_web.py
python plugins/watch-friends/tools/build_web.py --check
```

PNG 只在开发时从 SVG 导出；运行插件不需要 Playwright 或图像处理库。更新市场图标后按仓库流程运行 `python scripts/generate_index.py --update`，由内容哈希刷新市场资源，避免继续使用旧缓存。市场图标随 catalog 发布，已安装网页资源随插件下一次打包发布更新。
