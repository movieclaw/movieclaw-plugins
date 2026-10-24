"""真实目录装配与内存原生账号夹具；所有站点网络请求均被本地 MockTransport 接管。"""
import inspect
from unittest.mock import patch
import httpx
from movieclaw_kernel.registry import Registry
from movieclaw_api.plugins.keys import SITE_CLASSES, SITE_DATA_PACKS
from movieclaw_tracker import registry as tracker
import movieclaw_tracker
from movieclaw_tracker import CookieAuthProvider
from movieclaw_tracker.http import HttpClient
from movieclaw_tracker.models import SearchQuery
from pt_pack import Settings
from .test_assistant import FakeContext


def fixture_response(request):
    if request.url.path == "/index.php":
        return httpx.Response(200, text='<a href="userdetails.php?id=12">fixture</a>')
    if request.url.path == "/userdetails.php":
        return httpx.Response(200, text='<h1>fixture</h1>')
    if request.url.path == "/torrents.php":
        return httpx.Response(200, text='''<table class="torrents"><tr><td>header</td></tr><tr>
        <td><a href="?cat=401">Movie</a></td><td><a href="details.php?id=9">Fixture Film</a><a href="download.php?id=9">DL</a></td>
        <td>1</td><td><span title="2026-10-24 01:00:00">now</span></td><td>1 GiB</td><td>7</td><td>2</td><td>3</td><td>u</td>
        </tr></table>''')
    if request.url.path == "/details.php":
        return httpx.Response(200, text='<h1 id="top">Fixture Film</h1><div id="kdescr">fixture description</div><a href="download.php?id=9">DL</a>')
    raise AssertionError("Unexpected request: " + request.url.path)


class NativeOps:
    def __init__(self):
        self.accounts = {}
        self.calls = []
        self.network_calls = []

    async def instance(self, sid):
        def response(request):
            self.network_calls.append(request)
            return fixture_response(request)
        factory = lambda **kwargs: HttpClient(transport=httpx.MockTransport(response))
        with patch("movieclaw_tracker.HttpClient", factory):
            site = await movieclaw_tracker.create_site(sid, auth_provider=CookieAuthProvider({"uid": "fixture"}))
        await site.authenticate()
        return site

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "site.catalog":
            return [{"site_id": s.site_id, "display_name": s.display_name, "base_url": s.base_url, "web_base_url": s.web_base_url} for s in tracker.list_sites()]
        if name == "site.list": return list(self.accounts.values())
        if name == "site.stats": return {}
        sid = args.get("site_id") or args.get("sites", [None])[0]
        if name == "site.verify":
            site = await self.instance(sid)
            try:
                profile = await site.get_user_profile()
                self.accounts[sid].update(status="active", usable=True, profile={"username": profile.username})
                return self.accounts[sid]
            finally: await site.client.close()
        if name == "search.torrents":
            site = await self.instance(sid)
            try:
                result = await site.search(SearchQuery(keyword=args["keyword"]))
                return {"sites": [], "items": [i.model_dump(mode="json") for i in result.items]}
            finally: await site.client.close()
        raise AssertionError(name)


class RuntimeContext(FakeContext):
    def __init__(self, root, config=None, data=None):
        super().__init__(root, config or Settings(), data)
        self.ops = NativeOps()
        self.registries = {key: Registry(key) for key in (SITE_CLASSES, SITE_DATA_PACKS)}
        self.disposers = []
        def on_class(change):
            if change.kind == "added": tracker.register_site_class(change.id, change.item)
            else: tracker.unregister_site_class(change.id)
            tracker.load_all_sites()
        def on_pack(change):
            if change.kind == "added": tracker.register_data_pack(change.id, change.item)
            else: tracker.unregister_data_pack(change.id)
            tracker.load_all_sites()
        self.registries[SITE_CLASSES].watch(on_class)
        self.registries[SITE_DATA_PACKS].watch(on_pack)
        tracker.load_all_sites()

    def registry(self, key): return self.registries[key]

    def contribute(self, key, name, item):
        self.disposers.append(self.registry(key).add("pt-site-pack:"+name, item, entry_id="pt-site-pack", tier=1))

    def effect(self, callback, **kwargs): self.disposers.append(callback)

    async def close(self):
        for callback in reversed(self.disposers):
            result = callback()
            if inspect.isawaitable(result): await result
        self.disposers.clear()
        for coro in self.pending: coro.close()
        self.pending.clear()
