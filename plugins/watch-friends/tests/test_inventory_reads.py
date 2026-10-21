"""重复扫描合并：不跨账号/库/授权、不保留旧缓存，并独立传播取消和错误。"""

import asyncio

import pytest
from movieclaw_friends.inventory_reads import InventoryReads


async def test_same_inflight_scan_shared_with_progress_but_next_round_is_fresh():
    service = InventoryReads(asyncio.create_task)
    started, release = asyncio.Event(), asyncio.Event()
    calls, a, b = [], [], []

    async def load(progress):
        calls.append(1)
        progress(100)
        started.set()
        await release.wait()
        progress(200)
        return {"records": [200]}

    one = asyncio.create_task(service.read(("owner", "pin", "library"), load, a.append))
    await started.wait()
    two = asyncio.create_task(service.read(("owner", "pin", "library"), load, b.append))
    await asyncio.sleep(0)
    release.set()
    assert await one == await two == {"records": [200]}
    assert calls == [1] and a[-1] == b[-1] == 200 and 100 in b
    assert service.entries == {}
    await service.read(("owner", "pin", "library"), load, a.append)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "other",
    [
        ("bob", "pin", "library"),
        ("alice", "new-pin", "library"),
        ("alice", "pin", "private-library"),
    ],
)
async def test_account_grant_and_library_scopes_never_share(other):
    service = InventoryReads(asyncio.create_task)
    release = asyncio.Event()
    calls = []

    async def load(progress):
        calls.append(1)
        await release.wait()
        return {}

    tasks = [
        asyncio.create_task(service.read(key, load, lambda n: None))
        for key in (("alice", "pin", "library"), other)
    ]
    for _ in range(4):
        await asyncio.sleep(0)
    assert len(calls) == 2
    release.set()
    await asyncio.gather(*tasks)


async def test_cancel_or_revoke_one_consumer_does_not_cancel_other():
    service = InventoryReads(asyncio.create_task)
    started, tick, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def load(progress):
        calls.append(1)
        started.set()
        await tick.wait()
        progress(100)
        await release.wait()
        return {"ok": True}

    def revoked(count):
        if count:
            raise ValueError("此任务授权已撤回")

    one = asyncio.create_task(service.read("same", load, revoked))
    await started.wait()
    two = asyncio.create_task(service.read("same", load, lambda n: None))
    await asyncio.sleep(0)
    tick.set()
    with pytest.raises(ValueError, match="撤回"):
        await one
    assert not two.done()
    release.set()
    assert await two == {"ok": True} and calls == [1]
    assert not service.entries


async def test_cancel_last_consumer_closes_reader_and_failed_reads_are_not_cached():
    service = InventoryReads(asyncio.create_task)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def load(progress):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    reader = asyncio.create_task(service.read("one", load, lambda n: None))
    await started.wait()
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert cleaned.is_set() and not service.entries
    calls = []

    async def fails(progress):
        calls.append(1)
        raise ValueError("本轮读取失败")

    for _ in range(2):
        with pytest.raises(ValueError, match="读取失败"):
            await service.read("one", fails, lambda n: None)
    assert calls == [1, 1] and not service.entries
