"""搜索补头像：解析、认人规则、来源顺序与暂停、限速、冷却、待确认、写入与撤销。全程不联网。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from actor_avatar import nfo
from actor_avatar import search as S
from actor_avatar.filetree import FiletreeCache
from actor_avatar.matching import AvatarIndex
from actor_avatar.runtime import (
    ALIASES_KEY,
    ARMED_KEY,
    MANUAL_KEY,
    PENDING_KEY,
    REMOTE_GONE_NOTE,
    RUN_JOB,
    SEARCH_JOB,
    SEARCH_TRIED_KEY,
    UNMATCHED_KEY,
    Runtime,
    SkipItem,
)
from actor_avatar.settings import Settings

from .conftest import FIXTURES, FakeJobs, FakeOps
from .test_runtime import Mirror

SF = FIXTURES / "search"
API_ID = "SECRET-api-id-123"
AFF_ID = "secret-aff-990"


def text(name: str) -> str:
    return (SF / name).read_text("utf-8")


def raw(name: str) -> bytes:
    return (SF / name).read_bytes()


class Clock:
    """假时钟：sleep 只往前拨时间。"""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Web:
    """假的 av-wiki / javdb / DMM：按 host+path 给夹具；记下每个请求与发出时刻。"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.log: list[tuple[str, float]] = []
        self.requests: list[httpx.Request] = []
        self.avwiki: dict[str, str | int | list[str]] = {
            "/ebwh-189/": "avwiki_EBWH-189_alias.html",
            "/hsoda-113/": "avwiki_HSODA-113_exact.html",
            "/ienf-395/": "avwiki_IENF-395_multi.html",
        }
        self.javdb_search: dict[str, str | int] = {
            "EBWH-189": "javdb_search_EBWH-189.html",
            "HSODA-113": "javdb_search_HSODA-113.html",
        }
        self.javdb_work: dict[str, str] = {
            "/v/bKKkav": "javdb_work_EBWH-189.html",
            "/v/vey23O": "javdb_work_HSODA-113.html",
        }
        self.avatars: dict[str, int] = {"/avatars/me/mez5R.jpg": 200}
        self.dmm: dict[str, object] = {"/mono/actjpgs/ogura_hikari.jpg": 200}
        self.api: dict[str, object] = {}
        self.fail_hosts: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.log.append((host, self.clock.t))
        self.requests.append(request)
        if host in self.fail_hosts:
            raise httpx.ConnectError("boom " + str(request.url), request=request)
        if host == "av-wiki.net":
            hit = self.avwiki.get(path)
            if isinstance(hit, list):  # 依次回：每请求一次取下一个，最后一个一直回
                hit = hit.pop(0) if len(hit) > 1 else hit[0]
            if isinstance(hit, int):
                return httpx.Response(hit)
            if hit is None:
                return httpx.Response(404)
            return httpx.Response(200, text=text(hit))
        if host == "javdb.com":
            assert "over18=1" in request.headers.get("cookie", "")
            if path == "/search":
                hit = self.javdb_search.get(request.url.params.get("q"))
                if isinstance(hit, int):
                    return httpx.Response(hit)
                return httpx.Response(200, text=text(hit) if hit else "<html></html>")
            work = self.javdb_work.get(path)
            return httpx.Response(200, text=text(work)) if work else httpx.Response(404)
        if host == "c0.jdbstatic.com":
            if self.avatars.get(path) == 200:
                return httpx.Response(
                    200,
                    content=raw("javdb_avatar_mez5R.jpg"),
                    headers={"content-type": "image/jpeg"},
                )
            return httpx.Response(403)
        if host == "pics.dmm.co.jp":
            hit = self.dmm.get(path)
            if hit == 200:
                return httpx.Response(
                    200, content=raw("dmm_ogura_hikari.jpg"), headers={"content-type": "image/jpeg"}
                )
            if hit == "placeholder":
                return httpx.Response(
                    200, content=raw("dmm_now_printing.jpg"), headers={"content-type": "image/jpeg"}
                )
            if hit == "html":
                return httpx.Response(
                    200, text="<html></html>", headers={"content-type": "text/html"}
                )
            return httpx.Response(
                302, headers={"location": "https://pics.dmm.com/mono/noimage/now_printing.jpg"}
            )
        if host == "api.dmm.com":
            endpoint = path.rsplit("/", 1)[-1]
            data = self.api.get(endpoint)
            if isinstance(data, int):
                return httpx.Response(data)
            return httpx.Response(200, json=data or {"result": {"status": 200, "items": []}})
        return httpx.Response(404)

    def hosts(self) -> list[str]:
        return [h for h, _t in self.log]


def make_settings(**kw) -> Settings:
    return Settings.model_validate({"dry_run": False, "request_interval_ms": 0, **kw})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def web(clock):
    return Web(clock)


def searcher(web, clock, logger=None, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))
    return S.Searcher(
        http=http, settings=make_settings(**kw), logger=logger, sleep=clock.sleep, clock=clock
    )


def index(*names: str) -> AvatarIndex:
    return AvatarIndex({"Content": {"Co": {f"{n}.jpg": f"AI-Fix-{n}.jpg" for n in names}}})


# ---------------------------------------------------------------------- 解析
@pytest.mark.parametrize(
    ("raw_text", "want"),
    [
        ("HSODA-113", "HSODA-113"),
        ("ebwh00189", "EBWH-189"),
        ("[FHD] dldss_561.mp4", "DLDSS-561"),
        ("EBWH-189 投薬を媚薬に", "EBWH-189"),
        ("x264 1080p", None),
        ("", None),
    ],
)
def test_normalize_num(raw_text, want):
    assert S.normalize_num(raw_text) == want


def test_item_num_prefers_nfo_num():
    nfo_text = "<movie><num>ipx-0123</num><title>x</title></movie>"
    assert S.item_num(nfo_text, "EBWH-189") == "IPX-123"
    assert S.item_num(None, "无番号标题", "HSODA-113.strm") == "HSODA-113"
    assert S.item_num(None, "无番号标题") is None


def test_parse_avwiki_exact_alias_multi_and_challenge():
    assert S.parse_avwiki(text("avwiki_HSODA-113_exact.html"), "HSODA-113") == [
        S.Member("小椋ひかり", "ogura-hikari")
    ]
    # 页面下面相关作品的演员不算进来
    assert S.parse_avwiki(text("avwiki_EBWH-189_alias.html"), "EBWH-189") == [
        S.Member("あかね麗", "akane-urara")
    ]
    multi = S.parse_avwiki(text("avwiki_IENF-395_multi.html"), "IENF-395")
    assert [m.name for m in multi] == ["杏ここ", "沙月恵奈", "皆月ひかる"]
    assert S.is_challenge(text("avwiki_challenge.html"))
    assert not S.is_challenge(text("avwiki_HSODA-113_exact.html"))
    assert S.is_challenge("<html><head><title>Loader</title></head><body></body></html>")
    assert not S.is_challenge(text("avwiki_incomplete.html"))  # 不像挑战页，靠完整性判断
    assert S.avwiki_complete(text("avwiki_EBWH-189_alias.html"), "ebwh-189")
    assert not S.avwiki_complete(text("avwiki_incomplete.html"), "EBWH-189")


def test_parse_javdb_strict_number_and_female_cast():
    body = text("javdb_search_EBWH-189.html")
    assert S.parse_javdb_search(body, "EBWH-189") == "bKKkav"
    assert S.parse_javdb_search(body, "EBWH-199") == "XeEPq4"
    assert S.parse_javdb_search(body, "EBWH-18") is None  # 必须完全相等
    assert S.parse_javdb_work(text("javdb_work_EBWH-189.html")) == [S.Member("二階堂麗", "mez5R")]
    assert S.javdb_avatar_url("mez5R") == "https://c0.jdbstatic.com/avatars/me/mez5R.jpg"


def test_dmm_helpers():
    assert S.dmm_guess_slugs("fujiko-mio") == ["fujiko_mio", "huziko_mio", "fujiko_mio2"]
    assert S.dmm_guess_slugs("kuroki-nami") == ["kuroki_nami", "kuroki_nami2"]
    assert S.dmm_guess_slugs("%e7%99%bd") == []  # 非罗马字 slug 不猜
    assert S.dmm_cid_num("ebwh00189") == "EBWH-189"
    assert S.dmm_cid_num("1hsoda113") == "HSODA-113"
    data = {
        "result": {
            "items": [
                {"content_id": "ebwh00199", "iteminfo": {"actress": [{"id": 1, "name": "甲"}]}},
                {"content_id": "ebwh00189", "iteminfo": {"actress": [{"id": 7, "name": "乙"}]}},
            ]
        }
    }
    assert S.parse_dmm_items(data, "EBWH-189") == ([S.Member("乙", "7")], "ebwh00189")
    assert S.parse_dmm_actress(
        {"result": {"actress": [{"imageURL": {"large": "https://pics.dmm.co.jp/a.jpg"}}]}}
    ) == ("https://pics.dmm.co.jp/a.jpg")


# ---------------------------------------------------------------------- 图片校验
async def test_image_ok_only_real_images(web, clock):
    s = searcher(web, clock)
    web.dmm["/mono/actjpgs/p.jpg"] = "placeholder"
    web.dmm["/mono/actjpgs/h.jpg"] = "html"
    assert await s.image_ok("https://pics.dmm.co.jp/mono/actjpgs/ogura_hikari.jpg")
    assert not await s.image_ok("https://pics.dmm.co.jp/mono/actjpgs/missing.jpg")  # 302
    assert not await s.image_ok("https://pics.dmm.co.jp/mono/actjpgs/p.jpg")  # 占位图哈希
    assert not await s.image_ok("https://pics.dmm.co.jp/mono/actjpgs/h.jpg")  # 不是图片
    assert not await s.image_ok("https://c0.jdbstatic.com/avatars/b8/B8Em1.jpg")  # 403 没头像
    assert await s.image_ok("https://c0.jdbstatic.com/avatars/me/mez5R.jpg")
    assert not await s.image_ok("https://pics.dmm.com/mono/noimage/now_printing.jpg")
    assert not await s.image_ok("http://insecure.example/a.jpg")
    # 不跟随跳转：302 之后没有再请求 pics.dmm.com
    assert "pics.dmm.com" not in web.hosts()


# ---------------------------------------------------------------------- 认人规则
async def test_avwiki_alias_hits_gfriends_and_stops(web, clock):
    s = searcher(web, clock)
    out = await s.search("二階堂麗", "EBWH-189", index=index("あかね麗"))
    assert out.status == "found" and out.alias == "あかね麗"
    [cand] = out.candidates
    assert (cand.source, cand.how, cand.name) == ("gfriends", "alias", "あかね麗")
    row = cand.row()
    assert row["kind"] == "gfriends" and row["company"] == "Co" and row["alias_from"] == "av-wiki"
    assert row["page"] == "https://av-wiki.net/ebwh-189/" and row["cast"] == ["あかね麗"]
    assert web.hosts() == ["av-wiki.net"]  # 后面的来源不再请求


async def test_alias_missing_in_gfriends_falls_through_to_javdb(web, clock):
    s = searcher(web, clock)
    out = await s.search("二階堂麗", "EBWH-189", index=index())
    [cand] = out.candidates
    assert (cand.source, cand.how) == ("javdb", "exact")
    assert cand.url == "https://c0.jdbstatic.com/avatars/me/mez5R.jpg"
    assert cand.page == "https://javdb.com/v/bKKkav" and cand.cast == ["二階堂麗"]
    assert cand.auto_ok and cand.key == "javdb：二階堂麗"
    assert web.hosts() == ["av-wiki.net", "javdb.com", "javdb.com", "c0.jdbstatic.com"]


async def test_exact_on_avwiki_then_dmm_guess(web, clock):
    s = searcher(web, clock)
    out = await s.search("小椋ひかり", "HSODA-113", index=index())
    # javdb 有这个人但头像 403；DMM 用 av-wiki 的 slug 猜：ogura_hikari 有图，ogura_hikari2 302
    [cand] = out.candidates
    assert (cand.source, cand.how) == ("dmm", "guess")
    assert cand.url == "https://pics.dmm.co.jp/mono/actjpgs/ogura_hikari.jpg"
    assert not cand.auto_ok and cand.row()["how_label"] == "猜测"
    assert cand.key == "DMM 猜测：ogura_hikari"
    assert out.tried == ["av-wiki", "javdb", "DMM"]


async def test_multi_cast_without_same_name_decides_nothing(web, clock):
    s = searcher(web, clock)
    out = await s.search("誰か", "IENF-395", index=index("杏ここ"))
    assert out.status == "none" and out.candidates == []
    # 多人且无同名：不认人，也不猜 DMM
    assert "pics.dmm.co.jp" not in web.hosts()


async def test_source_switches(web, clock):
    s = searcher(web, clock, search_avwiki=False, search_dmm=False)
    out = await s.search("小椋ひかり", "HSODA-113", index=index())
    assert out.candidates == [] and set(web.hosts()) <= {"javdb.com", "c0.jdbstatic.com"}
    assert s.enabled_sources() == ["javdb"]


# ---------------------------------------------------------------------- 暂停与限速
async def test_challenge_pauses_source_for_the_round(web, clock):
    web.avwiki["/ebwh-189/"] = "avwiki_challenge.html"
    s = searcher(web, clock)
    paused: set[str] = set()
    out = await s.search("二階堂麗", "EBWH-189", index=index(), paused=paused)
    assert paused == {"avwiki"} and out.blocked == ["av-wiki"]
    assert out.candidates[0].source == "javdb"  # 其它来源照常
    before = web.hosts().count("av-wiki.net")
    await s.search("小椋ひかり", "HSODA-113", index=index(), paused=paused)
    assert web.hosts().count("av-wiki.net") == before  # 本轮不再请求


async def test_avwiki_incomplete_page_retried_once(web, clock):
    web.avwiki["/ebwh-189/"] = ["avwiki_incomplete.html", "avwiki_EBWH-189_alias.html"]
    s = searcher(web, clock)
    out = await s.search("二階堂麗", "EBWH-189", index=index("あかね麗"))
    assert out.blocked == [] and out.alias == "あかね麗"
    [cand] = out.candidates
    assert (cand.source, cand.how, cand.name) == ("gfriends", "alias", "あかね麗")
    assert web.hosts() == ["av-wiki.net", "av-wiki.net"]
    assert clock.sleeps == [5]  # 重试前等了一个请求间隔


async def test_avwiki_incomplete_twice_blocks_and_javdb_continues(web, clock):
    web.avwiki["/ebwh-189/"] = ["avwiki_incomplete.html"]
    s = searcher(web, clock)
    paused: set[str] = set()
    out = await s.search("二階堂麗", "EBWH-189", index=index("あかね麗"), paused=paused)
    assert paused == {"avwiki"} and out.blocked == ["av-wiki"]
    assert "av-wiki" not in out.tried and out.alias is None
    assert web.hosts().count("av-wiki.net") == 2  # 只重试 1 次
    assert out.candidates[0].source == "javdb"  # 其它来源照常


async def test_avwiki_page_with_num_but_no_cast_is_not_blocked(web, clock):
    web.avwiki["/ebwh-189/"] = ["avwiki_nocast.html"]
    s = searcher(web, clock)
    out = await s.search("二階堂麗", "EBWH-189", index=index())
    assert out.blocked == [] and out.tried[0] == "av-wiki" and out.alias is None
    assert web.hosts().count("av-wiki.net") == 1  # 不重试
    assert out.candidates[0].source == "javdb"


@pytest.mark.parametrize("status", [403, 429])
async def test_javdb_403_429_pause(web, clock, status):
    web.javdb_search["EBWH-189"] = status
    s = searcher(web, clock)
    paused: set[str] = set()
    out = await s.search("二階堂麗", "EBWH-189", index=index(), paused=paused)
    assert "javdb" in paused and "javdb" in out.blocked


async def test_network_error_pauses_without_leaking_url(web, clock, caplog):
    web.fail_hosts.add("api.dmm.com")
    s = searcher(
        web,
        clock,
        logger=logging.getLogger("aa-test"),
        dmm_api_id=API_ID,
        dmm_affiliate_id=AFF_ID,
    )
    paused: set[str] = set()
    with caplog.at_level(logging.DEBUG):
        out = await s.search("誰か", "IENF-395", index=index(), paused=paused)
    assert "dmm_api" in paused and "DMM API" in out.blocked
    assert API_ID not in caplog.text and AFF_ID not in caplog.text
    assert "DMM API" in caplog.text


async def test_same_site_interval(web, clock):
    s = searcher(web, clock, search_interval_s=5)
    await s.search("二階堂麗", "EBWH-189", index=index())
    times: dict[str, list[float]] = {}
    for host, t in web.log:
        site = S.site_for(f"https://{host}/")
        times.setdefault(site, []).append(t)
    for stamps in times.values():
        assert all(b - a >= 5 for a, b in zip(stamps, stamps[1:], strict=False))
    # 不同站点之间不用等：av-wiki → javdb 没等；javdb 搜索 → 作品页 → 头像（同属 javdb）各等一次
    assert clock.sleeps == [5.0, 5.0]


async def test_dmm_api_exact_candidate(web, clock):
    web.api["ItemList"] = {
        "result": {
            "status": 200,
            "items": [
                {
                    "content_id": "ienf00395",
                    "iteminfo": {
                        "actress": [{"id": 42, "name": "杏ここ"}, {"id": 43, "name": "乙"}]
                    },
                }
            ],
        }
    }
    web.api["ActressSearch"] = {
        "result": {
            "status": 200,
            "actress": [
                {"imageURL": {"large": "https://pics.dmm.co.jp/mono/actjpgs/ogura_hikari.jpg"}}
            ],
        }
    }
    s = searcher(web, clock, dmm_api_id=API_ID, dmm_affiliate_id=AFF_ID)
    out = await s.search("杏ここ", "IENF-395", index=index())
    [cand] = out.candidates
    assert (cand.source, cand.how) == ("dmm_api", "exact") and cand.auto_ok
    blob = json.dumps(cand.row(), ensure_ascii=False)
    assert API_ID not in blob and AFF_ID not in blob
    api_calls = [r for r in web.requests if r.url.host == "api.dmm.com"]
    assert api_calls and api_calls[0].url.params["api_id"] == API_ID


async def test_dmm_api_not_used_without_both_ids(web, clock):
    s = searcher(web, clock, dmm_api_id=API_ID)
    assert not s.api_configured()
    await s.search("杏ここ", "IENF-395", index=index())
    assert "api.dmm.com" not in web.hosts()


# ---------------------------------------------------------------------- 运行时
@pytest.fixture
def renv(files, store, logger, media_root, filetree_data, web, clock):
    mirror = Mirror(filetree_data)
    http = httpx.AsyncClient(transport=httpx.MockTransport(mirror.handler))
    ops = FakeOps(media_root)
    jobs = FakeJobs()

    def make(**overrides):
        settings = make_settings(**overrides)
        search_http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))
        return Runtime(
            settings=settings,
            ops=ops,
            store=store,
            files=files,
            jobs=jobs,
            filetree=FiletreeCache(files=files, store=store, http=http, logger=logger),
            http=http,
            logger=logger,
            base_url="https://mc.example.com",
            sleep=clock.sleep,
            searcher=S.Searcher(
                http=search_http, settings=settings, logger=logger, sleep=clock.sleep, clock=clock
            ),
        )

    class Env:
        pass

    e = Env()
    e.make, e.ops, e.jobs, e.store, e.web = make, ops, jobs, store, web
    return e


def search_hosts(web) -> list[str]:
    return [h for h in web.hosts() if h != "cdn.jsdelivr.net"]


async def test_auto_round_searches_and_fills_pending(renv):
    path = renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make()
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"] == {
        "searched": 1,
        "candidates": 1,
        "blocked": [],
        "auto_written": 0,
    }
    [row] = await rt.pending()
    [cand] = row["candidates"]
    assert row["actor"] == "二階堂麗" and row["items"] == [1]
    assert cand["via"] == "search" and cand["kind"] == "remote" and cand["source"] == "javdb"
    assert cand["how"] == "exact" and cand["num"] == "EBWH-189"
    assert cand["page"] == "https://javdb.com/v/bKKkav"
    assert cand["preview"] == cand["url"] == "https://c0.jdbstatic.com/avatars/me/mez5R.jpg"
    assert cand["cast"] == ["二階堂麗"] and cand["how_label"] == "同名"
    # 不自动写（默认关）
    assert "二階堂麗" not in (nfo.actor_names(path.read_text("utf-8")) if path.exists() else {})
    unmatched = (await rt.unmatched())["actors"][0]
    assert unmatched["num"] == "EBWH-189" and unmatched["search_tried"]["status"] == "found"
    events = [e for e in renv.store.data["events"] if e["status"] == "search"]
    assert events and "待确认" in events[-1]["reason"]


async def test_confirm_writes_source_url_ledger_and_undo(renv):
    path = renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make()
    await rt.run(None, {"reason": "schedule"})
    result = await rt.confirm("二階堂麗", "javdb：二階堂麗")
    assert result["items"] == [1]
    assert renv.store.data[PENDING_KEY] == {}
    assert renv.store.data[MANUAL_KEY]["二階堂麗"]["kind"] == "remote"
    # 确认后的重跑受写入闸门：还没解锁 → 只预览
    gated = await rt.run(None, {"reason": "confirm", "media_item_ids": [1]})
    assert gated["dry_run"] is True and not path.exists()
    assert "search" not in gated  # 确认后的重跑不搜索
    preview = [e for e in renv.store.data["events"] if e["status"] == "preview"][-1]
    assert preview["matched"]["二階堂麗"] == "search:javdb"
    await renv.store.set(ARMED_KEY, {"at": "x"})
    before = len(renv.web.log)
    written = await rt.run(None, {"reason": "confirm", "media_item_ids": [1]})
    assert written["written"] == 1
    url = "https://c0.jdbstatic.com/avatars/me/mez5R.jpg"
    assert nfo.actor_names(path.read_text("utf-8"))["二階堂麗"] == url
    assert renv.store.data["ledger:1"]["owned"] == {"二階堂麗": url}
    # 写前再验证了一次（只请求了图片）
    assert [h for h, _ in renv.web.log[before:]] == ["c0.jdbstatic.com"]
    # 再跑一次：已写进去的同一地址不再请求
    before = len(renv.web.log)
    await rt.run(None, {"reason": "confirm", "media_item_ids": [1]})
    assert len(renv.web.log) == before
    await rt.undo(None, {"media_item_ids": [1]})
    assert not path.exists() and renv.store.data["ledger:1"]["owned"] == {}


async def test_dead_source_image_is_not_written(renv):
    path = renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make()
    await rt.run(None, {"reason": "schedule"})
    await rt.confirm("二階堂麗", "javdb：二階堂麗")
    renv.web.avatars.clear()  # 头像没了：403
    summary = await rt.run(None, {"reason": "manual", "media_item_ids": [1]})
    assert summary["written"] == 0 and REMOTE_GONE_NOTE in summary["notes"]
    assert not path.exists()


async def test_alias_confirm_uses_gfriends_and_remembers_alias(renv, filetree_data):
    filetree_data["Content"]["z-DMM(骑)"]["あかね麗.jpg"] = "AI-Fix-あかね麗.jpg"
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make()
    # 换一份带 あかね麗 的索引
    rt.filetree.http = httpx.AsyncClient(
        transport=httpx.MockTransport(Mirror(filetree_data).handler)
    )
    await rt.run(None, {"reason": "schedule"})
    [row] = await rt.pending()
    [cand] = row["candidates"]
    assert cand["kind"] == "gfriends" and cand["name"] == "あかね麗" and cand["how"] == "alias"
    assert (
        cand["preview"].startswith("https://cdn.jsdelivr.net/") and cand["alias_from"] == "av-wiki"
    )
    await rt.confirm("二階堂麗", "あかね麗")
    assert renv.store.data[ALIASES_KEY] == {"二階堂麗": "あかね麗"}
    summary = await rt.run(None, {"reason": "manual", "media_item_ids": [1]})
    assert summary["written"] == 1


async def test_auto_write_only_exact_and_only_when_armed(renv):
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    renv.ops.add_item(2, "HSODA-113", [{"name": "小椋ひかり"}])
    rt = renv.make(search_auto_write=True)
    # 未解锁：自动轮次按预览处理，搜索结果只进待确认
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["auto_written"] == 0
    assert set(renv.store.data[PENDING_KEY]) == {"二階堂麗", "小椋ひかり"}
    renv.store.data.pop(PENDING_KEY)
    renv.store.data.pop(SEARCH_TRIED_KEY)
    await renv.store.set(ARMED_KEY, {"at": "x"})
    renv.jobs.created.clear()
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["auto_written"] == 1
    manual = renv.store.data[MANUAL_KEY]
    assert manual["二階堂麗"]["kind"] == "remote" and manual["二階堂麗"]["auto"] is True
    # 猜测（DMM）永远只进待确认
    assert "小椋ひかり" not in manual
    assert renv.store.data[PENDING_KEY]["小椋ひかり"]["candidates"][0]["how"] == "guess"
    [job] = renv.jobs.created
    assert job["handler"] == RUN_JOB and job["input"]["reason"] == "search"
    assert job["input"]["media_item_ids"] == [1]


async def test_auto_write_never_in_dry_run(renv):
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make(search_auto_write=True, dry_run=True)
    await renv.store.set(ARMED_KEY, {"at": "x"})
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["auto_written"] == 0 and MANUAL_KEY not in renv.store.data
    assert "二階堂麗" in renv.store.data[PENDING_KEY]


async def test_only_auto_reasons_search(renv):
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make()
    for data in (
        {"reason": "manual", "dry_run": True},
        {"reason": "manual", "media_item_ids": [1]},
        {"reason": "confirm", "media_item_ids": [1]},
        {"reason": "manual-entry", "media_item_ids": [1]},
    ):
        summary = await rt.run(None, data)
        assert "search" not in summary
    assert search_hosts(renv.web) == []
    # 待确认的确认 / 拒绝本身也不搜
    await rt.reject("二階堂麗")
    assert search_hosts(renv.web) == []
    summary = await rt.run(None, {"reason": "arrived", "library_id": 9, "media_item_ids": [1]})
    assert summary["search"]["searched"] == 0  # 拒绝过的演员不再自动搜


async def test_search_disabled(renv):
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make(search_enabled=False)
    summary = await rt.run(None, {"reason": "schedule"})
    assert "search" not in summary and search_hosts(renv.web) == []
    with pytest.raises(SkipItem, match="关闭"):
        await rt.enqueue_search("二階堂麗")


async def test_per_run_cap_and_cooldown(renv):
    for i in range(4):
        renv.ops.add_item(10 + i, f"IENF-{395 + i}", [{"name": f"无名{i}"}])
    rt = renv.make(search_per_run=2, search_javdb=False, search_dmm=False)
    first = await rt.run(None, {"reason": "schedule"})
    assert first["search"]["searched"] == 2
    second = await rt.run(None, {"reason": "schedule"})
    assert second["search"]["searched"] == 2  # 另外两位
    third = await rt.run(None, {"reason": "schedule"})
    assert third["search"]["searched"] == 0  # 都在冷却
    tried = renv.store.data[SEARCH_TRIED_KEY]
    assert {v["status"] for v in tried.values()} == {"none"}
    # 冷却过期后又能搜
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="seconds")
    tried["无名0"]["at"] = old
    await renv.store.set(SEARCH_TRIED_KEY, tried)
    fourth = await rt.run(None, {"reason": "schedule"})
    assert fourth["search"]["searched"] == 1


async def test_blocked_is_not_cooldown(renv):
    renv.web.avwiki["/ienf-395/"] = "avwiki_challenge.html"
    renv.ops.add_item(1, "IENF-395", [{"name": "无名"}])
    rt = renv.make(search_javdb=False, search_dmm=False)
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["blocked"] == ["av-wiki"]
    assert renv.store.data[SEARCH_TRIED_KEY]["无名"]["status"] == "blocked"
    again = await rt.run(None, {"reason": "schedule"})
    assert again["search"]["searched"] == 1  # 被拦不算没搜到，下一轮还会搜
    reasons = [e["reason"] for e in renv.store.data["events"] if e["status"] == "search"]
    assert any("拦截" in r for r in reasons)


async def test_round_stops_when_all_sources_paused(renv):
    renv.web.avwiki = {k: "avwiki_challenge.html" for k in ("/ienf-395/", "/ienf-396/")}
    renv.ops.add_item(1, "IENF-395", [{"name": "甲甲"}])
    renv.ops.add_item(2, "IENF-396", [{"name": "乙乙"}])
    rt = renv.make(search_javdb=False, search_dmm=False)
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["searched"] == 1
    assert search_hosts(renv.web).count("av-wiki.net") == 1


async def test_manual_button_job_ignores_cooldown(renv):
    renv.ops.add_item(1, "IENF-395", [{"name": "无名"}])
    rt = renv.make(search_javdb=False, search_dmm=False)
    await rt.run(None, {"reason": "schedule"})
    assert (await rt.run(None, {"reason": "schedule"}))["search"]["searched"] == 0
    ref = await rt.enqueue_search("无名")
    [job] = [j for j in renv.jobs.created if j["handler"] == SEARCH_JOB]
    assert job["input"] == {"actor": "无名"} and ref.created
    assert (await rt.search_status("无名"))["state"] == "queued"
    before = search_hosts(renv.web).count("av-wiki.net")
    result = await rt.search_job(None, {"actor": "无名"})
    assert result["status"] == "none"
    assert search_hosts(renv.web).count("av-wiki.net") == before + 1
    status = await rt.search_status("无名")
    assert status["state"] == "done" and status["candidates"] == 0 and status["num"] == "IENF-395"
    with pytest.raises(SkipItem):
        await rt.enqueue_search("不存在的人")


async def test_manual_search_result_goes_to_pending_even_with_auto_write(renv):
    renv.ops.add_item(1, "EBWH-189", [{"name": "二階堂麗"}])
    rt = renv.make(search_auto_write=True, search_enabled=True)
    await rt.run(None, {"reason": "manual", "dry_run": True})
    await renv.store.set(ARMED_KEY, {"at": "x"})
    result = await rt.search_job(None, {"actor": "二階堂麗"})
    assert result["status"] == "found" and result["auto_written"] is False
    assert "二階堂麗" in renv.store.data[PENDING_KEY]


async def test_no_num_actor_is_skipped(renv):
    renv.ops.add_item(1, "无番号", [{"name": "无名"}], title="家里拍的")
    rt = renv.make()
    summary = await rt.run(None, {"reason": "schedule"})
    assert summary["search"]["searched"] == 0 and search_hosts(renv.web) == []
    with pytest.raises(SkipItem, match="番号"):
        await rt.enqueue_search("无名")


async def test_old_unmatched_entries_without_nums_use_titles(renv):
    await renv.store.set(
        UNMATCHED_KEY, {"旧人": {"actor": "旧人", "items": [3], "titles": ["HSODA-113 旧标题"]}}
    )
    assert Runtime.entry_num(renv.store.data[UNMATCHED_KEY]["旧人"]) == "HSODA-113"


async def test_pending_backward_compatible_with_old_rows(renv):
    await renv.store.set(
        PENDING_KEY,
        {
            "甲": {
                "actor": "甲",
                "candidates": [{"name": "八掛うみ", "company": "Co", "file": "x.jpg"}],
                "items": [1],
                "first_seen": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    rt = renv.make()
    [row] = await rt.pending()
    assert row["candidates"][0]["preview"].endswith("/Content/Co/x.jpg")


async def test_secrets_never_leak(renv, caplog):
    renv.web.fail_hosts.add("api.dmm.com")
    renv.ops.add_item(1, "IENF-395", [{"name": "无名"}])
    rt = renv.make(dmm_api_id=API_ID, dmm_affiliate_id=AFF_ID)
    with caplog.at_level(logging.DEBUG):
        await rt.run(None, {"reason": "schedule"})
        report = await rt.report()
    assert report["settings"]["dmm_api"] is True
    blob = json.dumps(
        [report, renv.store.data, await rt.unmatched(), await rt.pending()], ensure_ascii=False
    )
    for secret in (API_ID, AFF_ID):
        assert secret not in blob and secret not in caplog.text
    assert API_ID not in repr(rt.settings) and API_ID not in str(rt.settings.model_dump())


def test_settings_bounds():
    s = Settings()
    assert s.search_enabled and s.search_avwiki and s.search_javdb and s.search_dmm
    assert not s.search_auto_write
    assert (s.search_per_run, s.search_interval_s, s.search_cooldown_days) == (5, 5, 30)
    assert s.dmm_api_id.get_secret_value() == ""
    from pydantic import ValidationError

    for bad in ({"search_per_run": 31}, {"search_interval_s": 1}, {"search_cooldown_days": 0}):
        with pytest.raises(ValidationError):
            Settings.model_validate(bad)


def test_page_has_search_controls():
    import re

    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text("utf-8")
    entry = (Path(__file__).resolve().parents[1] / "actor_avatar" / "__init__.py").read_text(
        "utf-8"
    )
    assert 'api("/avatar-search", { actor })' in html
    assert 'api("/avatar-search/status?actor="' in html
    assert re.search(r'"/avatar-search",\s*operation_id=', entry)
    assert re.search(r'"/avatar-search/status",\s*operation_id=', entry)
    assert 'title: "搜索补头像"' in html and "搜索头像" in html
    # 密码框不回显：只显示已设置 / 未设置
    assert 'el.type = "password"' in html and "secrets_set" in html
    assert 'rel = "noopener noreferrer"' in html
    assert "innerHTML" not in html
