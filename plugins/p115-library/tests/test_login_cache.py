"""Account cache behavior on a host whose monotonic clock is below ten minutes."""
import asyncio
from types import SimpleNamespace

import pytest
import p115_plugin as plugin
from conftest import Store


@pytest.mark.asyncio
async def test_first_account_read_fetches_before_ten_minutes(monkeypatch):
    store = Store()
    mounted = []
    calls = []
    clock = [30.0]

    class Life:
        def __init__(self, *args):
            self.engine = object()

        async def boot(self):
            pass

    async def client(ctx):
        return object()

    async def account(self):
        calls.append(clock[0])
        return {'uid': '123', 'user_name': '测试用户'}

    services = {
        'plugin-data': SimpleNamespace(scoped=lambda ctx: store),
        'plugin-files': SimpleNamespace(scoped=lambda ctx: object()),
        'host-ops': SimpleNamespace(client=client),
        'plugin-routes': SimpleNamespace(mount=lambda ctx, router, **kw: mounted.append(router)),
    }
    ctx = SimpleNamespace(config={'cookie': 'UID=123_A1; CID=test; SEID=test'},
                          use=lambda key: services[key.name])
    monkeypatch.setattr(plugin, 'LifeSTRM', Life)
    monkeypatch.setattr(plugin.Drive, 'account', account)
    # Patch only the plugin's clock; the event loop keeps its real monotonic clock.
    monkeypatch.setattr(plugin, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    await plugin.apply.apply(ctx)
    endpoint = next(route.endpoint for router in mounted for route in router.routes
                    if route.path == '/login/info')
    assert (await endpoint())['uid'] == '123'
    assert calls == [30.0]
    clock[0] = 60.0
    assert (await endpoint())['user_name'] == '测试用户'
    assert calls == [30.0]  # A populated cache still avoids repeated 115 requests.
    await endpoint(refresh=True)
    assert calls == [30.0, 60.0]
    clock[0] = 661.0
    await endpoint()
    assert calls == [30.0, 60.0, 661.0]
