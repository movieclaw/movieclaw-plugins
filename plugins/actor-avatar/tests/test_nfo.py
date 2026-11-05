from __future__ import annotations

import pytest
from actor_avatar import nfo

A = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Content/7-Moodyz/a.jpg"
B = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Content/7-Moodyz/b.jpg"

FULL = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!-- tinyMediaManager -->
<movie>
  <title>AARM-267 标题 &amp; 副标题</title>
  <plot><![CDATA[原始简介]]></plot>
  <genre>剧情</genre>
  <set><name>系列</name></set>
  <premiered>2021-03-15</premiered>
  <uniqueid type="num" default="true">AARM-267</uniqueid>
  <!-- 注释 -->
  <actor>
    <name>黒木奈美</name>
    <role>本人</role>
    <order>0</order>
  </actor>
  <actor><name>别人家</name><thumb>http://other.example/x.jpg</thumb></actor>
  <actor>
    <name>空头像</name>
    <thumb></thumb>
  </actor>
</movie>
"""


def tree(text):
    return nfo.parse(text)


def fields(text):
    root = tree(text)
    return {
        "title": root.findtext("title"),
        "plot": root.findtext("plot"),
        "genre": root.findtext("genre"),
        "set": root.findtext("set/name"),
        "premiered": root.findtext("premiered"),
        "uniqueid": root.find("uniqueid").attrib,
        "roles": [a.findtext("role") for a in root.findall("actor")],
        "order": [a.findtext("order") for a in root.findall("actor")],
        "names": [a.findtext("name") for a in root.findall("actor")],
    }


def test_existing_nfo_only_gains_thumbs():
    edit = nfo.apply_thumbs(FULL, {"黒木奈美": A, "别人家": B, "空头像": B})
    assert edit.written == {"黒木奈美": A, "空头像": B}
    assert edit.kept == ["别人家"]
    assert not edit.created and edit.added_actors == 0
    assert fields(edit.text) == fields(FULL)
    thumbs = nfo.actor_names(edit.text)
    assert thumbs == {"黒木奈美": A, "别人家": "http://other.example/x.jpg", "空头像": B}
    # 根元素前的声明和注释原样保留
    assert edit.text.startswith(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<!-- tinyMediaManager -->\n<movie>"
    )
    assert "<!-- 注释 -->" in edit.text


def test_strip_restores_original_tree():
    edit = nfo.apply_thumbs(FULL, {"黒木奈美": A, "空头像": B})
    back, removed = nfo.strip_thumbs(edit.text, edit.written)
    assert sorted(removed) == ["空头像", "黒木奈美"]
    # 「空头像」原本有个空 <thumb/>：撤销后整个删掉，语义相同（主程序都视为没头像）
    assert nfo.actor_names(back) == {
        "黒木奈美": None,
        "别人家": "http://other.example/x.jpg",
        "空头像": None,
    }
    assert fields(back) == fields(FULL)


def test_strip_exact_roundtrip_bytes():
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<movie>\n  <title>T</title>\n'
        "  <actor>\n    <name>甲</name>\n    <role>r</role>\n  </actor>\n</movie>\n"
    )
    edit = nfo.apply_thumbs(text, {"甲": A})
    back, _ = nfo.strip_thumbs(edit.text, edit.written)
    assert back == text


def test_strip_leaves_changed_thumbs():
    edit = nfo.apply_thumbs(FULL, {"黒木奈美": A})
    changed = edit.text.replace(A, "https://user.example/mine.jpg")
    back, removed = nfo.strip_thumbs(changed, {"黒木奈美": A})
    assert removed == [] and back == changed


def test_owned_thumb_can_be_replaced_foreign_cannot():
    first = nfo.apply_thumbs(FULL, {"黒木奈美": A})
    second = nfo.apply_thumbs(first.text, {"黒木奈美": B}, owned={"黒木奈美": A})
    assert second.written == {"黒木奈美": B}
    third = nfo.apply_thumbs(first.text, {"黒木奈美": B}, owned={})
    assert third.written == {} and third.kept == ["黒木奈美"] and not third.changed


def test_idempotent():
    first = nfo.apply_thumbs(FULL, {"黒木奈美": A})
    again = nfo.apply_thumbs(first.text, {"黒木奈美": A}, owned={"黒木奈美": A})
    assert not again.changed and again.text == first.text


def test_create_contains_title_and_all_db_actors():
    edit = nfo.apply_thumbs(
        None,
        {"甲": A},
        db_actors=[{"name": "甲", "role": "主演"}, {"name": "乙", "role": None}],
        title="ABC-123 片名 甲",
    )
    assert edit.created and edit.added_actors == 2
    root = tree(edit.text)
    assert root.tag == "movie"
    assert root.findtext("title") == "ABC-123 片名 甲"
    assert [
        (a.findtext("name"), a.findtext("role"), a.findtext("thumb")) for a in root.findall("actor")
    ] == [
        ("甲", "主演", A),
        ("乙", None, None),
    ]
    # 不写任何会覆盖档案的字段
    assert {child.tag for child in root} == {"title", "actor"}


def test_existing_nfo_without_actors_gets_db_actors():
    text = "<movie>\n  <title>T</title>\n  <plot>P</plot>\n</movie>"
    edit = nfo.apply_thumbs(
        text, {"乙": B}, db_actors=[{"name": "甲", "role": "r"}, {"name": "乙"}]
    )
    root = tree(edit.text)
    assert root.findtext("plot") == "P"
    assert [a.findtext("name") for a in root.findall("actor")] == ["甲", "乙"]
    assert edit.written == {"乙": B} and edit.added_actors == 2


def test_no_actor_match_means_no_change():
    edit = nfo.apply_thumbs(FULL, {"查无此人": A})
    assert not edit.changed and edit.text == FULL


def test_invalid_xml_and_encoding():
    with pytest.raises(nfo.NfoError):
        nfo.apply_thumbs("<movie><actor></movie>", {"a": A})
    with pytest.raises(nfo.NfoError):
        nfo.decode("<movie><title>中文</title></movie>".encode("gbk"))
    assert nfo.decode("\ufeff<movie/>".encode("utf-8")) == "<movie/>"


def test_invariant_guard(monkeypatch):
    # 人为把改写弄坏（顺手改了标题），不变式检查必须拦住
    original = nfo._set_thumb

    def evil(actor, url, tracker):
        original(actor, url, tracker)
        root_title = actor_parent["root"].find("title")
        root_title.text = "被改了"

    actor_parent = {}
    real_parse = nfo.parse

    def spy_parse(text):
        root = real_parse(text)
        actor_parent["root"] = root
        return root

    monkeypatch.setattr(nfo, "_set_thumb", evil)
    monkeypatch.setattr(nfo, "parse", spy_parse)
    with pytest.raises(nfo.NfoError):
        nfo.apply_thumbs(FULL, {"黒木奈美": A})


def test_duplicate_actor_names_only_first():
    text = "<movie><actor><name>甲</name></actor><actor><name>甲</name></actor></movie>"
    edit = nfo.apply_thumbs(text, {"甲": A})
    thumbs = [a.findtext("thumb") for a in nfo.parse(edit.text).findall("actor")]
    assert thumbs == [A, None]


def test_other_root_tags_supported():
    text = "<episodedetails><title>x</title><actor><name>甲</name></actor></episodedetails>"
    edit = nfo.apply_thumbs(text, {"甲": A})
    assert edit.text.startswith("<episodedetails>")
    assert nfo.actor_names(edit.text) == {"甲": A}
