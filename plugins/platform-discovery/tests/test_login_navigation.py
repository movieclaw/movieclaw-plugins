import unittest
import httpx
from fastapi import FastAPI, Depends, HTTPException
from platform_discovery import Settings
from platform_discovery.routes import build_routers
from test_routes import FakeSources

BASE = "/api/v1/plugins/platform-discovery"

class LoginNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_guard_401_redirects_only_html_navigation(self):
        class HostUnauthorized(Exception):
            status_code = 401
        async def guard():
            raise HostUnauthorized()
        pages, _ = build_routers("platform-discovery", Settings(), FakeSources(Settings()), None)
        app = FastAPI()
        app.include_router(pages, prefix=BASE, dependencies=[Depends(guard)])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://lan.test") as client:
            for url in [BASE+"/panel?view=personal", BASE+"/panel?view=personal&next=https://evil.test", BASE+"/"]:
                response = await client.get(url, headers={"Accept":"text/html"})
                self.assertEqual(response.status_code, 303, response.text)
                self.assertTrue(response.headers["location"].startswith("/login?next=%2Fapi%2Fv1%2Fplugins%2Fplatform-discovery%2Fpanel"))
                self.assertNotIn("evil", response.headers["location"])
            self.assertEqual((await client.get(BASE+"/catalog", headers={"Accept":"text/html"})).status_code, 500)

    async def test_json_401_and_forbidden_stay_errors(self):
        for code in [401,403]:
            async def guard():
                raise HTTPException(code)
            pages, _ = build_routers("platform-discovery", Settings(), FakeSources(Settings()), None)
            app = FastAPI()
            app.include_router(pages,prefix=BASE,dependencies=[Depends(guard)])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://wan.test") as client:
                self.assertEqual((await client.get(BASE+"/panel",headers={"Accept":"application/json"})).status_code,code)
                self.assertEqual((await client.get(BASE+"/catalog",headers={"Accept":"text/html"})).status_code,code)
                if code==403:self.assertEqual((await client.get(BASE+"/panel",headers={"Accept":"text/html"})).status_code,403)
