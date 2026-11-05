import pytest
from actor_avatar.junk import junk_reason


@pytest.mark.parametrize(
    "name",
    [
        "京香 大学生モデル",
        "かすみ 28歳 食品メーカー勤務Webデザイナー",
        "あいか 23歳 和食居酒屋ホール",
        "あおいさん",
        "ゆい（22）",
        "素人",
        "Unknown",
        "みく 看護師",
        "a" * 30,
    ],
)
def test_junk_names(name):
    assert junk_reason(name)


@pytest.mark.parametrize(
    "name",
    [
        "涼森れむ",
        "美月恋(美月レイア)",
        "緒方 恵美",
        "AIKA",
        "Julia",
        "あおい",
        "小梅",
        "三上悠亜",
        "",
    ],
)
def test_real_names_pass(name):
    assert junk_reason(name) is None
