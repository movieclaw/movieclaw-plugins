"""M-Team 保活的单元测试：签名、TOTP、响应判定、登录流程（全部假响应，不连站点）。

真站点行为（2026-10-20 实测）都有对应用例：

- ``/api/login`` 无 TOTP 时回 ``{code:1001}``，补上 ``otpCode`` 才 ``SUCCESS``；
- ``/api/member/updateLastBrowse`` 用会话 JWT 调返回 ``{code:0,SUCCESS}``；
  短时间再调一次返回 ``{code:"1",message:"FAIL"}``（站点认为无需刷新，**没刷新**）；
- 用宿主的 API Key 调它返回 ``{code:401,"Full authentication is required..."}``；
- 签名密钥不对返回 ``{code:1,"簽名錯誤"}``；连续登录返回 ``{code:1,"請求過於頻繁"}``。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pt_keepalive.mteam import (
    MTeamClient,
    Reply,
    Session,
    classify,
    classify_update,
    credentials,
    normalize_totp_secret,
    sign,
    totp_code,
)
from pt_keepalive.result import Outcome

#: RFC 6238 的公开种子（Base32）。用它算出的码只看「算法对不对」
_RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class _FakeResponse:
    def __init__(self, payload: dict, headers: dict[str, str] | None = None) -> None:
        import json

        self.text = json.dumps(payload, ensure_ascii=False)
        self.headers = headers or {}


class _FakeHttp:
    """按路径回放响应；记录每次请求的 path / form / headers。"""

    def __init__(self, script: dict[str, list[_FakeResponse]]) -> None:
        self.script = {path: list(items) for path, items in script.items()}
        self.calls: list[dict] = []

    async def post(self, url: str, *, headers: dict, data: dict) -> _FakeResponse:
        path = url.split("m-team.io", 1)[-1]
        self.calls.append({"path": path, "form": dict(data), "headers": dict(headers)})
        queue = self.script.get(path) or []
        if not queue:
            raise AssertionError(f"没有为 {path} 准备响应")
        return queue.pop(0)

    async def aclose(self) -> None:
        return None


def _client(script: dict[str, list[_FakeResponse]], *, totp: str = _RFC_SECRET) -> MTeamClient:
    client = MTeamClient(
        credentials=credentials("sh172736242", "pw", totp),
        transport=object(),  # 只看 _http，transport 不会被用到
    )
    client._http = _FakeHttp(script)
    assert client.credentials is not None
    return client


class TestSigning(unittest.TestCase):
    def test_signature_matches_site_algorithm(self):
        # base64(HMAC-SHA1("POST&/api/login&<ms>", "HLkPcWmycL57mfJt"))
        ts = 1_760_000_000_000
        expected = base64.b64encode(
            hmac.new(b"HLkPcWmycL57mfJt", f"POST&/api/login&{ts}".encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(sign("POST", "/api/login", ts), expected)

    def test_signature_depends_on_method_path_and_time(self):
        self.assertNotEqual(sign("POST", "/api/login", 1), sign("GET", "/api/login", 1))
        self.assertNotEqual(sign("POST", "/api/login", 1), sign("POST", "/api/login", 2))


class TestTotp(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238 附录 B：T=59 时 8 位码 94287082，取后 6 位 = 287082
        self.assertEqual(totp_code(_RFC_SECRET, at=59), "287082")

    def test_normalize_secret_forms(self):
        base = "JBSWY3DPEHPK3PXP"
        self.assertEqual(normalize_totp_secret(base), base)
        self.assertEqual(normalize_totp_secret(base.lower()), base)
        self.assertEqual(normalize_totp_secret(" JBSW Y3DP EHPK 3PXP "), base)
        self.assertEqual(normalize_totp_secret(f"otpauth://totp/M-Team:x?secret={base}&issuer=M-Team"), base)
        self.assertEqual(normalize_totp_secret(f"{base}===="), base)

    def test_code_is_six_digits(self):
        code = totp_code(_RFC_SECRET)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())


class TestClassify(unittest.TestCase):
    def test_success(self):
        self.assertIs(classify({"code": "0", "message": "SUCCESS"}).reply, Reply.OK)
        self.assertIs(classify({"code": 0, "message": "SUCCESS"}).reply, Reply.OK)

    def test_need_totp(self):
        self.assertIs(classify({"code": 1001, "message": "需要二次驗證"}).reply, Reply.NEED_TOTP)

    def test_api_key_only_is_auth_failure(self):
        # 宿主的 API Key 调不动这个接口，站点回 401
        verdict = classify({"code": 401, "message": "Full authentication is required to access this resource"})
        self.assertIs(verdict.reply, Reply.AUTH)

    def test_http_401_beats_success_looking_body(self):
        self.assertIs(classify({"code": "0"}, status_code=401).reply, Reply.AUTH)

    def test_invalid_request_is_auth_failure(self):
        # JWT 不对时站点回「無效的請求」
        self.assertIs(classify({"code": 1, "message": "無效的請求"}).reply, Reply.AUTH)

    def test_wrong_password(self):
        self.assertIs(
            classify({"code": 1, "message": "用戶名或密碼錯誤，您還有7次機會"}).reply, Reply.AUTH
        )

    def test_signature_rotation_is_not_auth_failure(self):
        # 密钥轮换要按「插件需要更新」报，不能当成用户凭据失效去打搅用户
        self.assertIs(classify({"code": 1, "message": "簽名錯誤"}).reply, Reply.SIGNATURE)

    def test_rate_limit_is_not_failure_of_account(self):
        self.assertIs(classify({"code": 1, "message": "請求過於頻繁"}).reply, Reply.RATE)

    def test_unknown(self):
        self.assertIs(classify({"code": 999, "message": "???"}).reply, Reply.UNKNOWN)
        self.assertIs(classify({"_text": "<html>"}).reply, Reply.UNKNOWN)

    def test_update_fail_means_no_change_not_success(self):
        # 站点认为无需刷新：{code:"1", message:"FAIL"}——它没刷新，也不是失效
        self.assertIs(classify_update({"code": "1", "message": "FAIL"}).reply, Reply.NO_CHANGE)
        self.assertIs(classify_update({"code": 1, "message": "FAIL"}).reply, Reply.NO_CHANGE)
        # 其余判定与普通接口一致
        self.assertIs(classify_update({"code": "0", "message": "SUCCESS"}).reply, Reply.OK)
        self.assertIs(classify_update({"code": 401, "message": "Full authentication"}).reply, Reply.AUTH)


class TestCredentials(unittest.TestCase):
    def test_empty_username_or_password_means_unconfigured(self):
        self.assertIsNone(credentials("", "pw"))
        self.assertIsNone(credentials("user", ""))
        self.assertIsNone(credentials("  ", "pw"))

    def test_values_are_trimmed(self):
        creds = credentials(" user ", "pw", " SECRET ")
        assert creds is not None
        self.assertEqual(creds.username, "user")
        self.assertEqual(creds.totp_secret, "SECRET")


class TestSession(unittest.TestCase):
    def test_prepare_keeps_existing_fingerprint(self):
        session = Session(visitorid="v1", did="d1")
        session.prepare()
        self.assertEqual((session.visitorid, session.did), ("v1", "d1"))

    def test_prepare_generates_missing(self):
        session = Session()
        session.prepare()
        self.assertEqual(len(session.visitorid), 32)
        self.assertEqual(len(session.did), 32)
        # 再跑一次不换指纹（每轮换设备会被风控盯上）
        first = session.visitorid
        session.prepare()
        self.assertEqual(session.visitorid, first)

    def test_roundtrip_ignores_junk(self):
        self.assertEqual(Session.from_dict(None).token, "")
        self.assertEqual(Session.from_dict("junk").token, "")
        session = Session(token="t", did="d", visitorid="v")
        self.assertEqual(Session.from_dict(session.to_dict()), session)


class TestBadTotpSecret(unittest.TestCase):
    def test_invalid_secret_is_explained_not_crashed(self):
        # 用户把手机号 / 密码填进 TOTP 那一栏：必须给一句人话，不能抛 traceback
        with self.assertRaises(ValueError):
            totp_code("18800001234")

    def test_client_reports_bad_secret(self):
        client = _client(
            {"/api/login": [_FakeResponse({"code": 1001, "message": "需要二次驗證"})]},
            totp="18800001234",
        )
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertIn("两步验证密钥填错了", result.detail)


class TestClientFlow(unittest.TestCase):
    def test_login_with_totp_then_refresh(self):
        script = {
            "/api/login": [
                _FakeResponse({"code": 1001, "message": "需要二次驗證"}),
                _FakeResponse({"code": 0, "message": "SUCCESS"}, {"Authorization": "JWT", "Did": "DID2"}),
            ],
            "/api/member/updateLastBrowse": [
                _FakeResponse({"code": "0", "message": "SUCCESS", "data": None})
            ],
        }
        client = _client(script)
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.SIGNIN_OK)
        self.assertEqual(client.session.token, "JWT")
        self.assertEqual(client.session.did, "DID2")  # 服务端滚动的 Did 要跟着更新
        login_calls = [c for c in client._http.calls if c["path"] == "/api/login"]
        self.assertEqual(len(login_calls), 2)
        self.assertNotIn("otpCode", login_calls[0]["form"])  # 第一次不带码
        self.assertIn("otpCode", login_calls[1]["form"])
        # 每个请求都要带签名与时间戳
        for call in client._http.calls:
            self.assertIn("_sgin", call["form"])
            self.assertIn("_timestamp", call["form"])

    def test_no_change_is_login_only(self):
        # 站点认为无需刷新（本机还跑着 mtlogin 时每轮都会撞上）
        client = _client(
            {
                "/api/member/profile": [_FakeResponse({"code": 0, "data": {}})],
                "/api/member/updateLastBrowse": [_FakeResponse({"code": "1", "message": "FAIL"})],
            }
        )
        client.session.token = "JWT"
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.LOGIN_ONLY)
        self.assertFalse(result.auth_expired)
        self.assertIn("无需刷新", result.detail)

    def test_expired_token_relogins_once(self):
        script = {
            "/api/member/profile": [
                _FakeResponse({"code": 401, "message": "Full authentication is required..."}),
                _FakeResponse({"code": 0, "data": {}}),
            ],
            "/api/login": [_FakeResponse({"code": 0, "message": "SUCCESS"}, {"Authorization": "NEW"})],
            "/api/member/updateLastBrowse": [_FakeResponse({"code": 0, "message": "SUCCESS"})],
        }
        client = _client(script)
        client.session.token = "OLD"
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.SIGNIN_OK)
        self.assertEqual(client.session.token, "NEW")

    def test_wrong_credentials_is_auth_expired(self):
        client = _client(
            {"/api/login": [_FakeResponse({"code": 1, "message": "用戶名或密碼錯誤，您還有7次機會"})]}
        )
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertTrue(result.auth_expired)
        # 登录失败会消耗账号的尝试次数：不许重试
        self.assertEqual(len(client._http.calls), 1)

    def test_missing_totp_secret_explains_where_to_fill(self):
        client = _client(
            {"/api/login": [_FakeResponse({"code": 1001, "message": "需要二次驗證"})]},
            totp="",
        )
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertIn("TOTP", result.detail)
        self.assertEqual(len(client._http.calls), 1)

    def test_signature_rotation_is_not_reported_as_expired(self):
        client = _client(
            {"/api/login": [_FakeResponse({"code": 1, "message": "簽名錯誤"})]},
        )
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertFalse(result.auth_expired)
        self.assertIn("签名", result.detail)

    def test_rate_limited_is_not_reported_as_expired(self):
        client = _client(
            {"/api/login": [_FakeResponse({"code": 1, "message": "請求過於頻繁"})]},
        )
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertFalse(result.auth_expired)

    def test_login_without_token_header_is_reported(self):
        client = _client({"/api/login": [_FakeResponse({"code": 0, "message": "SUCCESS"})]})
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertIn("Authorization", result.detail)

    def test_network_error_is_failure_not_expired(self):
        class _Boom:
            async def post(self, *a, **kw):
                import httpx

                raise httpx.ConnectError("boom")

            async def aclose(self):
                return None

        client = _client({})
        client._http = _Boom()
        result = asyncio.run(client.run())
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertFalse(result.auth_expired)
        self.assertIn("连接 M-Team 失败", result.detail)

    def test_did_from_every_response_is_kept(self):
        client = _client(
            {
                "/api/member/profile": [_FakeResponse({"code": 0}, {"Did": "DID-PROFILE"})],
                "/api/member/updateLastBrowse": [_FakeResponse({"code": 0}, {"Did": "DID-UPDATE"})],
            }
        )
        client.session.token = "JWT"
        client.session.did = "OLD"
        asyncio.run(client.run())
        self.assertEqual(client.session.did, "DID-UPDATE")


if __name__ == "__main__":
    unittest.main()
