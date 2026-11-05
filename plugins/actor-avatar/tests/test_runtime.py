from __future__ import annotations

import json
import os

import httpx
import pytest
from actor_avatar import nfo
from actor_avatar.aliases import NameTableCache
from actor_avatar.filetree import FiletreeCache
from actor_avatar.runtime import (
    ALIASES_KEY,
    ARMED_KEY,
    NOT_ARMED_NOTE,
    PENDING_KEY,
    RUN_JOB,
    SELECTION_KEY,
    UNDO_JOB,
    Runtime,
    SkipItem,
    thumb_url_warning,
)
from actor_avatar.settings import Settings, ThumbMode

from .conftest import FIXTURES, FakeContext, FakeJobs, FakeOps

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000


class Mirror:
    def __init__(self, filetree: dict) -> None:
        self.body = json.dumps(filetree, ensure_ascii=False).encode()
        self.requests: list[str] = []
        self.xml_down = False
        self.xml_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path.endswith("/Filetree.json"):
            return httpx.Response(200, content=self.body, headers={"etag": '"e1"'})
        if request.url.path.endswith("/mapping_actor.xml"):
            self.xml_requests += 1
            if self.xml_down:
                return httpx.Response(503)
            return httpx.Response(200, content=(FIXTURES / "mapping_actor.xml").read_bytes())
        if "/Content/" in request.url.path:
            return httpx.Response(200, content=JPEG)
        return httpx.Response(404)


async def _nosleep(_seconds):
    return None


@pytest.fixture
def env(files, store, logger, media_root, filetree_data):
    mirror = Mirror(filetree_data)
    http = httpx.AsyncClient(transport=httpx.MockTransport(mirror.handler))
    ops = FakeOps(media_root)
    jobs = FakeJobs()

    def make(**overrides):
        signer = overrides.pop("_sign", None)
        names = overrides.pop("_names", False)
        ai = overrides.pop("_ai", None)
        base_url = overrides.pop("_base_url", "https://mc.example.com")
        settings = Settings.model_validate(
            {"dry_run": False, "request_interval_ms": 0, **overrides}
        )

        async def sign(path):
            return f"/api/v1/plugins/actor-avatar{path}?sig=abc"

        return Runtime(
            settings=settings,
            ops=ops,
            store=store,
            files=files,
            jobs=jobs,
            filetree=FiletreeCache(files=files, store=store, http=http, logger=logger),
            http=http,
            logger=logger,
            sign=signer or sign,
            base_url=base_url,
            sleep=_nosleep,
            ai=ai,
            names=(
                NameTableCache(files=files, store=store, http=http, logger=logger)
                if names
                else None
            ),
        )

    class Env:
        pass

    e = Env()
    e.make, e.ops, e.jobs, e.store, e.files, e.mirror = make, ops, jobs, store, files, mirror
    return e


def thumbs(path):
    return nfo.actor_names(open(path, encoding="utf-8").read())


async def test_only_local_video_libraries(env):
    rt = env.make(libraries=["小姐姐", "电影", "不存在", "12"])
    libs, notes = await rt.target_libraries()
    assert [lib["name"] for lib in libs] == ["小姐姐", "家庭录像"]
    assert any("电影" in n and "跳过" in n for n in notes)
    assert any("不存在" in n for n in notes)


async def test_default_selection_by_name(env):
    rt = env.make()
    ids, source, notes = await rt.selection()
    assert ids == [9] and source == "default" and notes == []
    # 默认值不落库：以后改库名也还是按名字找
    assert SELECTION_KEY not in env.store.data


async def test_legacy_numeric_setting_migrates_once(env):
    rt = env.make(libraries=["9"])
    ids, source, _notes = await rt.selection()
    assert ids == [9] and source == "panel"
    assert env.store.data[SELECTION_KEY]["ids"] == [9]
    assert env.store.data[SELECTION_KEY]["migrated_from"] == ["9"]
    # 迁移后以勾选结果为准，旧设置不再起作用
    rt = env.make(libraries=["12"])
    assert (await rt.selection())[0] == [9]


async def test_library_choices_and_save(env):
    rt = env.make()
    choices = await rt.library_choices()
    by_name = {i["name"]: i for i in choices["items"]}
    assert set(by_name) == {"电影", "小姐姐", "家庭录像"}
    assert by_name["小姐姐"]["selected"] and by_name["小姐姐"]["eligible"]
    assert not by_name["电影"]["eligible"] and by_name["电影"]["reason"]
    assert not by_name["家庭录像"]["selected"]

    saved = await rt.save_selection([12, 9, 12])
    assert saved["selected"] == [12, 9] and saved["source"] == "panel"
    libs, _ = await rt.target_libraries()
    assert [lib["name"] for lib in libs] == ["家庭录像", "小姐姐"]
    with pytest.raises(SkipItem, match="不是本地视频库"):
        await rt.save_selection([1])
    with pytest.raises(SkipItem, match="不存在"):
        await rt.save_selection([404])
    assert env.store.data[SELECTION_KEY]["ids"] == [12, 9]

    # 一个都不勾：什么都不处理
    await rt.save_selection([])
    assert (await rt.run(None, {"reason": "manual"}))["status"] == "no_library"


async def test_selected_library_deleted_later(env):
    rt = env.make()
    await rt.save_selection([9, 12])
    env.ops.libraries = [lib for lib in env.ops.libraries if lib["id"] != 12]
    choices = await rt.library_choices()
    assert any("12" in n for n in choices["notes"])
    libs, notes = await rt.target_libraries()
    assert [lib["id"] for lib in libs] == [9] and any("12" in n for n in notes)


async def test_auto_runs_need_arming(env):
    env.ops.host_fixed = True
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}])
    rt = env.make()
    # 只预览关了，但从没手动跑过：自动运行仍只预览
    for reason in ("schedule", "arrived"):
        summary = await rt.run(None, {"reason": reason, "media_item_ids": [1]})
        assert summary["dry_run"] is True and summary["written"] == 0
        assert NOT_ARMED_NOTE in summary["notes"]
    assert not path.exists() and ARMED_KEY not in env.store.data

    # 手动跑一次写入 = 解锁
    first = await rt.run(None, {"reason": "manual"})
    assert first["written"] == 1 and ARMED_KEY in env.store.data
    env.ops.add_item(2, "SSIS-002", [{"name": "八掛うみ"}])
    auto = await rt.run(None, {"reason": "arrived", "library_id": 9, "media_item_ids": [2]})
    assert auto["dry_run"] is False and auto["written"] == 1

    # 停止自动写入
    await rt.disarm()
    env.ops.add_item(3, "SSIS-003", [{"name": "三上悠亜"}])
    again = await rt.run(None, {"reason": "schedule"})
    assert again["dry_run"] is True and again["written"] == 0
    # 手动预览不会解锁
    await rt.run(None, {"reason": "manual", "dry_run": True})
    assert ARMED_KEY not in env.store.data


async def test_name_table_matches_chinese_names(env):
    path = env.ops.add_item(1, "SSIS-001", [{"name": "凉森玲梦"}, {"name": "八挂海"}])
    rt = env.make(_names=True)
    summary = await rt.run(FakeContext(), {"reason": "manual"})
    assert summary["written"] == 1 and summary["avatars"] == 2
    assert all(t.startswith("https://") for t in thumbs(path).values())
    report = await rt.report()
    assert report["name_table"]["keys"] > 0
    assert report["settings"]["libraries"] == ["小姐姐"]


async def test_name_table_off_or_broken(env):
    env.ops.add_item(1, "SSIS-001", [{"name": "凉森玲梦"}])
    off = await env.make(_names=True, name_table=False).run(None, {"reason": "manual"})
    assert off["avatars"] == 0 and env.mirror.xml_requests == 0
    env.mirror.xml_down = True
    summary = await env.make(_names=True).run(None, {"reason": "manual", "dry_run": True})
    assert summary["avatars"] == 0
    assert any("译名表不可用" in n for n in summary["notes"])


async def test_dry_run_touches_nothing(env):
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}, {"name": "八挂うみ"}])
    rt = env.make(dry_run=True)
    summary = await rt.run(FakeContext(), {"reason": "manual"})
    assert summary["previewed"] == 1 and summary["avatars"] == 2 and summary["written"] == 0
    assert not path.exists()
    assert env.ops.refreshed == []
    assert all(p.endswith((".gz", ".tmp")) for p in env.files.writes)
    report = await rt.report()
    event = report["events"][0]
    assert event["status"] == "preview" and event["nfo"] == "将新建"
    assert event["files"]["涼森れむ"] == "9-AVDBS/AI-Fix-涼森れむ.jpg"
    assert event["matched"]["八挂うみ"] == "fold:八掛うみ"


async def test_run_override_dry_run_flag(env):
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}])
    rt = env.make(dry_run=True)
    await rt.run(None, {"dry_run": False})
    assert path.exists()


async def test_creates_nfo_with_title_and_all_actors(env):
    path = env.ops.add_item(
        1,
        "SSIS-001",
        [{"name": "涼森れむ", "role": "主演"}, {"name": "无名氏", "role": "路人"}],
        title="SSIS-001 新人 涼森れむ",
    )
    rt = env.make()
    summary = await rt.run(FakeContext(), {})
    assert summary["written"] == 1 and summary["avatars"] == 1
    root = nfo.parse(path.read_text("utf-8"))
    assert root.findtext("title") == "SSIS-001 新人 涼森れむ"
    assert [(a.findtext("name"), a.findtext("role")) for a in root.findall("actor")] == [
        ("涼森れむ", "主演"),
        ("无名氏", "路人"),
    ]
    url = root.find("actor").findtext("thumb")
    assert url.startswith(
        "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Content/9-AVDBS/AI-Fix-"
    )
    assert env.ops.refreshed == [1]
    ledger = env.store.data["ledger:1"]
    assert ledger["created"] is True and ledger["owned"] == {"涼森れむ": url}
    assert ledger["backup"] is None
    # 0.46/0.47 主程序缺口：复核时详情不显示
    assert summary["not_shown"] == 1 and summary["shown"] == 0
    assert (await rt.report())["host_display"]["not_shown"] == 1


async def test_verify_shown_when_host_fixed(env):
    env.ops.host_fixed = True
    env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}])
    summary = await env.make().run(None, {})
    assert summary["shown"] == 1 and summary["not_shown"] == 0


async def test_existing_nfo_preserved_and_foreign_thumb_kept(env):
    original = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<movie>\n  <title>手工标题</title>\n  <plot>简介</plot>\n"
        "  <actor>\n    <name>三上悠亚</name>\n  </actor>\n"
        "  <actor>\n    <name>明日花キララ</name>\n"
        "    <thumb>https://mine.example/k.jpg</thumb>\n  </actor>\n</movie>\n"
    )
    path = env.ops.add_item(
        2, "SSNI-100", [{"name": "三上悠亚"}, {"name": "明日花キララ"}], nfo=original
    )
    summary = await env.make(ai_fix=False).run(None, {})
    assert summary["written"] == 1
    got = thumbs(path)
    assert got["明日花キララ"] == "https://mine.example/k.jpg"
    assert got["三上悠亚"].endswith(
        "/Content/0-Hand-Storage/%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9C.jpg"
    )
    root = nfo.parse(path.read_text("utf-8"))
    assert root.findtext("title") == "手工标题" and root.findtext("plot") == "简介"
    backup = env.store.data["ledger:2"]["backup"]
    with open(backup, encoding="utf-8") as fh:
        assert fh.read() == original
    assert not backup.startswith(str(path.parent))
    event = (await env.make().report())["events"][0]
    assert event["has_avatar"] == ["明日花キララ"]


async def test_skips_actor_with_db_avatar(env):
    path = env.ops.add_item(3, "X-1", [{"name": "涼森れむ", "thumb_url": "https://tmdb/x.jpg"}])
    summary = await env.make().run(None, {})
    assert summary["noop"] == 1 and not path.exists()


async def test_second_run_is_noop(env):
    env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}])
    rt = env.make()
    await rt.run(None, {})
    env.ops.refreshed.clear()
    summary = await rt.run(None, {})
    assert summary["written"] == 0 and env.ops.refreshed == []


async def test_undo_created_nfo_is_deleted_after_refresh(env):
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}, {"name": "无名氏"}])
    rt = env.make()
    await rt.run(None, {})
    assert path.exists()
    result = await rt.undo(FakeContext(), {"media_item_ids": [1]})
    assert result == {"undone": 1, "deleted": 1, "skipped": 0, "items": [1]}
    assert not path.exists()
    assert env.ops.refreshed == [1, 1]
    assert env.store.data["ledger:1"]["owned"] == {}
    # 主程序档案里的演员表在删除前已被刷新成无头像版本
    assert [a["name"] for a in env.ops.items[1]["local_meta"]["actors"]] == ["涼森れむ", "无名氏"]


async def test_undo_existing_nfo_restores_content(env):
    original = (
        "<movie>\n  <title>T</title>\n  <actor>\n    <name>涼森れむ</name>\n  </actor>\n</movie>\n"
    )
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}], nfo=original)
    rt = env.make()
    await rt.run(None, {})
    assert path.read_text("utf-8") != original
    await rt.undo(None, {"all": True})
    assert path.exists() and path.read_text("utf-8") == original


async def test_undo_keeps_user_edits(env):
    path = env.ops.add_item(1, "SSIS-001", [{"name": "涼森れむ"}])
    rt = env.make()
    await rt.run(None, {})
    path.write_text(
        path.read_text("utf-8").replace("<title>", "<plot>用户加的</plot>\n  <title>"), "utf-8"
    )
    result = await rt.undo(None, {"media_item_ids": [1]})
    # 用户改过新建的 NFO：撤掉头像但不删文件
    assert result["deleted"] == 0 and path.exists()
    assert "用户加的" in path.read_text("utf-8")
    assert thumbs(path) == {"涼森れむ": None}


async def test_pending_inference_confirm_and_reject(env):
    env.ops.add_item(
        5,
        "SSIS-002",
        [{"name": "凉森玲梦"}, {"name": "八挂うみ"}],
        title="SSIS-002 共演 涼森れむ 八掛うみ",
    )
    rt = env.make()
    summary = await rt.run(None, {})
    assert summary["pending"] == 1
    pending = await rt.pending()
    assert pending[0]["actor"] == "凉森玲梦"
    assert [c["name"] for c in pending[0]["candidates"]] == ["涼森れむ"]
    assert pending[0]["candidates"][0]["preview"].startswith("https://cdn.jsdelivr.net/")
    # 推断出来的只进待确认，没写
    assert "凉森玲梦" not in env.store.data["ledger:5"]["owned"]
    result = await rt.confirm("凉森玲梦", "涼森れむ")
    assert result["items"] == [5]
    assert env.store.data[ALIASES_KEY] == {"凉森玲梦": "涼森れむ"}
    assert env.store.data[PENDING_KEY] == {}
    await rt.run(None, {"media_item_ids": [5], "library_id": 9})
    assert "凉森玲梦" in env.store.data["ledger:5"]["owned"]


async def test_reject_suppresses_future_pending(env):
    env.ops.add_item(5, "SSIS-002", [{"name": "凉森玲梦"}], title="SSIS-002 涼森れむ")
    rt = env.make()
    await rt.run(None, {})
    await rt.reject("凉森玲梦")
    assert await rt.pending() == []
    await rt.run(None, {})
    assert await rt.pending() == []
    with pytest.raises(SkipItem):
        await rt.confirm("凉森玲梦", "涼森れむ")


async def test_no_inference_when_two_unmatched(env):
    env.ops.add_item(6, "X-6", [{"name": "甲甲甲"}, {"name": "乙乙乙"}], title="X-6 涼森れむ")
    rt = env.make()
    summary = await rt.run(None, {})
    assert summary["pending"] == 0


async def test_cursor_and_max_items(env):
    for i in range(1, 8):
        env.ops.add_item(i, f"A-{i}", [{"name": "涼森れむ"}])
    rt = env.make(max_items_per_run=3, dry_run=True)
    seen = []
    for _ in range(3):
        summary = await rt.run(None, {})
        seen.append(summary["seen"])
    assert seen == [3, 3, 1]
    assert env.store.data["sweep:cursor"] == {"library_index": 0, "offset": 0}


async def test_item_outside_target_libraries_ignored(env):
    env.ops.add_item(1, "A-1", [{"name": "涼森れむ"}], library_id=12)
    summary = await env.make().run(None, {"media_item_ids": [1], "library_id": 12})
    assert summary["seen"] == 0


async def test_non_utf8_nfo_skipped_without_write(env):
    path = env.ops.add_item(1, "A-1", [{"name": "涼森れむ"}])
    path.write_bytes(
        "<movie><title>中文</title><actor><name>涼森れむ</name></actor></movie>".encode(
            "gbk", "replace"
        )
    )
    before = path.read_bytes()
    summary = await env.make().run(None, {})
    assert summary["failed"] == 1 and path.read_bytes() == before


async def test_no_library_status(env):
    summary = await env.make(libraries=["电影"]).run(None, {})
    assert summary["status"] == "no_library"


async def test_hosted_mode_downloads_and_signs(env):
    path = env.ops.add_item(1, "A-1", [{"name": "涼森れむ"}])
    rt = env.make(thumb_mode=ThumbMode.HOSTED)
    await rt.run(None, {})
    url = thumbs(path)["涼森れむ"]
    assert url.startswith(
        "https://mc.example.com/api/v1/plugins/actor-avatar/avatar/"
    ) and url.endswith(".jpg?sig=abc")
    stored = os.listdir(env.files.path("plugin", "avatars"))
    assert len(stored) == 1 and stored[0].endswith(".jpg")
    # 第二部片用同一张图：不再下载
    env.ops.add_item(2, "A-2", [{"name": "涼森れむ"}])
    before = len([u for u in env.mirror.requests if "/Content/" in u])
    await rt.run(None, {})
    assert len([u for u in env.mirror.requests if "/Content/" in u]) == before


def test_thumb_url_warning():
    assert thumb_url_warning("")
    assert "IP" in thumb_url_warning("http://192.168.1.2")
    assert "端口" in thumb_url_warning("https://mc.example.com:8443")
    assert thumb_url_warning("https://mc.example.com") is None


async def test_enqueue_dedupe_keys(env):
    rt = env.make()
    await rt.enqueue_run(reason="arrived", library_id=9, media_item_ids=[3, 1, 3])
    await rt.enqueue_undo([2])
    await rt.enqueue_undo(None)
    assert env.jobs.created[0]["handler"] == RUN_JOB
    assert env.jobs.created[0]["input"]["media_item_ids"] == [1, 3]
    assert env.jobs.created[0]["dedupe_key"] == "run:items:1,3"
    assert (
        env.jobs.created[1]["handler"] == UNDO_JOB and env.jobs.created[2]["input"]["all"] is True
    )


async def test_store_values_stay_small(env):
    for i in range(1, 260):
        env.ops.add_item(i, f"B-{i}", [{"name": f"不存在{i}号"}], title=f"B-{i} 涼森れむ")
    rt = env.make(max_items_per_run=300)
    await rt.run(None, {})
    # FakeStore 每次 set 都断言 < 64KB；待确认按演员聚合、超出时丢最旧的
    assert 0 < len(env.store.data[PENDING_KEY]) <= 259
    assert len(env.store.data["events"]) <= 150


async def test_big_events_and_ledger_shards(env):
    rt = env.make()
    long = ["很长的演员名字" * 8] * 12
    for i in range(400):
        await rt._event(i, 9, "noop", "x" * 200, unmatched=long, has_avatar=long)
    assert 0 < len(env.store.data["events"]) <= 150
    for item_id in (1, 3999, 4000, 12001):
        await rt._save_ledger(item_id, {"owned": {"a": "u"}, "library_id": 9})
    assert env.store.data["ledger:shards"] == [0, 1, 3]
    assert await rt._ledger_ids() == [1, 3999, 4000, 12001]


async def test_host_gap_stops_after_probe_item(env):
    paths = [env.ops.add_item(i, f"A-{i}", [{"name": "涼森れむ"}]) for i in range(1, 5)]
    rt = env.make()
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["status"] == "host_gap"
    assert summary["written"] == 1 and env.ops.refreshed == [1]
    assert summary["not_shown"] == 1 and summary["shown"] == 0
    assert env.store.data["host:display"]["state"] == "hidden"
    assert env.store.data["ledger:1"]["shown"] is False
    # 没处理的条目下次接着来：游标没前进
    assert env.store.data.get("sweep:cursor", {}).get("offset", 0) == 0
    assert paths[0].exists() and not any(p.exists() for p in paths[1:])


async def test_host_gap_known_skips_arrivals(env):
    env.ops.add_item(1, "A-1", [{"name": "涼森れむ"}])
    env.ops.add_item(2, "A-2", [{"name": "涼森れむ"}])
    rt = env.make()
    await rt.run(None, {"reason": "manual"})
    summary = await rt.run(None, {"reason": "arrived", "library_id": 9, "media_item_ids": [2]})
    assert summary["status"] == "host_gap" and summary["seen"] == 0
    assert env.ops.refreshed == [1]
    # 预览不受影响
    preview = await rt.run(None, {"reason": "arrived", "media_item_ids": [2], "dry_run": True})
    assert preview["status"] == "ok" and preview["previewed"] == 1


async def test_host_fixed_later_resumes_and_redisplays(env):
    for i in range(1, 4):
        env.ops.add_item(i, f"A-{i}", [{"name": "涼森れむ"}])
    rt = env.make()
    first = await rt.run(None, {"reason": "manual"})
    assert first["status"] == "host_gap" and env.ops.refreshed == [1]
    env.ops.host_fixed = True
    second = await rt.run(None, {"reason": "schedule"})
    assert second["status"] == "ok"
    assert second["written"] == 2 and second["shown"] == 2 and second["not_shown"] == 0
    assert second["redisplayed"] == 1
    assert sorted(env.ops.refreshed) == [1, 1, 2, 3]
    assert env.store.data["host:display"]["state"] == "shown"
    assert env.store.data["ledger:2"]["shown"] is True


# ---------------------------------------------------------------------- 0.3.0
from actor_avatar.ai import AiAdvisor  # noqa: E402
from actor_avatar.runtime import MANUAL_KEY, UNMATCHED_KEY, UPLOAD_NO_URL_NOTE  # noqa: E402

from .conftest import FakeStore  # noqa: E402,F401
from .fakes_ai import FakeAgent  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 500


def _ai(env, answers, **kwargs):
    async def sleep(_s):
        return None

    agent = FakeAgent(answers)
    adv = AiAdvisor(agent=agent, store=env.store, daily_limit=kwargs.get("limit", 20), sleep=sleep)
    return agent, adv


async def test_unmatched_actors_are_tracked_and_cleared(env):
    env.ops.add_item(
        1, "ABC-001", [{"name": "谁也不是"}, {"name": "涼森れむ"}], title="ABC-001 片名"
    )
    rt = env.make(dry_run=True)
    await rt.run(None, {"reason": "manual"})
    data = await rt.unmatched()
    assert [a["actor"] for a in data["actors"]] == ["谁也不是"]
    row = data["actors"][0]
    assert row["items"] == [1] and row["titles"] == ["ABC-001 片名"] and row["count"] == 1
    await rt.set_manual_alias("谁也不是", "八掛うみ")
    await rt.run(None, {"reason": "manual", "media_item_ids": [1], "library_id": 9})
    assert (await rt.unmatched())["actors"] == []


async def test_manual_alias_is_top_priority_and_validated(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "三上悠亜"}, {"name": "某艺名"}])
    rt = env.make()
    with pytest.raises(SkipItem, match="gfriends 里没有"):
        await rt.set_manual_alias("某艺名", "不存在的名字")
    await rt.run(None, {"reason": "manual", "dry_run": True})
    result = await rt.set_manual_alias("某艺名", "八掛うみ")
    assert result["target"] == "八掛うみ" and result["items"] == [1]
    # 手动别名压过自动匹配（三上悠亜 本来 exact 命中）
    await rt.set_manual_alias("三上悠亜", "涼森れむ")
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["written"] == 1 and summary["avatars"] == 2
    got = thumbs(path)
    assert "%E6%B6%BC%E6%A3%AE" in got["三上悠亜"] or "涼森れむ" in got["三上悠亜"]
    assert "八掛" in got["某艺名"] or "%E5%85%AB" in got["某艺名"]
    events = env.store.data["events"]
    assert any(e.get("status") == "written" for e in events)
    rows = await rt.manual_rows()
    assert {r["actor"] for r in rows} == {"三上悠亜", "某艺名"} and all(r["preview"] for r in rows)
    # 删掉手动设置不动已写的 NFO
    assert (await rt.remove_manual("某艺名"))["removed"]
    assert "某艺名" in thumbs(path)


async def test_manual_alias_preview_respects_dry_run(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "某艺名"}])
    rt = env.make(dry_run=True)
    await rt.set_manual_alias("某艺名", "八掛うみ")
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["previewed"] == 1 and summary["written"] == 0 and not path.exists()
    preview = [e for e in env.store.data["events"] if e["status"] == "preview"][-1]
    assert preview["matched"]["某艺名"] == "manual:八掛うみ"


async def test_manual_entry_rerun_does_not_arm(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "某艺名"}])
    rt = env.make()
    await rt.set_manual_alias("某艺名", "八掛うみ")
    for reason in ("manual-entry", "confirm"):
        summary = await rt.run(None, {"reason": reason, "media_item_ids": [1]})
        assert summary["dry_run"] is True and summary["written"] == 0
        assert NOT_ARMED_NOTE in summary["notes"]
    assert not path.exists() and ARMED_KEY not in env.store.data
    await rt.run(None, {"reason": "manual"})  # 亲手写一次 = 解锁
    env.ops.add_item(2, "ABC-002", [{"name": "某艺名"}])
    summary = await rt.run(None, {"reason": "manual-entry", "media_item_ids": [2]})
    assert summary["written"] == 1


async def test_manual_upload_written_as_signed_thumb_and_undoable(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "无名氏"}])
    rt = env.make()
    with pytest.raises(SkipItem, match="JPG"):
        await rt.set_manual_upload("无名氏", b"GIF89a" + b"0" * 100)
    with pytest.raises(SkipItem, match="5 MB"):
        await rt.set_manual_upload("无名氏", b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024))
    with pytest.raises(SkipItem):
        await rt.set_manual_upload("无名氏", b"RIFF0000AVI " + b"0" * 100)
    result = await rt.set_manual_upload("无名氏", PNG)
    assert result["file"].endswith(".png") and result["linkable"]
    assert await rt.upload_path(result["file"])
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["written"] == 1
    thumb = thumbs(path)["无名氏"]
    assert (
        thumb
        == f"https://mc.example.com/api/v1/plugins/actor-avatar/avatar/{result['file']}?sig=abc"
    )
    assert env.store.data["ledger:1"]["owned"]["无名氏"] == thumb
    await rt.undo(None, {"media_item_ids": [1]})
    # 本插件新建的 NFO：撤销后整份删掉
    assert not path.exists()
    assert env.store.data["ledger:1"]["owned"] == {}


async def test_manual_upload_beats_alias_and_junk(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "あおいさん"}])
    rt = env.make()
    await rt.set_manual_upload("あおいさん", PNG)
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["written"] == 1 and "/avatar/" in thumbs(path)["あおいさん"]


async def test_manual_upload_without_external_url_is_held_back(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "无名氏"}])
    rt = env.make(_base_url="")
    result = await rt.set_manual_upload("无名氏", PNG)
    assert result["linkable"] is False and result["note"] == UPLOAD_NO_URL_NOTE
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["written"] == 0 and UPLOAD_NO_URL_NOTE in summary["notes"]
    assert not path.exists()
    report = await rt.report()
    assert report["can_link_uploads"] is False and report["thumb_warning"]


async def test_junk_names_listed_separately(env):
    env.ops.add_item(
        1,
        "ABC-001",
        [{"name": "京香 大学生モデル"}, {"name": "あおいさん"}],
        title="ABC-001 京香 大学生モデル",
    )
    rt = env.make(dry_run=True)
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["junk"] == 2 and summary["pending"] == 0
    data = await rt.unmatched()
    assert data["actors"] == []
    assert {j["actor"] for j in data["junk"]} == {"京香 大学生モデル", "あおいさん"}
    assert all(j["junk"] for j in data["junk"])
    # 关掉过滤就照常拿去匹配（查不到 → 进普通的没配上列表）
    env.store.data.pop(UNMATCHED_KEY)
    await env.make(dry_run=True, skip_junk_names=False).run(None, {"reason": "manual"})
    assert len((await rt.unmatched())["actors"]) == 2


async def test_manual_alias_rescues_junk_name(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "京香 大学生モデル"}])
    rt = env.make()
    await rt.set_manual_alias("京香 大学生モデル", "涼森れむ")
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["written"] == 1 and thumbs(path)["京香 大学生モデル"]


async def test_search_gfriends_and_name_table(env):
    rt = env.make(_names=True)
    hits = await rt.search("涼森")
    assert hits[0]["name"] == "涼森れむ" and hits[0]["preview"].startswith("https://")
    zh = await rt.search("凉森玲梦")
    assert zh[0]["name"] == "涼森れむ" and zh[0]["via"] == "table"
    part = await rt.search("傻")  # 只在译名表的别名里出现
    assert any(h["name"] == "涼森れむ" and h["via"].startswith("译名表") for h in part)
    assert await rt.search("") == []
    assert len(await rt.search("a", limit=2)) <= 2


async def test_ai_suggest_goes_to_pending_only(env):
    path = env.ops.add_item(1, "ABC-001", [{"name": "某艺名"}], title="ABC-001 某片")
    agent, adv = _ai(env, {"某艺名": '["八掛うみ", "瞎编的名字"]'})
    rt = env.make(_ai=adv)
    await rt.run(None, {"reason": "manual"})
    assert not path.exists()
    result = await rt.ai_suggest("某艺名")
    assert result["accepted"] == ["八掛うみ"] and result["pending_added"] == 1
    assert result["usage"]["total_tokens"] == 135
    assert "ABC-001 某片" in agent.calls[0]["content"]
    pending = await rt.pending()
    assert pending[0]["candidates"][0]["via"] == "ai" and pending[0]["items"] == [1]
    # 没写任何东西；确认后才会写
    assert not path.exists()
    confirmed = await rt.confirm("某艺名", "八掛うみ")
    assert confirmed["items"] == [1]
    await rt.run(None, {"reason": "manual", "media_item_ids": [1], "library_id": 9})
    assert thumbs(path)["某艺名"]
    status = await rt.ai_status()
    assert status["used_today"] == 1 and status["per_run"] is False


async def test_ai_unavailable_without_agent(env):
    rt = env.make()
    status = await rt.ai_status()
    assert status["available"] is False and "agent:none" in status["reason"]
    from actor_avatar.ai import AiError

    with pytest.raises(AiError):
        await rt.ai_suggest("某艺名")


async def test_ai_per_run_is_off_by_default(env):
    env.ops.add_item(1, "ABC-001", [{"name": "某艺名"}])
    agent, adv = _ai(env, {"某艺名": '["八掛うみ"]'})
    summary = await env.make(_ai=adv, dry_run=True).run(None, {"reason": "manual"})
    assert agent.calls == [] and "ai" not in summary


async def test_ai_per_run_caps_and_skips(env):
    names = [f"无名{i}" for i in range(8)]
    for i, name in enumerate(names, start=1):
        env.ops.add_item(i, f"ABC-{i:03}", [{"name": name}])
    env.ops.add_item(20, "ABC-020", [{"name": "あおいさん"}])
    agent, adv = _ai(env, {"无名": '["八掛うみ"]'})
    rt = env.make(_ai=adv, dry_run=True, ai_suggest_on_run=True)
    await rt.set_manual_alias("无名7", "涼森れむ")
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["ai"]["asked"] == 5 and summary["ai"]["accepted"] == 5
    asked = [c["content"] for c in agent.calls]
    assert not any("あおいさん" in c for c in asked)
    assert not any("无名7" in c for c in asked)
    # 问过的 30 天内不再问
    again = await rt.run(None, {"reason": "manual"})
    assert again["ai"]["asked"] <= 2
    assert env.store.data[MANUAL_KEY]["无名7"]["target"] == "涼森れむ"


async def test_ai_per_run_stops_at_daily_cap(env):
    for i in range(1, 4):
        env.ops.add_item(i, f"ABC-{i:03}", [{"name": f"无名{i}"}])
    agent, adv = _ai(env, {}, limit=2)
    rt = env.make(_ai=adv, dry_run=True, ai_suggest_on_run=True)
    summary = await rt.run(None, {"reason": "manual"})
    assert summary["ai"]["asked"] == 2 and summary["ai"]["errors"]
    assert len(agent.calls) == 2


async def test_report_link_info_for_v04_page(env):
    # 0.4.0 页面把地址提醒放在设置里：报告要说清楚是托管模式还是上传头像在用这个地址
    env.ops.add_item(1, "ABC-001", [{"name": "京香 大学生モデル"}, {"name": "无名氏"}])
    rt = env.make(_base_url="https://nas.example.com:8443")
    report = await rt.report()
    assert report["link"]["warning"] and "非标准端口" in report["link"]["warning"]
    assert report["link"]["hosted"] is False and report["link"]["uploads"] is False
    assert report["thumb_warning"] is None  # 镜像模式、没上传：顶部不该有提醒
    await rt.run(None, {"reason": "manual"})
    counts = (await rt.report())["unmatched"]
    assert counts == {"actors": 1, "junk": 1}
    await rt.set_manual_upload("无名氏", PNG)
    report = await rt.report()
    assert report["link"]["uploads"] is True and report["thumb_warning"]
    assert report["settings"]["process_arrivals"] is True
