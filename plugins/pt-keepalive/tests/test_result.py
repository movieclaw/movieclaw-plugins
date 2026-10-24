"""三态判定与状态机的单元测试（纯函数，不启动宿主）。

判据纪律是本插件的核心：登录在线不许冒充签到成功、失效证据优先、
script/隐藏元素剥除后再匹配、幂等视为成功。这里同时放正例与反例
（反例 = 看着像但不该命中的构造，如 script 里的「签到成功」）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pt_keepalive import state as st
from pt_keepalive.result import (
    Outcome,
    judge_nexusphp_page,
    visible_text,
)


def _page(body: str, title: str = "") -> str:
    head = f"<title>{title}</title>" if title else ""
    return f"<html>{head}<body>{body}</body></html>"


_LOGGED_IN = '<a href="logout.php">退出</a>'


class TestNexusphpJudge(unittest.TestCase):
    def test_signin_success(self):
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} 签到成功，获得 50 魔力"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.SIGNIN_OK)
        self.assertFalse(r.auth_expired)

    def test_signin_already_idempotent(self):
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} 今天已经签到过了，请勿重复刷新"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.SIGNIN_OK)

    def test_negative_word_beats_success_word(self):
        # 反例：同时出现「签到成功」模板文案与「回答错误」提示，必须判失败
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} 回答错误，签到失败。恭喜您签到成功"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.FAILED)

    def test_login_form_means_expired(self):
        # 失效证据优先于页面里任何成功词
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} 签到成功 <form><input type='password'></form>"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.FAILED)
        self.assertTrue(r.auth_expired)

    def test_script_text_not_counted(self):
        # 反例：script 里的「签到成功」是模板字符串，不是页面证据
        r = judge_nexusphp_page(
            _page(f"{_LOGGED_IN} <script>var s='签到成功，获得 100 魔力'</script>"), want_signin=True
        )
        self.assertEqual(r.outcome, Outcome.LOGIN_ONLY)

    def test_hidden_text_not_counted(self):
        r = judge_nexusphp_page(
            _page(f"{_LOGGED_IN} <div style='display:none'>签到成功</div>"), want_signin=True
        )
        self.assertEqual(r.outcome, Outcome.LOGIN_ONLY)

    def test_signin_button_means_unconfirmed(self):
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} <button>立即签到</button>"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.LOGIN_ONLY)
        self.assertIn("未确认", r.detail)

    def test_want_signin_false_skips_signin_words(self):
        r = judge_nexusphp_page(_page(f"{_LOGGED_IN} 签到成功"), want_signin=False)
        self.assertEqual(r.outcome, Outcome.LOGIN_ONLY)

    def test_challenge_page(self):
        r = judge_nexusphp_page(_page("checking your browser", title="Just a moment..."), want_signin=True)
        self.assertEqual(r.outcome, Outcome.NEED_HUMAN)
        self.assertTrue(r.need_human)

    def test_challenge_words_in_script_are_not_challenge(self):
        # 反例：站点自己的 JS 里有 "just a moment" 不能当成 CF 拦截页
        r = judge_nexusphp_page(
            _page(f"{_LOGGED_IN} <script>var msg = 'just a moment';</script>"), want_signin=True
        )
        self.assertEqual(r.outcome, Outcome.LOGIN_ONLY)

    def test_http_403_is_need_human_not_expired(self):
        r = judge_nexusphp_page("<html>forbidden</html>", want_signin=True, status_code=403)
        self.assertEqual(r.outcome, Outcome.NEED_HUMAN)
        self.assertFalse(r.auth_expired)

    def test_http_500_is_failure(self):
        r = judge_nexusphp_page("<html>oops</html>", want_signin=True, status_code=500)
        self.assertEqual(r.outcome, Outcome.FAILED)

    def test_login_confirm_can_be_skipped(self):
        # 签到页没有导航栏（没有登出链接），只有密码框才算失效
        r = judge_nexusphp_page(
            _page("签到成功，获得 30 魔力"), want_signin=True, login_confirm=False
        )
        self.assertEqual(r.outcome, Outcome.SIGNIN_OK)

    def test_no_login_evidence(self):
        r = judge_nexusphp_page(_page("<p>普通游客页面</p>"), want_signin=True)
        self.assertEqual(r.outcome, Outcome.FAILED)
        self.assertFalse(r.auth_expired)

    def test_visible_text_strips_noise(self):
        text = visible_text(_page("<script>js()</script><style>.x{}</style>你好"))
        self.assertIn("你好", text)
        self.assertNotIn("js()", text)


class TestState(unittest.TestCase):
    def test_success_resets_failures_and_alert(self):
        states = st.record({}, "hdsky", "failed", "网络错误", auth_expired=False)
        states = st.record(states, "hdsky", "failed", "又挂了", auth_expired=False)
        states = st.record(states, "hdsky", "signin_ok", "签到成功", auth_expired=False)
        entry = states["hdsky"]
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertIn("last_ok_at", entry)

    def test_expired_sets_alert_flag_and_threshold(self):
        states = st.record({}, "mteam", "failed", "凭据失效", auth_expired=True, alerted=True)
        entry = states["mteam"]
        self.assertEqual(entry["consecutive_failures"], st.FAILURE_THRESHOLD)
        self.assertTrue(entry["alerted_expired"])

    def test_failed_alert_publish_is_retried_next_round(self):
        # 告警没发出去（alerted=False）时不能置位，否则这次失效永远不会再报
        states = st.record({}, "mteam", "failed", "凭据失效", auth_expired=True, alerted=False)
        self.assertFalse(states["mteam"].get("alerted_expired"))
        self.assertTrue(st.should_alert_expired(states, "mteam", True))

    def test_should_alert_only_once(self):
        states = {}
        self.assertTrue(st.should_alert_expired(states, "mteam", True))
        states = st.record(
            states, "mteam", "failed", "凭据失效", auth_expired=True, alerted=True
        )
        self.assertFalse(st.should_alert_expired(states, "mteam", True))  # 已告警
        states = st.record(states, "mteam", "signin_ok", "恢复", auth_expired=False)
        self.assertTrue(st.should_alert_expired(states, "mteam", True))  # 恢复后再失效可再告

    def test_failure_summary_threshold(self):
        states = {}
        for _ in range(st.FAILURE_THRESHOLD):
            states = st.record(states, "hdsky", "failed", "超时", auth_expired=False)
        self.assertEqual(len(st.failure_summary(states)), 1)
        # 只失败 1 次的站不进明细
        states = st.record(states, "mteam", "failed", "超时", auth_expired=False)
        self.assertEqual(len(st.failure_summary(states)), 1)

    def test_failure_summary_uses_site_name(self):
        states = st.record(
            {}, "hdsky", "failed", "超时", auth_expired=False, site_name="HDSky"
        )
        states = st.record(states, "hdsky", "failed", "超时", auth_expired=False)
        states = st.record(states, "hdsky", "failed", "超时", auth_expired=False)
        self.assertEqual(st.failure_summary(states), ["HDSky：超时"])

    def test_normalize_bad_data(self):
        self.assertEqual(st.normalize(None), {})
        self.assertEqual(st.normalize("junk"), {})
        self.assertEqual(st.normalize({"a": {"outcome": "ok"}, "b": 3}), {"a": {"outcome": "ok"}})

    def test_need_human_does_not_count(self):
        states = st.record({}, "hdsky", "need_human", "CF", auth_expired=False)
        self.assertEqual(states["hdsky"].get("consecutive_failures", 0), 0)

    def test_skipped_does_not_count(self):
        states = st.record({}, "sunnypt", "skipped", "未适配", auth_expired=False)
        self.assertEqual(states["sunnypt"].get("consecutive_failures", 0), 0)
        self.assertEqual(st.failure_summary(states), [])


if __name__ == "__main__":
    unittest.main()
