"""同一账号、授权版本和库范围的在途扫描合并，不缓存跨轮次旧观看状态。"""

import asyncio
from types import SimpleNamespace


class InventoryReads:
    """共享一次读取；每个任务仍独立校验授权，取消一方不会中断另一方。"""

    def __init__(self, task_factory):
        self.task_factory = task_factory
        self.entries = {}

    async def read(self, key, load, progress):
        entry = self.entries.get(key)
        if entry is None:
            entry = SimpleNamespace(task=None, count=0, listeners={})
            self.entries[key] = entry
        failure = asyncio.get_running_loop().create_future()
        entry.listeners[failure] = progress

        def notify(count):
            entry.count = count
            for waiter, callback in tuple(entry.listeners.items()):
                if waiter.done():
                    continue
                try:
                    callback(count)
                except Exception as error:
                    # 范围变化只停止对应消费者，不能让它把另一项有效授权一起取消。
                    waiter.set_exception(error)

        try:
            progress(entry.count)
            if entry.task is None:
                entry.task = self.task_factory(load(notify))
            await asyncio.wait((entry.task, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure.done():
                await failure
            return await asyncio.shield(entry.task)
        finally:
            entry.listeners.pop(failure, None)
            if not failure.done():
                failure.cancel()
            else:
                failure.exception()
            if not entry.listeners:
                if self.entries.get(key) is entry:
                    self.entries.pop(key)
                if entry.task is not None:
                    if not entry.task.done():
                        entry.task.cancel()
                    await asyncio.gather(entry.task, return_exceptions=True)

    async def close(self):
        tasks = [entry.task for entry in self.entries.values() if entry.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
