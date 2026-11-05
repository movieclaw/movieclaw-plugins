import pytest
from actor_avatar.ai import TRIED_KEY, USAGE_KEY, AiAdvisor, AiError, build_prompt, parse_candidates
from actor_avatar.aliases import NameTable
from actor_avatar.matching import AvatarIndex, fold
from movieclaw_sdk.agent import AgentBusy, AgentNotGranted, AgentUnavailable

from .conftest import FakeStore, load_filetree
from .fakes_ai import FakeAgent


@pytest.fixture
def index():
    return AvatarIndex(load_filetree())


def advisor(agent, store=None, limit=5, day="2026-11-04"):
    slept = []

    async def sleep(s):
        slept.append(s)

    adv = AiAdvisor(
        agent=agent, store=store or FakeStore(), daily_limit=limit, sleep=sleep, today=lambda: day
    )
    adv.slept = slept
    return adv


def test_parse_candidates_tolerates_noise():
    text = '好的：\n```json\n["涼森れむ", "  涼森玲夢 ", "涼森れむ", 3]\n```'
    assert parse_candidates(text) == ["涼森れむ", "涼森玲夢", "3"]
    assert parse_candidates("不知道") == []
    assert parse_candidates("[not json]") == []
    assert len(parse_candidates(str([f"n{i}" for i in range(20)]).replace("'", '"'))) == 8


def test_prompt_has_name_and_titles_only():
    p = build_prompt("凉森玲梦", ["SSIS-001 标题" + "x" * 200, "", "B", "C", "D"])
    assert "凉森玲梦" in p and "SSIS-001" in p and "D" not in p.split("\n")[-2]
    assert len(max(p.split("\n"), key=len)) < 200


async def test_only_names_found_in_gfriends_are_accepted(index):
    agent = FakeAgent(
        {"凉森玲梦": '["涼森れむ", "完全不存在的人", "京香 大学生モデル", "凉森玲梦"]'}
    )
    adv = advisor(agent)
    result = await adv.suggest("凉森玲梦", ["SSIS-001"], index)
    assert [a.name for a in result["accepted"]] == ["涼森れむ"]
    why = {a["name"]: a["why"] for a in result["asked"]}
    assert why["完全不存在的人"] == "gfriends 里没有"
    assert why["京香 大学生モデル"] == "不像名字"
    assert result["usage"]["total_tokens"] == 135
    # 无工具档、会话问完就删
    assert agent.calls[0]["tools"] == "none" and agent.deleted == ["s1"]
    usage = await adv.status()
    assert usage["used_today"] == 1 and usage["tokens_today"]["total_tokens"] == 135
    assert usage["total"]["calls"] == 1
    assert (await adv.store.get(TRIED_KEY))["凉森玲梦"]["accepted"] == ["涼森れむ"]


async def test_table_names_count_as_found(index):
    agent = FakeAgent({"某人": '["凉森玲梦"]'})
    table = NameTable({fold("凉森玲梦"): "涼森れむ"})
    result = await advisor(agent).suggest("某人", [], index, table)
    assert [a.name for a in result["accepted"]] == ["涼森れむ"]


async def test_daily_cap_and_new_day(index):
    store = FakeStore()
    adv = advisor(FakeAgent(), store, limit=2)
    await adv.suggest("甲", [], index)
    await adv.suggest("乙", [], index)
    with pytest.raises(AiError, match="用满"):
        await adv.suggest("丙", [], index)
    tomorrow = advisor(FakeAgent(), store, limit=2, day="2026-11-05")
    status = await tomorrow.status()
    assert status["used_today"] == 0 and status["total"]["calls"] == 2
    await tomorrow.suggest("丙", [], index)


async def test_rate_limit_spaces_calls(index):
    adv = advisor(FakeAgent())
    await adv.suggest("甲", [], index)
    await adv.suggest("乙", [], index)
    assert adv.slept and adv.slept[0] > 0


async def test_disabled_when_limit_zero_or_no_agent(index):
    with pytest.raises(AiError):
        await advisor(FakeAgent(), limit=0).suggest("甲", [], index)
    adv = advisor(None)
    assert not adv.enabled
    assert (await adv.status())["available"] is False
    with pytest.raises(AiError):
        await adv.suggest("甲", [], index)


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (AgentNotGranted("no"), "授权"),
        (AgentUnavailable("no model"), "模型"),
        (AgentBusy("busy"), "正忙"),
    ],
)
async def test_host_errors_are_friendly_and_not_counted(index, exc, text):
    adv = advisor(FakeAgent(raise_exc=exc))
    with pytest.raises(AiError, match=text):
        await adv.suggest("甲", [], index)
    status = await adv.status()
    assert status["used_today"] == 0
    assert text in status["last_error"]["message"]


async def test_unfinished_turn_counts_usage_and_errors(index):
    store = FakeStore()
    adv = advisor(FakeAgent(status="timeout"), store)
    with pytest.raises(AiError, match="timeout"):
        await adv.suggest("甲", [], index)
    assert store.data[USAGE_KEY]["calls"] == 1
    assert store.data[USAGE_KEY]["tokens"]["total_tokens"] == 135


async def test_recently_tried(index):
    adv = advisor(FakeAgent())
    assert not await adv.recently_tried("甲")
    await adv.suggest("甲", [], index)
    assert await adv.recently_tried("甲")
