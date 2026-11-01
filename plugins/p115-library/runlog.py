"""运行事件日志（环形缓冲，面板「运行日志」栏展示）。

集中收录各环节的关键事件与失败原因（登录/同步/上传/配置），
方便在面板单独的日志栏里一眼发现问题。写入尽力而为，永不影响主流程。
"""
import time

MAX_ENTRIES = 100
LEVELS = ('info', 'warn', 'error')


async def log_event(store, level, source, message):
    """追加一条运行事件；level: info/warn/error，source: 登录/同步/上传/配置/系统/播放/115事件。"""
    if level not in LEVELS:
        level = 'info'
    try:
        rows = await store.get('runtime-log', default=[]) or []
        rows.append({'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                     'level': level, 'source': str(source)[:24],
                     'message': str(message)[:400]})
        await store.set('runtime-log', rows[-MAX_ENTRIES:])
    except Exception:
        pass
