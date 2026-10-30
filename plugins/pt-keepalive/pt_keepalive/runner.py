"""保活轮：site.list 取启用站点 → 逐站适配 → 代请求 → 三态判定 → 状态机 → 事件。

站点请求分两条路：

- NexusPHP 系站点经宿主 ``site.proxy.request`` 代发（借宿主已认证会话，插件接触不到
  凭据），请求节奏由宿主每站限流兜底，这里逐站串行即可；宿主不跟随站点的 3xx，
  同站的跳转由插件自己再发一次站内路径的请求（见 :meth:`KeepaliveRunner._proxy_following`）；
- M-Team 用插件设置里保存的账号密码 + TOTP 自己登录（它的保活接口只认网页会话），
  会话（JWT + 设备指纹）存插件数据，下一轮复用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from movieclaw_sdk.ops import HOST_OPS, OpsError

from pt_keepalive import state as st
from pt_keepalive.events import (
    AUTH_EXPIRED,
    DAILY_REPORT,
    FAILURE_REPORT,
    AuthExpired,
    DailyReport,
)
from pt_keepalive.mteam import SESSION_KEY, MTeamClient, Session
from pt_keepalive.result import Outcome, SiteResult
from pt_keepalive.settings import Settings, mteam_credentials
from pt_keepalive.sites import (
    MAX_REDIRECTS,
    SiteInfo,
    Step,
    adapter_for,
    is_login_location,
    redirect_location,
    redirect_target,
    step_response,
)

logger = logging.getLogger("movieclaw_plugins.pt_keepalive")

#: 自己登录的站点（不走宿主代请求）。键是宿主 ``site.list`` 的 site_id
SELF_LOGIN_SITES = ("mteam",)

#: 宿主 409 且消息含这些特征 → 本轮没访问（站点未配置 / 停用 / 还没验证通过）
_HOST_SKIP_MARKS = ("未配置", "已停用", "尚未验证通过")

#: 宿主 409 且消息含这些特征 → 已认证会话失效
_HOST_AUTH_MARKS = ("认证失败",)

#: 临时网络错误的重试间隔（秒）：1、3、5 分钟——站点抖动、DNS 抽风、代理换线路
#: 多半在十分钟内自己好；隔得太短打过去只会再撞一次同一个故障。
RETRY_DELAYS = (60.0, 180.0, 300.0)

#: 日报明细最多列几站（宿主推送载荷 2200 字节上限，超了会被静默截断）
MAX_REPORT_ITEMS = 8

#: 日报正文的兜底长度上限（字符）：极端长的 detail 也不至于把整条通知撑爆
MAX_DETAIL_CHARS = 900

#: 状态机单键（全部站点汇总 JSON）
STATE_KEY = st.STATE_KEY


class KeepaliveRunner:
    """一轮保活的执行器；由入口构造（注入 data/events 门面）并供任务处理器调用。"""

    def __init__(
        self,
        ctx: Any,
        *,
        data: Any,
        events: Any,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.ctx = ctx
        raw = getattr(ctx, "config", None)
        self.config: Settings = (
            raw if isinstance(raw, Settings) else Settings.model_validate(raw or {})
        )
        self.data = data  # PLUGIN_DATA.scoped(ctx)：get(key, default=) / set(key, value)
        self.events = events  # PLUGIN_EVENTS.register() 返回的 Publisher
        self.sleep = sleep or asyncio.sleep  # 重试间隔；单测注入不真等的实现
        self.ops: Any = None

    async def _call(self, operation: str, **arguments: Any) -> Any:
        if self.ops is None:
            self.ops = await self.ctx.use(HOST_OPS).client(self.ctx)
        return await self.ops.call(operation, arguments)

    # ------------------------------------------------------------------ 站点清单

    async def enabled_sites(self) -> list[SiteInfo]:
        """宿主已启用的站点（``enabled`` 为真）；带上能不能用、以及不能用时的原因。

        不能用的站点也**照收**，本轮记「跳过」并把宿主给的失败原因讲出来——否则
        用户配了个 NexusPHP 站、宿主因为两步验证验证不过，插件一声不吭，人会以为
        保活在跑。真正要做的判断在 :meth:`run_site` 里。

        自己登录的站点（M-Team）更是不看 ``usable``：它不用宿主那份 API Key 凭据，
        宿主标「未验证通过」不影响插件保活。
        """
        rows = await self._call("site.list")
        catalog = await self._catalog()
        sites: list[SiteInfo] = []
        for row in rows or []:
            if not isinstance(row, dict) or row.get("enabled") is False:
                continue
            site_id = str(row.get("site_id"))
            entry = catalog.get(site_id, {})
            sites.append(
                SiteInfo(
                    site_id=site_id,
                    display_name=str(
                        row.get("display_name") or entry.get("display_name") or site_id
                    ),
                    usable=bool(row.get("usable")),
                    auth_type=str(row.get("auth_type") or ""),
                    status=str(row.get("status") or ""),
                    last_error=str(row.get("last_error") or ""),
                    base_url=entry.get("base_url", ""),
                )
            )
        return sites

    async def _catalog(self) -> dict[str, dict[str, str]]:
        """site.catalog 的 site_id → 展示名（``display_name``）与站点根地址（``base_url``）。

        ``site.list`` 只回已配置站点、没有展示名也没有域名：展示名是给用户看的差别
        （``hdsky`` 还是 ``HDSky``），域名用来判断 3xx 是不是同站跳转。拿不到目录就退回
        site_id、且只跟相对路径的跳转，都不影响保活本身。
        """
        try:
            if self.ops is None:
                self.ops = await self.ctx.use(HOST_OPS).client(self.ctx)
            if not self.ops.allowed("site.catalog"):
                return {}
            rows = await self.ops.call("site.catalog", {})
        except Exception:  # 展示名与域名都是锦上添花，失败不拖垮整轮
            logger.debug("读取站点目录失败，展示名退回 site_id", exc_info=True)
            return {}
        return {
            str(row.get("site_id")): {
                "display_name": str(row.get("display_name") or ""),
                "base_url": str(row.get("base_url") or ""),
            }
            for row in rows or []
            if isinstance(row, dict)
        }

    # ------------------------------------------------------------------ 单站执行

    async def _proxy_request(self, site: SiteInfo, step: Step, path: str | None = None) -> dict:
        """经宿主代发一个请求；把宿主的 409 语义翻译成统一响应形态。

        ``path`` 缺省用步骤自己的，跟随跳转时换成跳转目标。
        """
        plan = step.request
        try:
            return await self._call(
                "site.proxy.request",
                site_id=site.site_id,
                method=plan.method,
                path=path or plan.path,
                base=plan.base,
                headers=plan.headers,
                body=plan.body,
            ) or {}
        except OpsError as exc:
            message = str(exc.message or exc)
            if exc.status == 409 and any(m in message for m in _HOST_SKIP_MARKS):
                # 站点没配置 / 停用 / 还没验证通过：本轮没访问，不是失效
                return {"_skip": message}
            if exc.status == 409 and any(m in message for m in _HOST_AUTH_MARKS):
                return {"_auth_expired": True}
            # 宿主把超时 / 连不上 / 网关错误统一回 503（其余 5xx 也可能是站点自己的故障）：
            # 这类是临时的，本轮按间隔再试；凭据、限流、参数错误重试没有意义
            transient = exc.status >= 500
            logger.warning(
                "站点 %s 代请求被宿主拒绝（%s）：%s",
                site.site_id,
                "临时错误，稍后重试" if transient else "不重试",
                message,
            )
            return {"_error": message, "_retryable": transient}

    async def _proxy_following(self, site: SiteInfo, step: Step) -> dict:
        """代发请求；站点回 3xx 时自己决定跟不跟（宿主不跟随，原样回 ``Location``）。

        - 跳到登录页 → ``_login_redirect``：会话失效，同「页面出现登录表单」；
        - 同站 → 对那个站内路径再发一次 GET，最多跟 ``MAX_REDIRECTS`` 跳；
        - 其余（跨主机、环、超限、没给目标、非 GET）→ ``_error`` 说明原因：不跟，
          也不置失效——一个跳转不足以断定凭据失效。
        """
        plan = step.request
        path = plan.path
        # 按「从哪跳到哪」去重，不按页面去重：做完动作跳回同一页、或跳去设个 cookie 再跳回来，
        # 都是正常流程（第二次到那一页拿到的才是真页面），同一个跳转出现第二次才是环
        seen: set[tuple[str, str]] = set()
        hops = 0
        while True:
            response = await self._proxy_request(site, step, path)
            status = step_response(response).status_code
            if not 300 <= status < 400:
                return response
            location = redirect_location(response)
            if is_login_location(location):
                return {"_login_redirect": True}
            if plan.method != "GET":
                return {"_error": f"站点对 {plan.method} 请求回了 HTTP {status} 跳转，不跟随"}
            if not location:
                return {"_error": f"站点回了 HTTP {status} 跳转，却没给目标地址"}
            target, reason = redirect_target(location, path, site.base_url)
            if target is None:
                return {"_error": reason}
            hop = ("/" + path.lstrip("/"), target)
            if hop in seen:
                return {"_error": "站点的跳转形成了环，已放弃"}
            if hops == MAX_REDIRECTS:
                return {"_error": f"站点连续跳转超过 {MAX_REDIRECTS} 次，已放弃"}
            logger.debug("站点 %s 跳转 %s → %s", site.site_id, path, target)
            hops += 1
            seen.add(hop)
            path = target

    async def run_site(self, site: SiteInfo) -> SiteResult:
        """跑单站：自己登录的站点走 M-Team 通道，其余按适配器逐步代请求。"""
        if site.site_id in SELF_LOGIN_SITES:
            return await self.run_self_login_site(site)
        if not site.usable:
            # 宿主还没认这个站的凭据，插件借不到会话：本轮不发请求。两种情形分开讲，
            # 别把「刚配好、还在验证」说成故障。
            if site.status in ("pending", "verifying"):
                return SiteResult(Outcome.SKIPPED, "宿主的站点凭据还在验证中，下一轮再看")
            reason = site.last_error or "尚未验证通过"
            return SiteResult(
                Outcome.SKIPPED,
                f"宿主的站点凭据不可用（{reason}）：请到「设置 → 站点」更新凭据并重新验证；"
                "若该站开了两步验证，改用 Cookie 方式即可绕开",
            )
        adapter = adapter_for(site)
        if adapter is None:
            return SiteResult(
                Outcome.SKIPPED,
                f"未适配的 API 站点（{site.auth_type or '未知认证'}），本轮跳过",
            )
        steps = [
            step
            for step in adapter(site)
            if not (step.signin_only and not self.config.signin_enabled)
        ]
        if not steps:
            return SiteResult(Outcome.SKIPPED, "没有要执行的动作（签到已关闭）")

        last: SiteResult | None = None
        for step in steps:
            response = await self._proxy_following(site, step)
            if response.get("_skip"):
                last = SiteResult(Outcome.SKIPPED, f"宿主判定站点当前不可访问：{response['_skip']}")
                break
            if response.get("_auth_expired"):
                last = SiteResult(
                    Outcome.FAILED,
                    "宿主判定站点凭据不可用（请更新凭据并重新验证）",
                    auth_expired=True,
                )
                break
            if response.get("_login_redirect"):
                last = SiteResult(
                    Outcome.FAILED, "站点跳转到了登录页，凭据已失效", auth_expired=True
                )
                break
            if response.get("_error"):
                last = SiteResult(
                    Outcome.FAILED,
                    f"代请求失败：{response['_error']}",
                    retryable=bool(response.get("_retryable")),
                )
                break
            last = step.judge(step_response(response))
            # 首页没登录证据就不必再试签到页（后续步骤只会更糟）
            if last.outcome in (Outcome.FAILED, Outcome.NEED_HUMAN):
                break
        assert last is not None
        return last

    # ------------------------------------------------------------------ 自己登录的站点

    async def run_self_login_site(self, site: SiteInfo) -> SiteResult:
        """M-Team：账号密码 + TOTP 登录网页会话，再刷新最后浏览时间。

        会话（JWT + 设备指纹）存插件数据并跨轮复用：既能少走登录（登录失败会消耗
        账号的尝试次数），也保住服务端认可的 ``Did``（每轮换设备会被风控盯上）。
        """
        creds = mteam_credentials(self.config)
        if creds is None:
            return SiteResult(
                Outcome.SKIPPED,
                "没有配置 M-Team 账号密码（设置里填好账号、密码与两步验证密钥才会做这一站）",
            )
        raw_session = await self.data.get(SESSION_KEY, default=None)
        client = MTeamClient(credentials=creds, session=Session.from_dict(raw_session))
        try:
            result = await client.run()
        finally:
            await client.aclose()
        await self.data.set(SESSION_KEY, client.session.to_dict(), secret=True)
        return result

    # ------------------------------------------------------------------ 整轮

    async def _run_sites(self, sites: list[SiteInfo]) -> dict[str, SiteResult]:
        """跑一遍全部站点；单站异常不拖垮整轮。"""
        results: dict[str, SiteResult] = {}
        for site in sites:
            try:
                results[site.site_id] = await self.run_site(site)
            except Exception:
                logger.exception("站点 %s 保活轮异常", site.site_id)
                results[site.site_id] = SiteResult(Outcome.FAILED, "执行异常，见插件日志")
        return results

    async def _run_with_retries(self, sites: list[SiteInfo]) -> dict[str, SiteResult]:
        """整轮跑完后再统一重试临时网络错误，最多 :data:`RETRY_DELAYS` 轮。

        只重试本轮判成临时错误（超时、连不上、连接被重置）的站点：登录失败、站点改版、
        限流重试只会白挨一次风控，M-Team 登录失败还扣账号尝试次数。整轮统一重试而非
        逐站重试——等待总时长（最长 :data:`RETRY_DELAYS` 之和）不随站点数增长。
        """
        results = await self._run_sites(sites)
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            retry_ids = [sid for sid, result in results.items() if result.retryable]
            if not retry_ids:
                return results
            logger.warning(
                "站点 %s 遇到临时错误，%g 分钟后第 %d 次重试（最多 %d 次）",
                "、".join(retry_ids),
                delay / 60,
                attempt,
                len(RETRY_DELAYS),
            )
            await self.sleep(delay)
            for site in sites:
                if site.site_id not in retry_ids:
                    continue
                try:
                    results[site.site_id] = await self.run_site(site)
                except Exception:
                    logger.exception("站点 %s 重试异常", site.site_id)
                    results[site.site_id] = SiteResult(Outcome.FAILED, "执行异常，见插件日志")
        return {
            sid: (
                replace(
                    result,
                    detail=f"{result.detail}（本轮已重试 {len(RETRY_DELAYS)} 次仍失败）",
                    retryable=False,
                )
                if result.retryable
                else result
            )
            for sid, result in results.items()
        }

    async def run_round(self) -> dict:
        """一轮：全部站点跑完，更新状态机，发事件。返回汇总（供 status 路由/测试）。"""
        summary: dict[str, Any] = {
            "ok": 0,
            "signin": 0,
            "failed": 0,
            "human": 0,
            "skipped": 0,
            "sites": {},
            "ran_at": st.now_iso(),
        }
        if not self.config.enabled:
            summary["skipped_reason"] = "插件已停用（设置里总开关关闭）"
            return summary
        states = st.normalize(await self.data.get(STATE_KEY, default=None))
        sites = await self.enabled_sites()
        results = await self._run_with_retries(sites)
        for site in sites:
            result = results[site.site_id]

            # 先发告警、再按「告警是否发出去」落状态：发失败下一轮还能重试，
            # 不会因为状态机先置位而永远丢掉这次告警。
            alert = st.should_alert_expired(states, site.site_id, result.auth_expired)
            alerted: bool | None = None
            if alert:
                try:
                    await self.events.publish(
                        AUTH_EXPIRED, AuthExpired(site_id=site.site_id, site_name=site.display_name)
                    )
                    alerted = True
                except Exception:  # 通知失败不该丢掉站点结果
                    logger.exception("站点 %s 失效告警发送失败", site.site_id)
                    alerted = False

            states = st.record(
                states,
                site.site_id,
                result.outcome.value,
                result.detail,
                auth_expired=result.auth_expired,
                site_name=site.display_name,
                alerted=alerted,
            )
            summary["sites"][site.site_id] = {
                "name": site.display_name,
                "outcome": result.outcome.value,
                "detail": result.detail,
            }
            if result.outcome == Outcome.SIGNIN_OK:
                summary["signin"] += 1
                summary["ok"] += 1
            elif result.outcome == Outcome.LOGIN_ONLY:
                summary["ok"] += 1
            elif result.outcome == Outcome.NEED_HUMAN:
                summary["human"] += 1
            elif result.outcome == Outcome.SKIPPED:
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
        await self.data.set(STATE_KEY, states)

        # 日报里列全「本轮每个失败的站 + 原因」：汇总数字说不出是哪个站、为什么
        # （issue #43）。连续失败轮数够阈值时在后面标出来，取代原来单列的「连续失败」段
        # ——那些站本来就在这一轮失败了，列两遍只是噪音。
        this_round = _failure_lines(sites, results, states)
        # 日报每轮都发；收不收由「账号 → 通知」里每个人自己的开关决定（默认关），
        # 插件里不留第二个开关——两个开关互不知道，都得开才有日报，关哪个都静默无声。
        await self._publish_report(
            summary,
            DAILY_REPORT,
            failures=this_round,
            sections=(("失败", this_round),),
        )
        pending = st.failure_summary(states, pending_only=True)
        if pending:
            # 连续失败升级不依赖日报：同一段失败只报一次
            await self._publish_report(
                summary,
                FAILURE_REPORT,
                failures=pending,
                sections=(("连续失败", pending),),
            )
            states = st.mark_failure_alerted(states)
            await self.data.set(STATE_KEY, states)
        summary["states"] = states
        logger.info(
            "保活轮完成：在线 %d（签到成功 %d）失败 %d 需人工 %d 跳过 %d",
            summary["ok"],
            summary["signin"],
            summary["failed"],
            summary["human"],
            summary["skipped"],
        )
        return summary

    async def _publish_report(
        self,
        summary: dict[str, Any],
        event: Any,
        *,
        failures: list[str],
        sections: tuple[tuple[str, list[str]], ...] = (),
    ) -> None:
        """发一条汇总事件；通知失败不影响本轮结果落库。

        ``failures`` 是结构化明细（``DailyReport.failures``，给 Webhook / 别的插件）；
        ``sections`` 是 ``（小标题，明细行）`` 顺序，正文按它拼——通知模板只做字段替换。
        """
        try:
            await self.events.publish(
                event,
                DailyReport(
                    ok_count=summary["ok"],
                    signin_count=summary["signin"],
                    failed_count=summary["failed"],
                    human_count=summary["human"],
                    skipped_count=summary["skipped"],
                    failures=failures,
                    detail=_report_detail(summary, sections),
                ),
            )
        except Exception:  # 通知失败不影响本轮结果落库
            logger.exception("保活汇总事件 %s 发送失败", getattr(event, "name", event))

    # ------------------------------------------------------------------ 状态查询（status 路由用）

    async def status(self) -> dict[str, Any]:
        """各站最近一轮结果（不发起请求）。"""
        states = st.normalize(await self.data.get(STATE_KEY, default=None))
        return {
            "enabled": self.config.enabled,
            "signin_enabled": self.config.signin_enabled,
            # 只报「填没填」，绝不回显账号密码
            "mteam_configured": mteam_credentials(self.config) is not None,
            "sites": {
                sid: {
                    "name": entry.get("site_name") or sid,
                    "outcome": entry.get("outcome"),
                    "detail": entry.get("detail"),
                    "consecutive_failures": entry.get("consecutive_failures", 0),
                    "last_ok_at": entry.get("last_ok_at"),
                    "updated_at": entry.get("updated_at"),
                }
                for sid, entry in sorted(states.items())
            },
        }


def _failure_lines(
    sites: list[SiteInfo], results: dict[str, SiteResult], states: dict[str, dict[str, Any]]
) -> list[str]:
    """本轮失败站点一行「站点：原因」，连续多轮的带上轮数。

    只列 ``failed``，外加「此前已连续失败、本轮却没真访问」的跳过 / 需人工站点：
    前者是故障，后者是故障被暂缓报告，混进总数会让人以为站点好了。
    凭据失效的站取 ``failures_before_expired`` 而不是 ``consecutive_failures``：
    状态机为了让失效立刻进告警把后者置成了阈值，那个数不是真实轮数（见 ``state.record``）。
    """
    lines = []
    for site in sites:
        result = results[site.site_id]
        entry = states.get(site.site_id, {})
        if result.outcome != Outcome.FAILED:
            # 跳过 / 需人工不清失败计数：连着坏了几天的站这轮变成「验证中」时若一声不吭，
            # 用户只看到「跳过 1」，会把之前那几天的失败一起忘掉
            carried = int(entry.get("consecutive_failures") or 0)
            if result.outcome in (Outcome.SKIPPED, Outcome.NEED_HUMAN) and carried >= st.FAILURE_THRESHOLD:
                lines.append(
                    f"{site.display_name}：此前已连续失败 {carried} 轮，本轮{result.detail}"
                )
            continue
        key = "failures_before_expired" if result.auth_expired else "consecutive_failures"
        failures = int(entry.get(key) or 0)
        suffix = f"（连续 {failures} 轮）" if failures > 1 else ""
        lines.append(f"{site.display_name}：{result.detail}{suffix}")
    return lines


def _report_detail(
    summary: dict[str, Any], sections: tuple[tuple[str, list[str]], ...] = ()
) -> str:
    """日报正文：通知模板只做字段替换，汇总与明细都在这里拼好。

    明细最多列 :data:`MAX_REPORT_ITEMS` 条、正文最多 :data:`MAX_DETAIL_CHARS` 字：
    宿主的推送载荷有 2200 字节明文上限，超了会被**静默截断**（末尾几站的原因正好丢光），
    与其发一条残缺的，不如自己先说清「另有 N 站」。完整明细去「设置 → 插件」看。
    """
    parts = [f"在线 {summary['ok']} 站（签到成功 {summary['signin']}）"]
    if summary["failed"]:
        parts.append(f"失败 {summary['failed']}")
    if summary["human"]:
        parts.append(f"需人工 {summary['human']}")
    if summary["skipped"]:
        parts.append(f"跳过 {summary['skipped']}")
    line = "，".join(parts) + "。"
    for title, items in sections:
        if not items:
            continue
        shown = items[:MAX_REPORT_ITEMS]
        line += f"{title}：" + "；".join(shown)
        if len(items) > len(shown):
            line += f"；另有 {len(items) - len(shown)} 站，详见「设置 → 插件」"
        line += "。"
    if len(line) > MAX_DETAIL_CHARS:
        line = line[: MAX_DETAIL_CHARS - 1] + "…"
    return line
