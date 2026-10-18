# 115 网盘（cloud-115）

把 115 网盘接进 MovieClaw 的媒体库：实现内核契约 `drive-backends`（`movieclaw_sdk.drives.DriveBackend`），
账号、凭据加密、限速、任务、界面都在主程序，插件只负责「怎么跟 115 说话」。设计见 MovieClaw 仓库
`docs/design/cloud-storage-115.md`。

- 登录：扫码（选一个没在用的登录端，默认支付宝小程序）或浏览器扩展推送的 Cookie。
- 纳管：目录选择、整树列举（带 SHA1）、操作动态增量。
- 上传：秒传（SHA1 + 区间校验），不中就走阿里云 OSS 分片上传，续传状态交宿主保存。
- 取流：按播放器的 User-Agent 取原画直链。
- 删除：进 115 回收站。

## 许可证

本插件以 **GPL-3.0-or-later** 发布（见 [LICENSE](LICENSE)），与本仓库其他插件不同：它依赖的
[p115cipher](https://pypi.org/project/p115cipher/)（115 的请求加密）与 [p115pickcode](https://pypi.org/project/p115pickcode/)
（提取码换文件 id）是 GPLv3（PyPI 元数据写的是 MIT，但轮子里的 LICENSE 与源码头都是 GPLv3）。
这两个包不随插件包分发：管理员批准后由 MovieClaw 服务器按 `requirements.lock`（钉死版本与哈希）从 PyPI 下载安装。
插件运行在独立进程里，经本机 HTTP 与主程序通信。接口路径与参数参考了 [p115client](https://github.com/ChenyangGao/p115client)（MIT）。

## 测试

```bash
pip install --require-hashes --only-binary=:all: --no-deps -r requirements.lock
python -m unittest discover -s tests -t . -v
```

测试用 `httpx.MockTransport` 仿 115（扫码各状态、整树列举分页、操作动态、上传初始化各分支、OSS 分片与续传、
登录失效与限流），不连真 115。真账号上还有若干【待实测】项，见设计稿。
