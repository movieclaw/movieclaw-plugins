"""所有站点共用的浏览器身份设置；更新时无需丢弃登录 Cookie。"""
from urllib.parse import urlparse, parse_qs, urlencode
from types import SimpleNamespace
import asyncio
from .profiles import PROFILE_PAGES, AUTH_CHECK, SELECTORS, enrich, api_fields
from .hr import CAPTURE, Capture, PREFIX, REPORTS, take, torrent_id


def validate_user_agent(value):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("User-Agent 只接受不超过 512 字符的单行英文请求头")
    return value.strip()


class IdentityClient:
    """保留宿主客户端的认证、代理、限流和重试，只覆盖当前请求的 UA。"""
    def __init__(self, client, site_id, resolve):
        self.inner, self.site_id, self.resolve = client, site_id, resolve

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @property
    def cookies(self):
        return self.inner.cookies

    @cookies.setter
    def cookies(self, value):
        self.inner.cookies = value

    async def request(self, operation, *args, **kwargs):
        ua = self.resolve(self.site_id)
        if ua:
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "User-Agent": ua}
        response = await getattr(self.inner, operation)(*args, **kwargs)
        capture = CAPTURE.get()
        if capture is not None:
            try: capture.response(response)
            except Exception: pass
        pages = PROFILE_PAGES.get()
        if pages is not None and operation in {"get", "raw_get", "post", "raw_post"} and len(pages) < 8:
            try:
                if response.status_code == 200 and len(response.content) <= 2 * 1024 * 1024:
                    content = response.text
                    if content.lstrip().startswith("<") and operation in {"get", "raw_get"}:
                        parsed=urlparse(str(response.url));args=parse_qs(parsed.query)
                        own_query=urlencode({"id":args["id"][0]}) if args.get("id") and args["id"][0].isdigit() else ""
                        pages.append((parsed.path+("?"+own_query if own_query else ""), content))
                    elif content.lstrip().startswith("{"):
                        path = urlparse(str(response.url)).path
                        fields = api_fields(path, response.json())
                        if fields: pages.append((path, fields))
            except (AttributeError, UnicodeError, ValueError): pass
        return response

    async def get(self, *a, **kw): return await self.request("get", *a, **kw)
    async def post(self, *a, **kw): return await self.request("post", *a, **kw)
    async def download(self, *a, **kw): return await self.request("download", *a, **kw)
    async def raw_get(self, *a, **kw): return await self.request("raw_get", *a, **kw)
    async def raw_post(self, *a, **kw): return await self.request("raw_post", *a, **kw)
    async def raw_request(self, *a, **kw): return await self.request("raw_request", *a, **kw)
    async def close(self): await self.inner.close()


def with_identity(base, resolve, observe=None, observe_profile=None, statistics_selectors=None):
    base = getattr(base, "_pt_identity_base", base)

    class IdentitySite(base):
        _pt_identity_base = base

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.client = IdentityClient(self.client, self.site_id, resolve)

        async def check_auth(self):
            token=AUTH_CHECK.set(True)
            try:return await super().check_auth()
            finally:AUTH_CHECK.reset(token)

        async def search(self, query):
            if query.keyword.startswith(PREFIX):
                token,ticket=take(query.keyword,self.site_id)
                capture=Capture(getattr(self,"selectors",None),torrent_id(ticket["url"]))
                context=CAPTURE.set(capture)
                try:
                    detail=await self.get_torrent_detail(ticket["url"])
                    if detail.torrent_id!=capture.expected:
                        raise ValueError("站点详情返回了不同的种子，未采用此结果")
                    value,source=capture.values.get(detail.torrent_id,(None,None))
                    REPORTS[token]={"value":value,"source":source}
                finally:CAPTURE.reset(context)
                from movieclaw_tracker.models import SearchResult
                return SearchResult(items=[],page=1,has_more=False)
            capture=Capture(getattr(self,"selectors",None));token=CAPTURE.set(capture)
            try:result=await super().search(query)
            finally:CAPTURE.reset(token)
            result=self._hr_items(result,capture)
            if observe: await observe(self.site_id, result.items)
            return result

        async def list_torrents(self, **kwargs):
            capture=Capture(getattr(self,"selectors",None));token=CAPTURE.set(capture)
            try:result=await super().list_torrents(**kwargs)
            finally:CAPTURE.reset(token)
            result=self._hr_items(result,capture)
            if observe: await observe(self.site_id, result.items)
            return result

        def _hr_items(self,result,capture):
            items=[]
            for item in result.items:
                value,source=capture.values.get(item.torrent_id,(None,None))
                # A configured CSS selector that misses an icon is not proof
                # that the torrent is exempt. Require explicit negative evidence.
                value=value if type(value) is bool else True if item.hit_and_run is True else None
                item=item.model_copy(update={"hit_and_run":value,"hr_source":source})
                items.append(item)
            return result.model_copy(update={"items":items})

        async def get_torrent_detail(self,url):
            capture=CAPTURE.get()
            token=None
            if capture is None:
                capture=Capture(getattr(self,"selectors",None),torrent_id(url));token=CAPTURE.set(capture)
            try:result=await super().get_torrent_detail(url)
            finally:
                if token is not None:CAPTURE.reset(token)
            value,source=capture.values.get(result.torrent_id,(None,None))
            if observe and type(value) is bool:
                await observe(self.site_id,[SimpleNamespace(torrent_id=result.torrent_id,hit_and_run=value,hr_source=source)])
            return result

        async def get_user_profile(self, user_id=None):
            pages = []
            token = PROFILE_PAGES.set(pages)
            try:
                result = await super().get_user_profile(user_id)
                # Naked Nexus userdetails.php often returns the FAQ/account
                # strip. Fetch the authenticated user's own page once, only
                # during an explicit profile collection, never a login probe.
                from .anniversary import registration_date, extract_registration, own_profile_id, already_requested
                from movieclaw_tracker.frameworks.nexusphp import NexusPHPSite
                if user_id is None and not AUTH_CHECK.get() and isinstance(self, NexusPHPSite) and registration_date(result.join_date) is None:
                    uid = own_profile_id(pages, result, self.base_url)
                    if uid and not already_requested(pages, uid) and extract_registration(pages, result) is None:
                        try:
                            response = await asyncio.wait_for(self.client.get(self._url('userdetails.php'), params={'id': uid},
                                headers={'Referer': self._url('index.php')}), 15)
                            if response.status_code == 200 and not result.user_id:
                                result = result.model_copy(update={'user_id': uid})
                        except Exception: pass  # Optional age must not fail a valid account.
            finally:
                PROFILE_PAGES.reset(token)
            try:
                selectors = getattr(self, "selectors", None)
                overrides = statistics_selectors(self.site_id) if statistics_selectors else {}
                if overrides:
                    selectors = SimpleNamespace(**({attr: getattr(selectors, attr, "") for attr in SELECTORS.values()} | overrides))
                result, fields, sources = enrich(result, pages, selectors)
                if observe_profile and user_id is None and not AUTH_CHECK.get():
                    await observe_profile(self.site_id, result, fields, sources)
            except Exception:
                pass  # Optional statistics must not turn a valid login into failure.
            return result

    return IdentitySite
