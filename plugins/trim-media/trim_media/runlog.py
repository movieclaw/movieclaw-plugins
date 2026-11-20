"""插件自己的运行日志：环形缓冲 + 日志处理器。

进程外插件的日志已经汇总进系统日志，但用户想知道「刚才同步到底做了什么」时要翻主机
文件；这里在插件进程里再留一份最近记录，页面每 3 秒增量拉一次，形成实时日志窗。

只记本插件自己的日志（``ctx.logger`` 的来源就是本条目），不读别人的日志文件，
因此不需要向用户申请任何路径授权。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

#: 留在内存里的最近条数（页面最多看 1000 条，留一倍余量）
CAPACITY = 2000


class RunLog:
    """线程安全的环形日志缓冲：``tail`` 给路由，``handler`` 接进 logging。"""

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._lines: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def add(self, level: str, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        with self._lock:
            self._seq += 1
            self._lines.append(
                {
                    "seq": self._seq,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "level": level,
                    "message": text,
                }
            )

    def tail(self, limit: int = 200, *, since: int = 0) -> list[dict[str, Any]]:
        """最近 ``limit`` 条；给了 ``since`` 就只回序号更大的（增量拉取用）。"""
        n = max(1, min(int(limit or 200), 1000))
        since = max(0, int(since or 0))
        with self._lock:
            rows = [row for row in self._lines if row["seq"] > since]
        return rows[-n:]

    def handler(self, level: int = logging.INFO) -> logging.Handler:
        """把插件的 logging 输出同时留一份进缓冲。"""

        sink = self

        class _Sink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    sink.add(record.levelname, record.getMessage())
                except Exception:  # noqa: BLE001 -- 日志本身不能再抛
                    pass

        h = _Sink(level=level)
        h.setFormatter(logging.Formatter("%(message)s"))
        return h
