# 快捷指令维护工具（Mac）

普通使用者直接从寻映页面下载分享版并填写 SEARCH_URL，不需要这些工具。
维护者仅在更新快捷指令流程，或为自己的服务器准备已配置个人版时使用。

需要 macOS 自带的“快捷指令”、`shortcuts`、`aea`、`aa`，以及 Python 3.11+。
在插件目录执行以下命令创建独立环境；服务端运行插件不需要安装这些额外依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install cryptography
```

`shortcut_source.py` 是完整、可审阅的动作源码，不读取本机配置。
公开文件始终是 `web/downloads/find-movie-share.shortcut`，配置保留占位。

## 更新或检查通用分享版

```sh
.venv/bin/python tools/build_share.py
.venv/bin/python tools/build_share.py --check
```

第一条从源码生成流程并调用系统签名；第二条只验签现有公开文件，核对占位和首次配置检查。
系统签名可能需要登录 Apple 账号与联网。Apple 在签名时会接收文件进行验证。

## 为自己的服务器准备个人版

先安装 MovieClaw 官方 `mclaw` CLI、加入 PATH，并按官方说明登录自己的服务器。
确认 `mclaw status` 指向预期服务器，再运行：

```sh
.venv/bin/python tools/publish_personal.py
# 多服务器时显式选择已配对的上下文：
.venv/bin/python tools/publish_personal.py --context my-server
```

工具从当前服务器取专用链接，填入临时流程、调用 macOS 签名，再验签并核对内嵌地址，最后通过管理员接口上传。
授权不放进命令行参数或公开目录。临时文件在结束后删除；个人版仅存于该服务器的加密插件数据。
Apple 也会收到这份含专用链接的文件以完成系统签名。不要把个人版上传到 Git 或公开分发。

刷新寻映页面后即可点击“下载已配置个人版”。下载时 Mac 无需在线。
服务器地址变化或撤销全部专用授权后，重新运行准备命令；旧个人版会停止分发。
Linux 服务端不能代替这次 macOS 签名，尚未准备时仍可使用通用分享版。

## 验证说明

Mac 工具验证 Apple Archive 的签名和实际解出的流程；个人版上传前核对配置，服务端再核对封装、大小和摘要。
服务端不解析完整 Apple 快捷指令签名，也不能从加密封装里独立核对内嵌链接，因此仅接受管理员受信工具提交。
仓库单元测试可在 Linux 运行；实际 Apple 验签和原生导入验收须在 Mac 上执行。
