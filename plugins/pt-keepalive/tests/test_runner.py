"""站点适配器与保活轮的单元测试（mock 宿主操作，不发真实请求）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pt_keepalive import state as st
from pt_keepalive.result import Outcome
from pt_keepalive.runner import RETRY_DELAYS, KeepaliveRunner
from pt_keepalive.settings import Settings
from pt_keepalive.sites import (
    SiteInfo,
    adapter_for,
    nexusphp_adapter,
    step_response,
)

_LOGGED_IN = '<a href="logout.php">退出</a>'


def _site(site_id: str = "hdsky", *, auth_type: str = "cookie") -> SiteInfo:
    return SiteInfo(site_id=site_id, display_name=site_id.upper(), usable=True, auth_type=auth_type)


def _html(body: str, title: str = "") -> dict[str, Any]:
    head = f"<title>{title}</title>" if title else ""
    return {"status_code": 200, "text": f"<html>{head}<body>{body}</body></html>"}


def _non_daily(published: list[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
    """除日报外的事件：日报每轮都发（收不收在通知设置），告警类断言要把它滤掉。"""
    return [(name, payload) for name, payload in published if "report.daily" not in str(name)]


class TestAdapters(unittest.TestCase):
    def test_nexusphp_plan_confirms_login_then_signs_in(self):
        steps = nexusphp_adapter(_site())
        self.assertEqual([s.request.path for s in steps], ["index.php", "attendance.php"])
        self.assertTrue(all(s.request.method == "GET" for s in steps))
        self.assertTrue(steps[1].signin_only)

    def test_nexusphp_home_step_judges_login(self):
        steps = nexusphp_adapter(_site())
        result = steps[0].judge(step_response(_html(_LOGGED_IN)))
        self.assertEqual(result.outcome, Outcome.LOGIN_ONLY)

    def test_nexusphp_attendance_without_navbar(self):
        # 签到页通常没有导航栏：没有登出链接不代表失效
        steps = nexusphp_adapter(_site())
        result = steps[1].judge(step_response(_html("签到成功，获得 100 魔力")))
        self.assertEqual(result.outcome, Outcome.SIGNIN_OK)

    def test_nexusphp_missing_attendance_page_is_login_only(self):
        # 站点没有签到页（404）不是失败：登录态已由首页确认
        steps = nexusphp_adapter(_site())
        result = steps[1].judge(
            step_response({"status_code": 404, "text": "<html>not found</html>"})
        )
        self.assertEqual(result.outcome, Outcome.LOGIN_ONLY)

    def test_nexusphp_attendance_login_form_expires(self):
        steps = nexusphp_adapter(_site())
        result = steps[1].judge(
            step_response(_html("<form><input type='password'></form>"))
        )
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertTrue(result.auth_expired)

    def test_mteam_is_not_dispatched_to_nexusphp(self):
        # M-Team 自己登录（只认网页会话），不走适配器分派，也不被当成 HTML 站
        self.assertIsNone(adapter_for(_site("mteam", auth_type="APIKEY")))

    def test_dispatch_nexusphp_sites(self):
        self.assertIs(adapter_for(_site("hdsky")), nexusphp_adapter)

    def test_unadapted_api_site_is_not_nexusphp(self):
        # sunnypt 这类纯 API 站没有可判定的页面：不套 HTML 模板，本轮跳过
        self.assertIsNone(adapter_for(_site("sunnypt", auth_type="apikey")))

    def test_dispatch_accepts_host_enum_spelling(self):
        # 宿主 site.list 回的是枚举值 APIKEY（MovieClaw v0.39.0 实测），大小写不能影响分派
        self.assertIsNone(adapter_for(_site("sunnypt", auth_type="APIKEY")))
        self.assertIsNone(adapter_for(_site("sunnypt", auth_type="ApiKey")))
        self.assertIs(adapter_for(_site("hdsky", auth_type="COOKIE")), nexusphp_adapter)


class _FakeData:
    """假 PLUGIN_DATA 门面。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.secret_writes: set[str] = set()

    async def get(self, key: str, *, default: Any = None, **kw: Any) -> Any:
        return self.store.get(key, default)

    async def set(self, key: str, value: Any, *, secret: bool = False, **kw: Any) -> None:
        self.store[key] = value
        if secret:
            self.secret_writes.add(key)


class _FakeEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[Any, Any]] = []
        self.fail = fail

    async def publish(self, event: Any, payload: Any) -> None:
        if self.fail:
            raise RuntimeError("notify unavailable")
        self.published.append((getattr(event, "name", str(event)), payload))


class _FakeOpsClient:
    """假宿主操作客户端：按 (operation, site_id, path) 回放响应字典。"""

    def __init__(
        self,
        responses: dict[tuple[str, str], Any],
        *,
        auth_409: set[str] | None = None,
        sites: list[dict[str, Any]] | None = None,
        catalog: list[dict[str, Any]] | None = None,
        unavailable_409: dict[str, str] | None = None,
        unavailable_503: dict[str, int] | None = None,
    ) -> None:
        self.responses = responses
        self.auth_409 = auth_409 or set()
        self.sites = sites
        self.catalog = catalog or []
        self.unavailable_409 = unavailable_409 or {}
        #: site_id → 还要抛几次 503（宿主对超时 / 连不上回的口子）；用完就正常回放
        self.unavailable_503 = dict(unavailable_503 or {})
        self.calls: list[dict[str, Any]] = []

    def allowed(self, operation_id: str) -> bool:
        """与真实宿主操作客户端一致：只回答授权过的操作。"""
        return operation_id in {"site.list", "site.catalog", "site.proxy.request"}

    async def call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        if operation == "site.list":
            return self.sites if self.sites is not None else [
                {"site_id": sid, "display_name": sid.upper(), "usable": True, "auth_type": "cookie"}
                for sid in ("hdsky", "mteam")
            ]
        if operation == "site.catalog":
            return self.catalog
        if operation == "site.proxy.request":
            sid = str(args.get("site_id"))
            self.calls.append({"op": operation, **args})
            if self.unavailable_503.get(sid):
                from movieclaw_sdk.ops import OpsError

                self.unavailable_503[sid] -= 1
                # 与真实宿主一致（sites.py）：超时回「站点响应超时（Ns）」，
                # 其它传输错误回「无法连接站点：{exc}」
                raise OpsError(503, "SERVICE_UNAVAILABLE", "站点响应超时（30s）")
            if sid in self.auth_409:
                from movieclaw_sdk.ops import OpsError

                raise OpsError(409, "CONFLICT", "认证失败：凭据已失效")
            if sid in self.unavailable_409:
                from movieclaw_sdk.ops import OpsError

                raise OpsError(409, "CONFLICT", self.unavailable_409[sid])
            # 同一站点可能按路径给不同响应
            key = (operation, sid, str(args.get("path")))
            if key in self.responses:
                return self.responses[key]
            return self.responses[(operation, sid)]
        raise AssertionError(f"unexpected op {operation}")


class _FakeCtx:
    """假 Context：只提供 runner 需要的 use/config。"""

    def __init__(self, ops_client: _FakeOpsClient, config: Any = None) -> None:
        self._ops_client = ops_client
        self.config = config
        self.entry_id = "pt-keepalive"

    def use(self, key: Any) -> Any:
        ops_client = self._ops_client

        class _Ops:
            async def client(self, ctx: Any) -> Any:
                return ops_client

        return _Ops()


class TestRunnerRound(unittest.TestCase):
    def _runner(
        self,
        responses: dict,
        auth_409: set[str] | None = None,
        config: Any = None,
        events: _FakeEvents | None = None,
        sites: list[dict[str, Any]] | None = None,
        catalog: list[dict[str, Any]] | None = None,
        unavailable_409: dict[str, str] | None = None,
        unavailable_503: dict[str, int] | None = None,
        sleeps: list[float] | None = None,
    ):
        data = _FakeData()
        events = events or _FakeEvents()
        ops = _FakeOpsClient(
            responses,
            auth_409=auth_409,
            sites=sites,
            catalog=catalog,
            unavailable_409=unavailable_409,
            unavailable_503=unavailable_503,
        )
        ctx = _FakeCtx(ops, config)

        async def _sleep(seconds: float) -> None:
            if sleeps is not None:
                sleeps.append(seconds)

        runner = KeepaliveRunner(ctx, data=data, events=events, sleep=_sleep)
        return runner, data, events, ops

    def test_host_unavailable_site_is_skipped_not_expired(self):
        # 宿主 409「站点已停用 / 尚未验证通过」= 本轮没访问，不是凭据失效
        import asyncio

        runner, data, events, _ = self._runner(
            {},
            sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
            unavailable_409={"hdsky": "站点尚未验证通过（当前状态 failed）：hdsky"},
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(_non_daily(events.published), [])
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "skipped")
        self.assertFalse(states["hdsky"].get("alerted_expired"))

    def test_display_name_comes_from_catalog(self):
        import asyncio

        runner, _, _, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功"),
            },
            sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
            catalog=[{"site_id": "hdsky", "display_name": "HDSky"}],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["sites"]["hdsky"]["name"], "HDSky")
        states = st.normalize(result["states"])
        self.assertEqual(states["hdsky"]["site_name"], "HDSky")

    def test_round_two_nexusphp_sites(self):
        runner, data, events, ops = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功，获得 100 魔力"),
                ("site.proxy.request", "chdbits", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "chdbits", "attendance.php"): _html("签到成功，获得 20 魔力"),
            },
            sites=[
                {"site_id": "hdsky", "usable": True, "auth_type": "cookie"},
                {"site_id": "chdbits", "usable": True, "auth_type": "cookie"},
            ],
        )
        import asyncio

        result = asyncio.run(runner.run_round())
        self.assertEqual(result["signin"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(_non_daily(events.published), [])  # 无失效告警
        # 两个站都走宿主代请求（base=web）
        self.assertTrue(all(c["base"] == "web" for c in ops.calls))
        self.assertEqual(len(ops.calls), 4)

    def test_round_expired_409_alerts_once(self):
        runner, data, events, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功"),
            },
            auth_409={"hdsky"},
        )
        import asyncio

        asyncio.run(runner.run_round())
        expired = [p for name, p in events.published if getattr(p, "site_id", None) == "hdsky"]
        self.assertEqual(len(expired), 1)
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "failed")
        self.assertTrue(states["hdsky"]["alerted_expired"])
        # 宿主在首页就 409：不该再打签到页
        self.assertFalse(
            any(c.get("path") == "attendance.php" and c["site_id"] == "hdsky" for c in _.calls)
        )
        # 第二轮仍失效：不再重复告警
        asyncio.run(runner.run_round())
        expired2 = [p for name, p in events.published if getattr(p, "site_id", None) == "hdsky"]
        self.assertEqual(len(expired2), 1)

    def test_mteam_without_credentials_is_skipped(self):
        runner, data, events, ops = self._runner(
            {},
            sites=[{"site_id": "mteam", "usable": True, "auth_type": "APIKEY"}],
        )
        import asyncio

        result = asyncio.run(runner.run_round())
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(_non_daily(events.published), [])
        self.assertEqual(ops.calls, [])  # 不借宿主会话，也不发任何请求
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["mteam"]["outcome"], "skipped")

    def test_mteam_uses_saved_session_and_persists_new_one(self):
        """M-Team 走自己的登录通道：读缓存会话、把新会话写回（secret=True）。"""
        import asyncio
        from unittest import mock

        from pt_keepalive import mteam as mteam_mod
        from pt_keepalive.result import SiteResult

        seen: dict[str, Any] = {}

        class _StubClient:
            def __init__(self, *, credentials, session):
                seen["credentials"] = credentials
                seen["session_in"] = session
                self.session = mteam_mod.Session(token="NEW", did="D", visitorid="V")

            async def run(self):
                return SiteResult(Outcome.SIGNIN_OK, "最后浏览时间已刷新")

            async def aclose(self):
                return None

        runner, data, events, ops = self._runner(
            {},
            config=Settings(
                mteam_username="sh172736242",
                mteam_password="pw",
                mteam_totp_secret="JBSWY3DPEHPK3PXP",
            ),
            sites=[{"site_id": "mteam", "usable": True, "auth_type": "APIKEY"}],
        )
        data.store[mteam_mod.SESSION_KEY] = {"token": "OLD", "did": "d", "visitorid": "v"}
        with mock.patch("pt_keepalive.runner.MTeamClient", _StubClient):
            result = asyncio.run(runner.run_round())

        self.assertEqual(result["signin"], 1)
        self.assertEqual(ops.calls, [])  # 不借宿主会话
        self.assertEqual(seen["credentials"].username, "sh172736242")
        self.assertEqual(seen["session_in"].token, "OLD")  # 复用了缓存会话
        self.assertEqual(data.store[mteam_mod.SESSION_KEY]["token"], "NEW")
        self.assertIn(mteam_mod.SESSION_KEY, data.secret_writes)  # 令牌加密落盘

    def test_mteam_auth_failure_alerts_with_site_name(self):
        import asyncio
        from unittest import mock

        from pt_keepalive import mteam as mteam_mod
        from pt_keepalive.result import SiteResult

        class _StubClient:
            def __init__(self, *, credentials, session):
                self.session = mteam_mod.Session()

            async def run(self):
                return SiteResult(Outcome.FAILED, "登录被拒：密碼錯誤", auth_expired=True)

            async def aclose(self):
                return None

        runner, data, events, _ = self._runner(
            {},
            config=Settings(mteam_username="u", mteam_password="pw"),
            sites=[{"site_id": "mteam", "usable": True, "auth_type": "APIKEY"}],
            catalog=[{"site_id": "mteam", "display_name": "M-Team"}],
        )
        with mock.patch("pt_keepalive.runner.MTeamClient", _StubClient):
            asyncio.run(runner.run_round())
        alerted = [p for name, p in events.published if getattr(p, "site_id", None) == "mteam"]
        self.assertEqual(len(alerted), 1)
        self.assertEqual(alerted[0].site_name, "M-Team")

    def test_alert_publish_failure_is_retried(self):
        import asyncio
        import logging

        failing = _FakeEvents(fail=True)
        runner, data, events, _ = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN)},
            auth_409={"hdsky"},
            events=failing,
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        with self.assertLogs("movieclaw_plugins.pt_keepalive", level=logging.ERROR):
            asyncio.run(runner.run_round())
        states = st.normalize(data.store[st.STATE_KEY])
        # 告警没发出去：不置位，下一轮还能再试
        self.assertFalse(states["hdsky"].get("alerted_expired"))
        self.assertTrue(st.should_alert_expired(states, "hdsky", True))

    def test_daily_report_has_detail_text(self):
        import asyncio

        runner, _, events, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功，获得 10 魔力"),
                ("site.proxy.request", "chdbits", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "chdbits", "attendance.php"): _html("签到成功，获得 10 魔力"),
            },
            sites=[
                {"site_id": "hdsky", "usable": True, "auth_type": "cookie"},
                {"site_id": "chdbits", "usable": True, "auth_type": "cookie"},
            ],
        )
        asyncio.run(runner.run_round())
        reports = [p for name, p in events.published if "daily" in str(name)]
        self.assertEqual(len(reports), 1)
        # 通知模板用 {detail}，载荷里必须有非空字符串
        self.assertIn("在线 2 站（签到成功 2）", reports[0].detail)
        self.assertEqual(reports[0].ok_count, 2)
        self.assertEqual(reports[0].signin_count, 2)

    def test_consecutive_failures_alert_alongside_daily(self):
        # 连续失败升级独立于日报：两条都发，升级靠「同一段失败只报一次」去重
        import asyncio

        runner, data, events, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): {
                    "status_code": 500,
                    "text": "<html>oops</html>",
                }
            },
            sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
        )
        # 上一轮已经失败到阈值 - 1：这一轮再失败就升级
        data.store[st.STATE_KEY] = {
            "hdsky": {
                "outcome": "failed",
                "detail": "站点返回 HTTP 500，服务端故障",
                "consecutive_failures": st.FAILURE_THRESHOLD - 1,
            }
        }
        asyncio.run(runner.run_round())
        names = [str(name) for name, _ in events.published]
        self.assertIn("pt-keepalive.report.failures", names)
        self.assertIn("pt-keepalive.report.daily", names)  # 日报也发，失败明细带着

    def test_failure_alert_is_not_repeated_every_round(self):
        import asyncio

        runner, data, events, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): {
                    "status_code": 500,
                    "text": "<html>oops</html>",
                }
            },
            sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
        )
        data.store[st.STATE_KEY] = {
            "hdsky": {"outcome": "failed", "detail": "HTTP 500", "consecutive_failures": 2}
        }
        asyncio.run(runner.run_round())  # 第 3 轮失败：发一次
        asyncio.run(runner.run_round())  # 第 4 轮失败：不再发
        names = [str(name) for name, _ in events.published]
        self.assertEqual(names.count("pt-keepalive.report.failures"), 1)

    def test_recovered_site_can_alert_failures_again(self):
        import asyncio

        runner, data, events, _ = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): {
                    "status_code": 500,
                    "text": "<html>oops</html>",
                }
            },
            sites=[{"site_id": "hdsky", "usable": True, "auth_type": "cookie"}],
        )
        data.store[st.STATE_KEY] = {
            "hdsky": {
                "outcome": "failed",
                "detail": "HTTP 500",
                "consecutive_failures": st.FAILURE_THRESHOLD - 1,
                "failure_alerted": True,  # 同一段失败已经报过
            }
        }
        asyncio.run(runner.run_round())
        names = [str(name) for name, _ in events.published]
        self.assertNotIn("pt-keepalive.report.failures", names)
        # 恢复后告警位清零：下一次再连续失败会重新报
        states = st.normalize(data.store[st.STATE_KEY])
        states = st.record(states, "hdsky", "login_only", "登录在线", auth_expired=False)
        self.assertFalse(states["hdsky"].get("failure_alerted"))

    def test_unadapted_api_site_is_skipped_without_request(self):
        import asyncio

        runner, data, events, ops = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN)},
            sites=[
                {"site_id": "sunnypt", "display_name": "Sunny", "usable": True, "auth_type": "apikey"}
            ],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(ops.calls, [])  # 一个请求都不发
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["sunnypt"]["outcome"], "skipped")

    def test_unusable_site_is_skipped_with_reason(self):
        # 宿主还没认这个站的凭据（最常见：站点开了两步验证、宿主填不了动态码）：
        # 本轮不发请求，但要把原因和「改用 Cookie」这条路讲出来，不能一声不吭
        import asyncio

        runner, data, events, ops = self._runner(
            {},
            sites=[
                {
                    "site_id": "hdsky",
                    "display_name": "HDSky",
                    "usable": False,
                    "auth_type": "credential",
                    "last_error": "登录失败：两步验证码错误",
                }
            ],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(ops.calls, [])  # 一个请求都不发
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)  # 不是插件的失败
        detail = result["sites"]["hdsky"]["detail"]
        self.assertIn("两步验证码错误", detail)  # 宿主给的原因原样带上
        self.assertIn("Cookie", detail)  # 给出可行的做法
        self.assertEqual(_non_daily(events.published), [])  # 不发失效告警
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "skipped")

    def test_site_still_verifying_is_not_reported_as_broken(self):
        # 刚配好、宿主的验证还在跑：也说「跳过」，但换个说法，别吓人
        import asyncio

        runner, _, _, ops = self._runner(
            {},
            sites=[
                {
                    "site_id": "hdsky",
                    "display_name": "HDSky",
                    "usable": False,
                    "auth_type": "credential",
                    "status": "verifying",
                }
            ],
        )
        result = asyncio.run(runner.run_round())
        detail = result["sites"]["hdsky"]["detail"]
        self.assertIn("验证中", detail)
        self.assertNotIn("两步验证", detail)
        self.assertEqual(ops.calls, [])

    def test_disabled_site_is_not_visited(self):
        # 站点在宿主里被关掉（enabled=false）：连 M-Team 也不该保活
        import asyncio

        runner, _, _, ops = self._runner(
            {},
            config=Settings(mteam_username="u", mteam_password="pw"),
            sites=[
                {"site_id": "hdsky", "usable": True, "enabled": False, "auth_type": "cookie"},
                {"site_id": "mteam", "usable": True, "enabled": False, "auth_type": "APIKEY"},
            ],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["sites"], {})
        self.assertEqual(ops.calls, [])

    def test_mteam_unverified_still_keepalive(self):
        # 宿主把 M-Team 的 API Key 标成未验证通过，也不该拦住插件自己登录保活
        import asyncio
        from unittest import mock

        from pt_keepalive import mteam as mteam_mod
        from pt_keepalive.result import SiteResult

        class _StubClient:
            def __init__(self, *, credentials, session):
                self.session = mteam_mod.Session()

            async def run(self):
                return SiteResult(Outcome.SIGNIN_OK, "最后浏览时间已刷新")

            async def aclose(self):
                return None

        runner, _, _, _ = self._runner(
            {},
            config=Settings(mteam_username="u", mteam_password="pw"),
            sites=[{"site_id": "mteam", "usable": False, "enabled": True, "auth_type": "APIKEY"}],
        )
        with mock.patch("pt_keepalive.runner.MTeamClient", _StubClient):
            result = asyncio.run(runner.run_round())
        self.assertEqual(result["sites"]["mteam"]["outcome"], "signin_ok")

    def test_signin_disabled_still_confirms_login(self):
        import asyncio

        runner, _, _, ops = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN)},
            config=Settings(signin_enabled=False),
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["ok"], 1)
        self.assertEqual([c["path"] for c in ops.calls], ["index.php"])

    def test_need_human_page_stops_round_for_site(self):
        import asyncio

        runner, data, _, ops = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(
                    "checking your browser", title="Just a moment..."
                )
            },
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["human"], 1)
        self.assertEqual([c["path"] for c in ops.calls], ["index.php"])
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "need_human")

    def test_round_disabled_skips(self):
        runner, data, events, _ = self._runner(
            {("site.proxy.request", "hdsky"): {"text": "x"}},
            config=Settings(enabled=False),
        )
        import asyncio

        result = asyncio.run(runner.run_round())
        self.assertIn("skipped_reason", result)
        self.assertEqual(events.published, [])

    # ------------------------------------------------------------------ 日报列失败站点与原因

    def test_daily_report_lists_each_failed_site_with_reason(self):
        # issue #43：日报只说「失败 1」，得让人知道是哪个站、为什么失败
        import asyncio

        runner, _, events, _ = self._runner(
            {
                # 跳到外站：不重试的失败，正好用来单看日报怎么写
                ("site.proxy.request", "hdsky", "index.php"): {
                    "status_code": 302,
                    "headers": {"location": "https://evil.example/collect"},
                    "text": "",
                },
                ("site.proxy.request", "chdbits", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "chdbits", "attendance.php"): _html("签到成功"),
            },
            sites=[
                {"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"},
                {"site_id": "chdbits", "usable": True, "auth_type": "cookie"},
            ],
        )
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("在线 1 站（签到成功 1）", report.detail)
        self.assertIn("失败 1", report.detail)
        self.assertIn("HDSky", report.detail)  # 是哪个站
        self.assertIn("evil.example", report.detail)  # 为什么
        self.assertNotIn("chdbits", report.detail)  # 成功的站不列
        self.assertEqual(len(report.failures), 1)
        self.assertTrue(report.failures[0].startswith("HDSky："))

    def test_daily_report_marks_sites_with_consecutive_failures(self):
        import asyncio

        runner, data, events, _ = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): {"status_code": 500, "text": ""}},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        data.store[st.STATE_KEY] = {
            "hdsky": {"outcome": "failed", "detail": "HTTP 500", "consecutive_failures": 2}
        }
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("连续 3 轮", report.detail)

    def test_daily_report_explains_expired_credentials_site(self):
        # 凭据失效的站不进「连续失败」段（有自己的告警），日报里也必须说清楚它为什么失败
        import asyncio

        runner, _, events, _ = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN)},
            auth_409={"hdsky"},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("HDSky", report.detail)
        self.assertIn("凭据", report.detail)
        # 状态机为触发告警把计数置成了阈值：那不是真实轮数，不能写进日报
        self.assertNotIn("连续", report.detail)

    def test_daily_report_does_not_list_skipped_sites_as_failures(self):
        import asyncio

        runner, _, events, _ = self._runner(
            {},
            sites=[{"site_id": "sunnypt", "display_name": "Sunny", "usable": True, "auth_type": "apikey"}],
        )
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertNotIn("Sunny", report.detail)
        self.assertEqual(report.failures, [])

    # ------------------------------------------------------------------ 临时网络错误自动重试

    def test_transient_host_error_is_retried_until_it_works(self):
        import asyncio

        sleeps: list[float] = []
        runner, _, events, ops = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功"),
            },
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
            unavailable_503={"hdsky": 2},  # 前两次报超时，第三次成功
            sleeps=sleeps,
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["sites"]["hdsky"]["outcome"], "signin_ok")
        # 按 1、3 分钟退避；第三次成功就不再有等待
        self.assertEqual(sleeps, [60.0, 180.0])
        self.assertEqual(len(ops.calls), 4)  # 两次 503 + 首页 + 签到页
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertNotIn("失败", report.detail)  # 重试成功后不算失败

    def test_transient_error_still_fails_after_all_retries(self):
        import asyncio

        sleeps: list[float] = []
        runner, data, events, _ = self._runner(
            {},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
            unavailable_503={"hdsky": 99},
            sleeps=sleeps,
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(sleeps, [60.0, 180.0, 300.0])
        detail = result["sites"]["hdsky"]["detail"]
        self.assertIn("超时", detail)  # 宿主给的错误类型原样带上
        self.assertIn(f"重试 {len(RETRY_DELAYS)} 次仍失败", detail)
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("HDSky", report.detail)
        self.assertIn("超时", report.detail)
        states = st.normalize(data.store[st.STATE_KEY])
        self.assertEqual(states["hdsky"]["outcome"], "failed")

    def test_auth_failure_is_not_retried(self):
        # 凭据失效重试只会白挨一次风控：一次就够，也不等
        import asyncio

        sleeps: list[float] = []
        runner, _, _, ops = self._runner(
            {},
            auth_409={"hdsky"},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
            sleeps=sleeps,
        )
        asyncio.run(runner.run_round())
        self.assertEqual(sleeps, [])
        self.assertEqual(len(ops.calls), 1)

    def test_client_error_is_not_retried(self):
        # 4xx（路径不合法等）是插件的错，重试没有意义
        import asyncio

        class _Rejecting(_FakeOpsClient):
            async def call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
                if operation == "site.proxy.request":
                    from movieclaw_sdk.ops import OpsError

                    self.calls.append({"op": operation, **(arguments or {})})
                    raise OpsError(400, "INVALID_ARGUMENTS", "路径不允许指定主机")
                return await super().call(operation, arguments)

        sleeps: list[float] = []
        operations = _Rejecting(
            {},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )

        async def _sleep(seconds: float) -> None:
            sleeps.append(seconds)

        runner = KeepaliveRunner(
            _FakeCtx(operations), data=_FakeData(), events=_FakeEvents(), sleep=_sleep
        )
        result = asyncio.run(runner.run_round())
        self.assertEqual(sleeps, [])
        self.assertEqual(len(operations.calls), 1)
        self.assertIn("路径不允许指定主机", result["sites"]["hdsky"]["detail"])

    def test_site_server_error_is_retried(self):
        # 站点自己的 5xx（维护、网关抖动）与网络错误同类：本轮值得再试
        import asyncio

        sleeps: list[float] = []
        runner, _, _, ops = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): {
                    "status_code": 503,
                    "text": "<html>maintenance</html>",
                },
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功"),
            },
            config=Settings(signin_enabled=False),
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
            sleeps=sleeps,
        )
        asyncio.run(runner.run_round())
        self.assertEqual(sleeps, [60.0, 180.0, 300.0])
        self.assertEqual(len(ops.calls), 4)  # 首次 + 三次重试

    def test_retry_only_visits_the_failed_site_again(self):
        import asyncio

        runner, _, _, ops = self._runner(
            {
                ("site.proxy.request", "hdsky", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "hdsky", "attendance.php"): _html("签到成功"),
                ("site.proxy.request", "chdbits", "index.php"): _html(_LOGGED_IN),
                ("site.proxy.request", "chdbits", "attendance.php"): _html("签到成功"),
            },
            sites=[
                {"site_id": "hdsky", "usable": True, "auth_type": "cookie"},
                {"site_id": "chdbits", "usable": True, "auth_type": "cookie"},
            ],
            unavailable_503={"hdsky": 1},
        )
        asyncio.run(runner.run_round())
        # chdbits 只在第一遍访问两次，重试那遍只打 hdsky
        self.assertEqual(
            [c["site_id"] for c in ops.calls].count("chdbits"), 2
        )
        self.assertEqual([c["site_id"] for c in ops.calls].count("hdsky"), 3)

    def test_mteam_network_error_is_retried(self):
        # M-Team 的 ConnectTimeout 是 issue #43 的现场：临时网络错误要再试
        import asyncio
        from unittest import mock

        from pt_keepalive import mteam as mteam_mod
        from pt_keepalive.result import SiteResult

        attempts: list[int] = []

        class _StubClient:
            def __init__(self, *, credentials, session):
                self.session = mteam_mod.Session()

            async def run(self):
                attempts.append(1)
                if len(attempts) == 1:
                    return SiteResult(
                        Outcome.FAILED, "连接 M-Team 失败：连接超时（ConnectTimeout）", retryable=True
                    )
                return SiteResult(Outcome.SIGNIN_OK, "最后浏览时间已刷新")

            async def aclose(self):
                return None

        sleeps: list[float] = []
        runner, _, _, _ = self._runner(
            {},
            config=Settings(mteam_username="u", mteam_password="pw"),
            sites=[{"site_id": "mteam", "usable": True, "auth_type": "APIKEY"}],
            sleeps=sleeps,
        )
        with mock.patch("pt_keepalive.runner.MTeamClient", _StubClient):
            result = asyncio.run(runner.run_round())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [60.0])
        self.assertEqual(result["sites"]["mteam"]["outcome"], "signin_ok")

    def test_mteam_auth_error_is_not_retried(self):
        # 登录失败会扣账号尝试次数：绝不重试
        import asyncio
        from unittest import mock

        from pt_keepalive import mteam as mteam_mod
        from pt_keepalive.result import SiteResult

        attempts: list[int] = []

        class _StubClient:
            def __init__(self, *, credentials, session):
                self.session = mteam_mod.Session()

            async def run(self):
                attempts.append(1)
                return SiteResult(Outcome.FAILED, "登录被拒：密碼錯誤", auth_expired=True)

            async def aclose(self):
                return None

        sleeps: list[float] = []
        runner, _, _, _ = self._runner(
            {},
            config=Settings(mteam_username="u", mteam_password="pw"),
            sites=[{"site_id": "mteam", "usable": True, "auth_type": "APIKEY"}],
            sleeps=sleeps,
        )
        with mock.patch("pt_keepalive.runner.MTeamClient", _StubClient):
            asyncio.run(runner.run_round())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeps, [])

    # -------------------------------------------------- 逐站重试的边界（复查补的用例）

    def test_retried_result_no_longer_claims_to_be_retryable(self):
        # 已经重试完的结果不该还举着「可重试」的旗子：外层若再套一层循环会重复打
        import asyncio

        runner, data, _, _ = self._runner(
            {},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
            unavailable_503={"hdsky": 99},
        )
        results = asyncio.run(runner._run_with_retries(asyncio.run(runner.enabled_sites())))
        self.assertFalse(results["hdsky"].retryable)
        self.assertIn("重试 3 次仍失败", results["hdsky"].detail)

    def test_consecutive_failure_severity_survives_an_expired_round(self):
        # 失效那轮把计数置成阈值；之后接着数真实轮数，不能把植入值当成真的
        import asyncio

        runner, data, events, _ = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): {"status_code": 500, "text": ""}},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        data.store[st.STATE_KEY] = {
            "hdsky": {
                "outcome": "failed",
                "detail": "凭据已失效",
                "consecutive_failures": st.FAILURE_THRESHOLD,
                "failures_before_expired": 1,  # 失效前只失败过 1 轮
                "alerted_expired": True,
            }
        }
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        # 真实连续轮数是 2（不是阈值+1）
        self.assertIn("连续 2 轮", report.detail)
        self.assertNotIn(f"连续 {st.FAILURE_THRESHOLD + 1} 轮", report.detail)

    def test_expired_round_reports_its_true_count(self):
        # 失效轮自身也要说真实轮数，不能写阈值的 3
        import asyncio

        runner, _, events, _ = self._runner(
            {("site.proxy.request", "hdsky", "index.php"): {"status_code": 302, "headers": {"location": "/login.php"}}},
            sites=[{"site_id": "hdsky", "display_name": "HDSky", "usable": True, "auth_type": "cookie"}],
        )
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("HDSky", report.detail)
        self.assertNotIn("连续 3 轮", report.detail)  # 这是第一轮

    def test_site_stuck_after_threshold_is_not_silently_dropped(self):
        # 连着坏了几天、这轮变成「跳过」的站：不能让用户以为它好了
        import asyncio

        runner, data, events, _ = self._runner(
            {},
            sites=[
                {
                    "site_id": "hdsky",
                    "display_name": "HDSky",
                    "usable": False,
                    "auth_type": "credential",
                    "status": "verifying",
                }
            ],
        )
        data.store[st.STATE_KEY] = {
            "hdsky": {"outcome": "failed", "detail": "HTTP 500", "consecutive_failures": 3}
        }
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("HDSky", report.detail)
        self.assertIn("此前已连续失败 3 轮", report.detail)

    def test_long_failure_list_is_capped_before_the_push_limit(self):
        # 宿主推送载荷上限 2200 字节，超了会被静默截断：插件自己先说清「另有 N 站」
        import asyncio

        from pt_keepalive.runner import MAX_REPORT_ITEMS, MAX_DETAIL_CHARS

        sites = [
            {"site_id": f"site{i}", "display_name": f"Site{i}", "usable": True, "auth_type": "cookie"}
            for i in range(30)
        ]
        runner, _, events, _ = self._runner(
            {},
            sites=sites,
            unavailable_503={f"site{i}": 99 for i in range(30)},
        )
        asyncio.run(runner.run_round())
        report = next(p for name, p in events.published if "daily" in str(name))
        self.assertIn("另有 22 站", report.detail)  # 30 - MAX_REPORT_ITEMS
        self.assertIn(f"Site{MAX_REPORT_ITEMS - 1}", report.detail)
        self.assertNotIn(f"Site{MAX_REPORT_ITEMS}", report.detail)
        self.assertLessEqual(len(report.detail), MAX_DETAIL_CHARS)
        self.assertLess(len(report.detail.encode("utf-8")), 2200)  # 宿主的明文上限


if __name__ == "__main__":
    unittest.main()
