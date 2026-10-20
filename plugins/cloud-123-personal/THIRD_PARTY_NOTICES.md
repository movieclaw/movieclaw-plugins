# Third-party notices

运行依赖按 requirements.lock 下载；包本身不含第三方轮子或复制的库源码。33项依赖的许可证正文从实际轮子提取，保留在 licenses/；另1项只有MIT声明但漏附正文，见下方明确缺口。

| Dependency | Version | Wheel license |
|---|---|---|
| bitarray | 3.12.3 | PSF-2.0 |
| brotli | 1.2.0 | MIT |
| errno2 | 0.0.5 | MIT |
| http-response | 0.0.9.1 | MIT |
| httpx-request | 0.1.9.1 | MIT |
| integer-tool | 0.0.6.1 | MIT |
| iterdir | 0.0.10.1 | MIT |
| jh2 | 5.0.15 | MIT |
| multidict | 7.1.0 | Apache-2.0 |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| p123client | 0.0.9.4 | MIT |
| propcache | 0.5.4 | Apache-2.0 |
| python-argtools | 0.0.4 | MIT |
| python-asynctools | 0.2.2 | MIT |
| python-cookietools | 0.1.5 | MIT |
| python-decotools | 0.0.4 | MIT |
| python-dicttools | 0.0.5 | MIT |
| python-encode-uri | 0.0.3 | MIT |
| python-ensure | 0.0.2 | MIT |
| python-filewrap | 0.3.1.1 | MIT |
| python-hashtools | 0.0.6 | MIT |
| python-http-request | 0.1.7.2 | MIT |
| python-httpfile | 0.0.6.1 | MIT |
| python-iterutils | 0.3.1.1 | MIT |
| python-property | 0.0.3 | MIT |
| python-texttools | 0.0.6 | MIT |
| python-undefined | 0.0.4 | MIT |
| qh3 | 2.0.4 | BSD-3-Clause |
| socket-keepalive | 0.0.1 | MIT |
| urllib3-future | 2.25.902 | MIT |
| urllib3-future-request | 0.0.2 | MIT声明；发行物缺许可证正文 |
| wcwidth | 0.9.2 | MIT |
| yarl | 1.25.1 | Apache-2.0 |
| zstandard | 0.25.0 | BSD-3-Clause |

p123client 0.0.9.4 的发行轮子为 MIT。P123Disk（GPL-3.0）和 OpenList（AGPL-3.0）仅作为独立接口研究来源，没有复制其实现或作为运行依赖。

orjson 3.13.0 的许可表达式为 MPL-2.0 AND (Apache-2.0 OR MIT)，不是全部 MIT。这里使用未修改的独立依赖；不移除或重写其许可证。若日后修改或重新分发覆盖文件，需要保留相应源码提供和通知义务。jh2 的 classifier 提到 Apache，但实物 LICENSE 是 MIT，此处以实物为依据。qh3 和 zstandard 为 BSD-3-Clause，bitarray 为 PSF-2.0。

2026-10-19：官方工具生成34包锁，实际下载并SHA256核对 linux/amd64 与 linux/arm64 共68个目标轮子选择；分别检查了许可证文件。源码没有引入GPL实现；MPL/Apache/BSD/PSF并不需要把原创适配层改为GPL。未将其解释为法律意见或官方发布批准。

宿主提供 httpx、pydantic、qrcode 等，不重复打入锁。MovieClaw 核心自身有独立的非商业许可；本插件不重新分发核心或SDK，MIT声明不改变宿主许可。

`licenses/` 保留上游许可证的原始字节（包括原有尾随空白）；本目录 `.gitattributes` 仅对这些文本禁用Git空白告警，不影响源码、测试或契约检查。

## 发布前待补齐

`urllib3-future-request 0.0.2` 的wheel、同版本sdist以及声明源码仓库的固定提交均缺LICENSE正文，只有METADATA/pyproject里的MIT声明。详见 [证据与哈希](licenses/urllib3-future-request/README.md)。此前“全部来自wheel正文”的描述不准确，本次更正。保持依赖锁与源码原样；需要上游提供缺失原文后才可声称许可正文审查全部完成。其余33项正文继续保留，不以别的包MIT文本补位。
