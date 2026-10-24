"""站点 3xx 跳转的单元测试（假宿主，不发真实请求）。

宿主的 ``site.proxy.request`` 不跟随站点的 3xx（认证头不能经站点的开放重定向带到外站），
原样回 ``{"status_code": 302, "headers": {"location": ...}}``；跳到登录页就是会话失效，
同站的跳转由插件自己再发一次站内路径的请求，其余一律不跟。这里验证插件怎么决定。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pt_keepalive import state as st
from pt_keepalive.result import Outcome, SiteResult
from pt_keepalive.runner import KeepaliveRunner
from pt_keepalive.settings import Settings
from pt_keepalive.sites import (
    MAX_REDIRECTS,
    PlannedRequest,
    SiteInfo,
    Step,
    is_login_location,
    redirect_location,
    redirect_target,
)

from tests.test_runner import _LOGGED_IN, _FakeCtx, _FakeData, _FakeEvents, _FakeOpsClient, _html

_HDSKY = "https://hdsky.me"


def _redirect(
    location: str | None, *, status: int = 302, header: str = "location"
) -> dict[str, Any]:
    """宿主原样回传的 3xx：``Location`` 在响应头里（宿主回的头名是小写），正文为空。"""
    headers = {} if location is None else {header: location}
    return {
        "status_code": status,
        "headers": headers,
        "text": "",
        "json_body": None,
        "truncated": False,
    }


def _runner(responses: dict[str, Any], *, catalog: bool = True, config: Any = None):
    """hdsky 一个站的假宿主；``responses`` 按请求路径给响应，没列出的路径一请求就报错。

    ``catalog=False`` 模拟拿不到站点目录（也就拿不到站点域名）。
    """
    data, events = _FakeData(), _FakeEvents()
    ops = _FakeOpsClient(
        {("site.proxy.request", "hdsky", path): response for path, response in responses.items()},
        sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
        catalog=(
            [{"site_id": "hdsky", "display_name": "HDSky", "base_url": _HDSKY}] if catalog else []
        ),
    )
    runner = KeepaliveRunner(_FakeCtx(ops, config), data=data, events=events)
    return runner, data, events, ops


def _paths(ops: _FakeOpsClient) -> list[str]:
    return [call["path"] for call in ops.calls]


def _expired(events: _FakeEvents) -> list[Any]:
    return [p for _, p in events.published if getattr(p, "site_id", None) == "hdsky"]


class TestRedirectHelpers(unittest.TestCase):
    def test_location_header_is_found_case_insensitively(self):
        self.assertEqual(redirect_location(_redirect("/a", header="Location")), "/a")
        self.assertEqual(redirect_location(_redirect("  /a  ")), "/a")
        self.assertEqual(redirect_location(_redirect(None)), "")
        self.assertEqual(redirect_location({"status_code": 302}), "")
        self.assertEqual(redirect_location({"headers": None}), "")

    def test_login_location_looks_at_the_path_only(self):
        for location in (
            "login.php",
            "takelogin.php",
            "/login.php?returnto=index.php",
            "https://hdsky.me/login.php?returnto=index.php",
            "//hdsky.me/Login.PHP",
            "/user/login",
            "https://sso.example/login",  # 是不是登录页不依赖主机
        ):
            with self.subTest(location=location):
                self.assertTrue(is_login_location(location))
        for location in (
            "",
            "/index.php",
            "/index.php?from=login",  # 只出现在查询串里，不算
            "/attendance.php?returnto=login.php",
            "https://hdsky.me/index.php#login",
            "http://[::1",  # 解析不了：不当成登录页，也不抛
        ):
            with self.subTest(location=location):
                self.assertFalse(is_login_location(location))

    def test_relative_locations_resolve_against_the_current_path(self):
        for location, current, expected in (
            ("login.php", "index.php", "/login.php"),
            ("/forum/", "index.php", "/forum/"),
            ("view.php?id=1", "/forum/index.php", "/forum/view.php?id=1"),
            ("../top.php", "/forum/a/index.php", "/forum/top.php"),
            ("../top.php", "index.php", "/top.php"),  # 已在根目录：多出来的 .. 直接丢掉
            ("?page=2", "/forum.php?page=1", "/forum.php?page=2"),
            ("/x.php?a=1&b=2#top", "index.php", "/x.php?a=1&b=2"),  # 片段不进请求
        ):
            with self.subTest(location=location, current=current):
                self.assertEqual(redirect_target(location, current, _HDSKY), (expected, ""))
                # 相对路径不需要域名：拿不到站点域名也照跟
                self.assertEqual(redirect_target(location, current), (expected, ""))

    def test_absolute_locations_are_followed_only_on_the_site_host(self):
        for location, expected in (
            ("https://hdsky.me/index.php?x=1", "/index.php?x=1"),
            ("http://hdsky.me/index.php?x=1", "/index.php?x=1"),  # 只看主机，不看协议
            ("HTTPS://HDSky.ME:443/index.php?x=1", "/index.php?x=1"),
            ("//hdsky.me/index.php?x=1", "/index.php?x=1"),
            ("https://hdsky.me", "/"),
        ):
            with self.subTest(location=location):
                self.assertEqual(redirect_target(location, "index.php", _HDSKY), (expected, ""))
        for location in (
            "https://evil.example/collect",
            "//evil.example/collect",
            "https://hdsky.me.evil.example/index.php",
            "https://hdsky.me@evil.example/index.php",
            "https://www.hdsky.me/index.php",  # 同一个站的另一个域名也是「别的主机」
        ):
            with self.subTest(location=location):
                target, reason = redirect_target(location, "index.php", _HDSKY)
                self.assertIsNone(target)
                self.assertIn("外部地址", reason)
        _, reason = redirect_target("https://evil.example/collect", "index.php", _HDSKY)
        self.assertIn("evil.example", reason)

    def test_absolute_location_without_site_domain_is_not_followed(self):
        target, reason = redirect_target("https://hdsky.me/index.php", "index.php")
        self.assertIsNone(target)
        self.assertIn("无法确认是同站", reason)

    def test_unfollowable_locations(self):
        for location in (
            "javascript:alert(1)",
            "mailto:ops@hdsky.me",
            "ftp://hdsky.me/x",
            "http://[::1",
        ):
            with self.subTest(location=location):
                target, reason = redirect_target(location, "index.php", _HDSKY)
                self.assertIsNone(target)
                self.assertTrue(reason)


class TestRedirectFollowing(unittest.TestCase):
    # ------------------------------------------------------------------ 跳到登录页 = 凭据失效

    def test_login_redirect_means_credentials_expired(self):
        # 以前宿主跟随跳转，拿到登录页 HTML（密码框）才判失效；现在只看到 302
        runner, data, events, ops = _runner(
            {
                "index.php": _redirect("login.php?returnto=index.php"),
                "attendance.php": _html("签到成功"),
            }
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        detail = result["sites"]["hdsky"]["detail"]
        self.assertIn("登录页", detail)
        self.assertIn("凭据已失效", detail)
        self.assertEqual(len(_expired(events)), 1)  # 凭据失效告警照发
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "failed")
        self.assertTrue(states["hdsky"]["alerted_expired"])
        self.assertEqual(_paths(ops), ["index.php"])  # 首页就失效：不再打签到页
        asyncio.run(runner.run_round())  # 同一段失效只告警一次
        self.assertEqual(len(_expired(events)), 1)

    def test_every_login_redirect_form_means_expired(self):
        for catalog in (True, False):  # 判登录页不需要站点域名
            for location in (
                "login.php",
                "login.php?returnto=index.php",
                "/login.php?returnto=attendance.php",
                "https://hdsky.me/login.php?returnto=index.php",  # 绝对 URL 的写法
                "http://hdsky.me/takelogin.php",
                "//hdsky.me/Login.PHP",
                "https://sso.example/login",
            ):
                with self.subTest(catalog=catalog, location=location):
                    runner, _, events, ops = _runner(
                        {"index.php": _redirect(location)}, catalog=catalog
                    )
                    result = asyncio.run(runner.run_round())
                    self.assertEqual(result["failed"], 1)
                    self.assertEqual(len(_expired(events)), 1)
                    self.assertEqual(_paths(ops), ["index.php"])  # 登录页不跟

    def test_login_redirect_on_a_later_step_expires_too(self):
        runner, _, events, ops = _runner(
            {"index.php": _html(_LOGGED_IN), "attendance.php": _redirect("/login.php")}
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(_expired(events)), 1)
        self.assertEqual(_paths(ops), ["index.php", "attendance.php"])

    def test_login_form_page_after_a_followed_redirect_is_still_expired(self):
        runner, _, events, ops = _runner(
            {
                "index.php": _redirect("/index.php?lang=zh"),
                "/index.php?lang=zh": _html("<form><input type='password'></form>"),
            }
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(_expired(events)), 1)
        self.assertEqual(_paths(ops), ["index.php", "/index.php?lang=zh"])

    # ------------------------------------------------------------------ 同站跳转：照常跟

    def test_same_site_redirect_is_followed_to_the_page(self):
        runner, _, events, ops = _runner(
            {
                "index.php": _html(_LOGGED_IN),
                "attendance.php": _redirect("/attendance.php?ref=daily"),
                "/attendance.php?ref=daily": _html("签到成功，获得 10 魔力"),
            }
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["signin"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(events.published, [])
        self.assertEqual(_paths(ops), ["index.php", "attendance.php", "/attendance.php?ref=daily"])
        followed = ops.calls[-1]  # 跟随的是同一个步骤：还是 GET、还是 web 域名
        self.assertEqual((followed["method"], followed["base"]), ("GET", "web"))

    def test_every_same_site_form_and_status_is_followed(self):
        forms = (
            "/attendance.php?ref=1",
            "attendance.php?ref=1",
            "?ref=1",
            "https://hdsky.me/attendance.php?ref=1",
            "http://hdsky.me/attendance.php?ref=1",  # http→https 之类：只看主机
            "//hdsky.me/attendance.php?ref=1",
        )
        for status in (301, 302, 303, 307, 308):
            for form in forms:
                with self.subTest(status=status, location=form):
                    runner, _, _, ops = _runner(
                        {
                            "index.php": _html(_LOGGED_IN),
                            "attendance.php": _redirect(form, status=status),
                            "/attendance.php?ref=1": _html("签到成功"),
                        }
                    )
                    result = asyncio.run(runner.run_round())
                    self.assertEqual(result["signin"], 1)
                    self.assertEqual(_paths(ops)[-1], "/attendance.php?ref=1")

    def test_trailing_slash_redirect_on_the_home_step(self):
        runner, _, _, ops = _runner(
            {"index.php": _redirect("/index.php/"), "/index.php/": _html(_LOGGED_IN)},
            config=Settings(signin_enabled=False),
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual(_paths(ops), ["index.php", "/index.php/"])

    def test_location_header_name_is_case_insensitive(self):
        runner, _, _, ops = _runner(
            {
                "index.php": _redirect("/index.php?x=1", header="Location"),
                "/index.php?x=1": _html(_LOGGED_IN),
            },
            config=Settings(signin_enabled=False),
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual(_paths(ops), ["index.php", "/index.php?x=1"])

    def test_relative_redirect_is_followed_without_the_site_domain(self):
        runner, _, _, ops = _runner(
            {"index.php": _redirect("/index.php?x=1"), "/index.php?x=1": _html(_LOGGED_IN)},
            catalog=False,
            config=Settings(signin_enabled=False),
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual(_paths(ops), ["index.php", "/index.php?x=1"])

    # ------------------------------------------------------------------ 不跟的：记失败、不置失效

    def test_cross_site_redirect_is_refused_without_expiry(self):
        for location in (
            "https://evil.example/collect",
            "//evil.example/collect",
            "https://hdsky.me.evil.example/index.php",
            "https://hdsky.me@evil.example/index.php",
        ):
            with self.subTest(location=location):
                runner, data, events, ops = _runner({"index.php": _redirect(location)})
                result = asyncio.run(runner.run_round())
                self.assertEqual(result["failed"], 1)
                self.assertIn("外部地址", result["sites"]["hdsky"]["detail"])
                self.assertEqual(events.published, [])  # 一个跳转不足以断定凭据失效
                states = st.normalize(data.store[st.STATE_KEY])
                self.assertFalse(states["hdsky"].get("alerted_expired"))
                self.assertEqual(_paths(ops), ["index.php"])  # 不向目标发任何请求

    def test_absolute_redirect_is_refused_when_the_site_domain_is_unknown(self):
        runner, _, events, ops = _runner(
            {"index.php": _redirect("https://hdsky.me/index.php?x=1")}, catalog=False
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertIn("无法确认是同站", result["sites"]["hdsky"]["detail"])
        self.assertEqual(events.published, [])
        self.assertEqual(_paths(ops), ["index.php"])

    def test_redirect_loop_is_refused(self):
        # index.php → /a?x=1 → index.php → /a?x=1：同一个跳转第二次出现才算环
        runner, _, events, ops = _runner(
            {
                "index.php": _redirect("/a?x=1"),
                "/a?x=1": _redirect("index.php"),
                "/index.php": _redirect("/a?x=1"),
            }
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertIn("环", result["sites"]["hdsky"]["detail"])
        self.assertEqual(events.published, [])
        self.assertEqual(_paths(ops), ["index.php", "/a?x=1", "/index.php"])

    def test_redirect_to_itself_forever_is_refused(self):
        runner, _, _, ops = _runner(
            {"index.php": _redirect("/index.php"), "/index.php": _redirect("/index.php")}
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertIn("环", result["sites"]["hdsky"]["detail"])
        self.assertEqual(_paths(ops), ["index.php", "/index.php"])

    def test_redirect_back_to_the_same_page_is_not_a_loop(self):
        # 做完动作再跳回同一页（先处理后跳转的写法）：第二次到那一页拿到的才是真页面
        runner, _, _, ops = _runner(
            {
                "index.php": _html(_LOGGED_IN),
                "attendance.php": _redirect("attendance.php"),
                "/attendance.php": _html("签到成功"),
            }
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["signin"], 1)
        self.assertEqual(_paths(ops), ["index.php", "attendance.php", "/attendance.php"])

    def test_bouncing_through_a_cookie_page_is_not_a_loop(self):
        # index.php → cookiecheck.php（在那儿设 cookie）→ 回 index.php：同一页出现两次，
        # 但没有哪个跳转重复
        runner, _, _, ops = _runner(
            {
                "index.php": _redirect("/cookiecheck.php"),
                "/cookiecheck.php": _redirect("/index.php"),
                "/index.php": _html(_LOGGED_IN),
            },
            config=Settings(signin_enabled=False),
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual(_paths(ops), ["index.php", "/cookiecheck.php", "/index.php"])

    @staticmethod
    def _chain(length: int) -> dict[str, Any]:
        """index.php → /h1 → … → /h<length>（最后一站是登录在线的首页）。"""
        responses: dict[str, Any] = {"index.php": _redirect("/h1")}
        for i in range(1, length):
            responses[f"/h{i}"] = _redirect(f"/h{i + 1}")
        responses[f"/h{length}"] = _html(_LOGGED_IN)
        return responses

    def test_up_to_the_hop_limit_is_followed(self):
        runner, _, _, ops = _runner(
            self._chain(MAX_REDIRECTS), config=Settings(signin_enabled=False)
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual(
            _paths(ops), ["index.php"] + [f"/h{i}" for i in range(1, MAX_REDIRECTS + 1)]
        )

    def test_more_hops_than_the_limit_are_refused(self):
        runner, _, events, ops = _runner(
            self._chain(MAX_REDIRECTS + 1), config=Settings(signin_enabled=False)
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertIn(f"超过 {MAX_REDIRECTS} 次", result["sites"]["hdsky"]["detail"])
        self.assertEqual(events.published, [])
        # 第 MAX_REDIRECTS + 1 跳不发
        self.assertEqual(
            _paths(ops), ["index.php"] + [f"/h{i}" for i in range(1, MAX_REDIRECTS + 1)]
        )

    def test_redirect_without_a_target_is_refused(self):
        for response in (
            _redirect(None),
            _redirect(""),
            _redirect(None, status=304),
        ):
            with self.subTest(status=response["status_code"], headers=response["headers"]):
                runner, _, events, ops = _runner({"index.php": response})
                result = asyncio.run(runner.run_round())
                self.assertEqual(result["failed"], 1)
                self.assertIn("没给目标地址", result["sites"]["hdsky"]["detail"])
                self.assertEqual(events.published, [])
                self.assertEqual(_paths(ops), ["index.php"])

    def test_post_redirects_are_never_followed(self):
        runner, _, _, ops = _runner(
            {"takeaction.php": _redirect("/done.php"), "/done.php": _html("不该被请求到")}
        )
        site = SiteInfo("hdsky", "HDSky", True, "cookie", base_url=_HDSKY)
        step = Step(
            PlannedRequest(method="POST", path="takeaction.php", body={"a": 1}),
            lambda response: SiteResult(Outcome.LOGIN_ONLY, "不会走到这里"),
        )
        response = asyncio.run(runner._proxy_following(site, step))
        self.assertIn("POST", response["_error"])
        self.assertEqual(_paths(ops), ["takeaction.php"])

    def test_post_redirect_to_the_login_page_still_means_expired(self):
        runner, _, _, ops = _runner({"takeaction.php": _redirect("/login.php")})
        site = SiteInfo("hdsky", "HDSky", True, "cookie", base_url=_HDSKY)
        step = Step(
            PlannedRequest(method="POST", path="takeaction.php"),
            lambda response: SiteResult(Outcome.LOGIN_ONLY, "不会走到这里"),
        )
        response = asyncio.run(runner._proxy_following(site, step))
        self.assertTrue(response["_login_redirect"])
        self.assertEqual(_paths(ops), ["takeaction.php"])

    # ------------------------------------------------------------------ 站点域名来自 site.catalog

    def test_enabled_sites_carry_the_catalog_base_url(self):
        runner, *_ = _runner({})
        sites = asyncio.run(runner.enabled_sites())
        self.assertEqual((sites[0].display_name, sites[0].base_url), ("HDSky", _HDSKY))

    def test_enabled_sites_without_a_catalog_have_no_base_url(self):
        runner, *_ = _runner({}, catalog=False)
        sites = asyncio.run(runner.enabled_sites())
        self.assertEqual((sites[0].display_name, sites[0].base_url), ("hdsky", ""))


if __name__ == "__main__":
    unittest.main()
