from __future__ import annotations

import pytest
from actor_avatar.matching import (
    AI_FIX_PREFIX,
    AvatarIndex,
    basic,
    bracket_aliases,
    expand_repeats,
    fold,
    strip_brackets,
)


@pytest.fixture
def index(filetree_data):
    return AvatarIndex(filetree_data)


def test_exact(index):
    m = index.lookup("涼森れむ")
    assert m.how == "exact" and m.confirmed
    assert m.avatar.name == "涼森れむ"


def test_later_company_overrides_earlier(index):
    # 公司按质量升序：后面的 0-Hand-Storage 覆盖 9-AVDBS
    assert index.lookup("三上悠亜").avatar.company == "0-Hand-Storage"
    # 涼森れむ：9-AVDBS 在 z-DMM(骑) 之后
    assert index.lookup("涼森れむ").avatar.company == "9-AVDBS"


def test_simplified_and_variant_fold(index):
    assert index.lookup("凉森れむ").avatar.name == "涼森れむ"
    assert index.lookup("枫カレン").avatar.name == "楓カレン"
    assert index.lookup("八挂うみ").avatar.name == "八掛うみ"
    assert index.lookup("三上悠亚").avatar.name == "三上悠亜"
    assert index.lookup("黑木奈美").avatar.name == "黒木奈美"
    assert index.lookup("绪方恵美").avatar.name == "緒方 恵美"


def test_basic_spacing_and_width(index):
    m = index.lookup("緒方恵美")
    assert m.avatar.name == "緒方 恵美" and m.how in {"basic", "fold"}
    assert index.lookup(" 涼森れむ ").avatar.name == "涼森れむ"


def test_bracket_aliases(index):
    assert index.lookup("美月恋").avatar.name == "美月恋(美月レイア)"
    assert index.lookup("美月レイア").how == "bracket"
    assert index.lookup("美月恋（美月レイア）").avatar.name == "美月恋(美月レイア)"
    assert strip_brackets("A（B）") == "A"
    assert bracket_aliases("A（B）") == ["B"]


def test_alias_takes_priority(index):
    m = index.lookup("桥本有菜", {"桥本有菜": "新ありな"})
    assert m.how == "alias" and m.avatar.name == "新ありな"


def test_miss_returns_none(index):
    assert index.lookup("凉森玲梦") is None
    assert index.lookup("") is None


def test_ai_fix_pick(index):
    a = index.lookup("八掛うみ").avatar
    assert a.file.startswith(AI_FIX_PREFIX)
    assert a.pick(True) == "AI-Fix-八掛うみ.jpg"
    assert a.pick(False) == "八掛うみ.jpg"
    # 文件名与演员名不同时以 Filetree 里的真实文件为准
    assert index.lookup("桃乃木かな").avatar.pick(False) == "松嶋真麻.jpg"
    assert "?" not in index.lookup("三上悠亜").avatar.file


def test_infer_from_title(index):
    found = index.infer_from_title("SSIS-001 新人デビュー 涼森れむ")
    assert [a.name for a in found] == ["涼森れむ"]
    assert index.infer_from_title("X 涼森れむ", exclude={fold("涼森れむ")}) == []
    # 太短的词不推断
    assert index.infer_from_title("ABC-1 れむ") == []
    # 只看最后两个词
    assert index.infer_from_title("涼森れむ 甲 乙 丙") == []


def test_normalizers():
    assert basic("Ａｂ・c .d") == "abcd"
    assert fold("涼森") == fold("凉森")
    assert len(AvatarIndex({"Content": {"c": {"a.jpg": "a.jpg"}, "bad": None}})) == 1


def test_repeat_marks():
    assert expand_repeats("野々浦暖") == "野野浦暖"
    assert expand_repeats("いすゞ") == "いすず"
    assert expand_repeats("々") == "々"
    assert basic("野々浦 暖") == basic("野野浦暖")


def test_repeat_mark_lookup(index):
    m = index.lookup("野野浦暖")
    assert m is not None and m.avatar.name == "野々浦暖" and m.how == "basic"
    assert index.lookup("野々浦暖").how == "exact"


class _Table:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, name):
        return self.mapping.get(fold(name))


def test_name_table_lookup(index):
    table = _Table({fold("凉森玲梦"): "涼森れむ", fold("八挂海"): "八掛うみ", fold("鬼"): "不存在"})
    m = index.lookup("凉森玲梦", None, table)
    assert m.how == "table" and m.confirmed and m.avatar.name == "涼森れむ"
    # 括号里的译名也查表
    assert index.lookup("某某（八挂海）", None, table).avatar.name == "八掛うみ"
    # 表里的日文名头像库没有：不命中
    assert index.lookup("鬼", None, table) is None
    # 直接命中优先，不走表
    assert index.lookup("涼森れむ", None, _Table({fold("涼森れむ"): "八掛うみ"})).how == "exact"
    assert index.lookup("凉森玲梦") is None
