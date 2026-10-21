"""前台控制真实后台任务：停止、暂停、恢复、幂等重试和授权隔离。"""

import asyncio

import pytest
from test_app import harness as _harness
from test_resume_sync import setup

harness = _harness


@pytest.mark.parametrize("kind", ["auto-sync", "resume-sync"])
async def test_stop_keeps_schedule_pause_preserves_scope_and_resume_starts_new_round(
    harness, monkeypatch, kind
):
    h = harness
    await setup(h, monkeypatch)
    if kind == "auto-sync":
        view = (await h.clients["alice"].get("/api/watch/auto-sync")).json()
        assert (
            await h.clients["alice"].put(
                "/api/watch/auto-sync",
                json={
                    "enabled": True,
                    "consent": True,
                    "sources": view["sources"],
                },
            )
        ).status_code == 200
    service = h.app.state.auto_sync if kind == "auto-sync" else h.app.state.resume_sync
    baseline = (await h.clients["alice"].get(f"/api/watch/{kind}")).json()
    original = h.app.state.watch_archive.external.read
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        args[3](123)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    h.app.state.watch_archive.external.read = blocked
    await h.clients["alice"].post(f"/api/watch/{kind}/check")
    await entered.wait()
    current = (await h.clients["alice"].get(f"/api/watch/{kind}")).json()["runs"][0]
    response = await h.clients["alice"].post(
        f"/api/watch/{kind}/control",
        json={
            "action": "stop",
            "run_id": current["id"],
        },
    )
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["enabled"] and view["sources"] == baseline["sources"]
    assert view["runs"][0]["status"] == "stopped" and view["next_at"]
    assert not service.running
    # 过时页面不能停止后来的一轮任务。
    await h.clients["alice"].post(f"/api/watch/{kind}/control", json={"action": "retry"})
    await asyncio.sleep(0)
    response = await h.clients["alice"].post(
        f"/api/watch/{kind}/control",
        json={
            "action": "stop",
            "run_id": current["id"],
        },
    )
    assert response.status_code == 409 and service.running
    for _ in range(2):
        assert (
            await h.clients["alice"].post(
                f"/api/watch/{kind}/control",
                json={
                    "action": "retry",
                },
            )
        ).status_code == 200
    assert len(service.running) == 1
    paused = await h.clients["alice"].post(f"/api/watch/{kind}/control", json={"action": "pause"})
    assert not paused.json()["enabled"] and not paused.json()["next_at"]
    assert paused.json()["sources"] == baseline["sources"]
    assert not service.running
    retry = await h.clients["alice"].post(f"/api/watch/{kind}/control", json={"action": "retry"})
    assert retry.status_code == 409
    response = await h.clients["alice"].post(
        f"/api/watch/{kind}/control", json={"action": "resume"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] and response.json()["sources"] == baseline["sources"]
    release.set()
    await asyncio.gather(*service.running.values())
    report = (await h.clients["alice"].get(f"/api/watch/{kind}")).json()
    assert report["runs"][0]["id"] != current["id"]
    assert report["runs"][0]["status"] == "done"


@pytest.mark.parametrize("kind", ["auto-sync", "resume-sync"])
async def test_other_account_and_changed_grant_cannot_resume_saved_scope(
    harness, monkeypatch, kind
):
    h = harness
    await setup(h, monkeypatch)
    if kind == "auto-sync":
        view = (await h.clients["alice"].get("/api/watch/auto-sync")).json()
        await h.clients["alice"].put(
            "/api/watch/auto-sync",
            json={
                "enabled": True,
                "consent": True,
                "sources": view["sources"],
            },
        )
    service = h.app.state.auto_sync if kind == "auto-sync" else h.app.state.resume_sync
    await h.clients["alice"].post(f"/api/watch/{kind}/check")
    await asyncio.gather(*service.running.values())
    run = (await h.clients["alice"].get(f"/api/watch/{kind}")).json()["runs"][0]
    other = await h.clients["bob"].post(
        f"/api/watch/{kind}/control",
        json={
            "action": "pause",
            "run_id": run["id"],
        },
    )
    assert other.status_code == 404
    await h.clients["alice"].post(f"/api/watch/{kind}/control", json={"action": "pause"})
    store = h.app.state.store
    store.execute("UPDATE watch_sources SET account_id='changed' WHERE platform='emby'")
    response = await h.clients["alice"].post(
        f"/api/watch/{kind}/control", json={"action": "resume"}
    )
    assert response.status_code == 409
    assert not (await h.clients["alice"].get(f"/api/watch/{kind}")).json()["enabled"]
