"""虚拟好友与观看行为使用真实影片目录；不读取或改写宿主用户数据。"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from .demo_catalog import FILMS
from .store import Store

DEMO_ID = "demo-movie-friend"
DEMO_USERS = (
    {"id": DEMO_ID, "nickname": "小映", "avatar_id": "xiaoying"},
    {"id": "demo-river-friend", "nickname": "阿川", "avatar_id": "achuan"},
    {"id": "demo-deer-friend", "nickname": "小鹿", "avatar_id": "xiaolu"},
    {"id": "demo-star-friend", "nickname": "星子", "avatar_id": "xingzi"},
)

# 每人独占 20 部真实作品：当前 1、历史 5、喜欢 4、片单 2×5，区块互不重复。
_GROUP_SIZE = 20
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
_THEMES = (
    (
        ("向时间与星空出发", "时间旅行和太空探索，留给周末的想象。"),
        ("未知边界", "从人工智能到宇宙联络，寻找科学与人性的边界。"),
    ),
    (
        ("人间百态", "关于公正、成长、爱与自我选择的故事。"),
        ("执念与舞台", "悬疑与音乐之间，看看人如何追逐心中的答案。"),
    ),
    (
        ("勇敢做自己", "超能力、料理、情绪与冰雪中的自我发现。"),
        ("伙伴与成长", "有伙伴作伴，冒险的路就更有勇气。"),
    ),
    (
        ("韩影悬念", "在不同人的命运里，感受韩国电影的悬念与冲击。"),
        ("华语光影", "功夫、江湖与都市情感，五种华语电影的光影。"),
    ),
)
_RECOMMENDATIONS = (
    ("这部电影把想象力留在了银幕之外，看完还会想聊很久。", "喜欢它对未知世界的认真想象。"),
    ("人物的每一个选择都值得回味，想推荐给喜欢故事的朋友。", "平凡人生也能有特别动人的时刻。"),
    ("那些小小的勇气和陪伴，总能让我重新开心起来。", "适合和家人一起看，也适合留给长大的自己。"),
    ("喜欢这部作品细腻的情绪，也喜欢它留下的余韵。", "看完后还在想其中的人物，想听听大家的感受。"),
)


def is_demo(user: dict) -> bool:
    """仅根据插件专用角色识别演示账号，不以名字猜测真实成员身份。"""
    return user.get("role") == "demo"


def _user_index(user_id: str) -> int:
    return next((i for i, user in enumerate(DEMO_USERS) if user["id"] == user_id), 0)


def _presence(user_index: int, observed: datetime) -> dict:
    online = user_index < 2
    # 离线时间在同一 UTC 日期内保持稳定，避免刷新时看起来一直有新活动。
    last_seen = (
        observed
        if online
        else observed.replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(hours=3 * (user_index - 1))
    )
    return {"online": online, "last_seen": last_seen.isoformat()}


def profile(user_id: str = DEMO_ID, *, observed: datetime | None = None) -> dict:
    """前两位演示人物在线，后两位离线；此状态不代表任何真实宿主账号。"""
    observed = (observed or datetime.now(UTC)).astimezone(UTC)
    user_index = _user_index(user_id)
    return dict(DEMO_USERS[user_index], is_demo=True, presence=_presence(user_index, observed))


def details() -> dict:
    """真实电影的只读资料与来源；返回副本，避免调用方改变公共演示目录。"""
    return {film["demo_id"]: deepcopy(film) | {"is_demo": True} for film in FILMS}


def _media(index: int) -> dict:
    film = FILMS[index % len(FILMS)]
    return {
        key: film[key] for key in ("demo_id", "tmdb_id", "kind", "title", "year", "poster_path")
    }


def collections(user_id: str = DEMO_ID) -> list[dict]:
    """每位演示好友有两份独立主题片单，电影不与其在播、历史或喜欢重复。"""
    user_index = _user_index(user_id)
    base = user_index * _GROUP_SIZE + 10
    return [
        {
            "id": -(user_index * 10 + index + 1),
            "name": name,
            "description": description,
            "items": [_media(base + index * 5 + offset) for offset in range(5)],
        }
        for index, (name, description) in enumerate(_THEMES[user_index])
    ]


def _timeline(user_index: int, observed: datetime) -> tuple[int, float]:
    # 当前电影有自己的固定槽位，播完后正常重播，不轮入其他已展示的区块。
    film_index = user_index * _GROUP_SIZE
    duration = FILMS[film_index]["runtime_minutes"] * 60
    return film_index, (observed.timestamp() - _EPOCH + user_index * 5_400) % duration


def snapshot(user_id: str = DEMO_ID, *, observed: datetime | None = None) -> dict:
    """以真实片长和 1 倍速推进在线人物的播放，离线人物仅保留已结束历史。

    时间轴由固定纪元和人物偏移确定，刷新或重启不会把进度重置；不需要后台
    任务、实际播放文件或改写宿主观看记录。完整片长结束后仅重播本人的当前槽位。
    """
    observed = (observed or datetime.now(UTC)).astimezone(UTC)
    user_index = _user_index(user_id)
    presence = _presence(user_index, observed)
    last_seen = datetime.fromisoformat(presence["last_seen"])
    film_index, elapsed = _timeline(user_index, last_seen)
    duration_ms = FILMS[film_index]["runtime_minutes"] * 60_000
    position_ms = int(elapsed * 1_000)
    history = []
    for index in range(1, 6):
        ended = last_seen - timedelta(days=index, hours=user_index)
        watched_index = user_index * _GROUP_SIZE + index
        length = FILMS[watched_index]["runtime_minutes"] * 60_000
        history.append(
            {
                "id": -(user_index * 10 + index),
                "media": _media(watched_index),
                "started_at": (ended - timedelta(milliseconds=length)).isoformat(),
                "ended_at": ended.isoformat(),
                "watched_ms": length,
                "start_position_ms": 0,
                "end_position_ms": length,
                "duration_ms": length,
                "progress_percent": 100,
                "completed": True,
            }
        )
    return {
        "is_demo": True,
        "avatar_id": DEMO_USERS[user_index]["avatar_id"],
        "presence": presence,
        "current": {
            "id": -(100 + user_index),
            "media": _media(film_index),
            "started_at": (observed - timedelta(seconds=elapsed)).isoformat(),
            "ended_at": None,
            "watched_ms": position_ms,
            "start_position_ms": 0,
            "end_position_ms": position_ms,
            "duration_ms": duration_ms,
            "progress_percent": position_ms / duration_ms * 100,
            "completed": False,
            "playback_rate": 1.0,
            "expires_at": (observed + timedelta(seconds=90)).isoformat(),
        }
        if presence["online"]
        else None,
        "history": history,
        "favorites": [_media(user_index * _GROUP_SIZE + index) for index in range(6, 10)],
        "collections": collections(user_id),
        "recommendations": [
            {
                "id": f"{user_id}-recommendation-{index + 1}",
                "body": body,
                "media": _media(user_index * _GROUP_SIZE + 6 + index),
                "created_at": (
                    last_seen.replace(hour=12, minute=0, second=0, microsecond=0)
                    - timedelta(days=index + 1)
                ).isoformat(),
            }
            for index, body in enumerate(_RECOMMENDATIONS[user_index])
        ],
        "sharing": {"current": True, "history": True},
        "observed_at": observed.isoformat(),
    }


def seed(store: Store) -> None:
    """幂等添加演示账号，并仅迁移旧演示人物的虚构影片公开历史。

    更新条件同时绑定固定 ID 和 demo 角色；真实账号、本人开关、好友关系与
    插件片单不会被重置。已使用真实目录的演示行也不在重启时重复改写。
    """
    catalog_ids = {film["demo_id"]: film["tmdb_id"] for film in FILMS}
    for index, user in enumerate(DEMO_USERS):
        data = snapshot(user["id"])
        username = "__movieclaw_friends_demo__" if index == 0 else f"__movieclaw_demo_{index}__"
        store.execute(
            "INSERT OR IGNORE INTO users("
            "id,username,nickname,member_id,role,token,invite_token,"
            "discoverable,share_current,share_history,public_history,public_updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"],
                username,
                user["nickname"],
                None,
                "demo",
                store.encrypt(""),
                store.secret(),
                1,
                1,
                1,
                json.dumps(data["history"], ensure_ascii=False),
                data["observed_at"],
            ),
        )
        stored = store.one("SELECT role,public_history FROM users WHERE id=?", (user["id"],))
        if not stored or stored["role"] != "demo":
            continue
        try:
            history = json.loads(stored["public_history"])
        except (ValueError, TypeError):
            history = []
        history_ids = {row["media"]["tmdb_id"] for row in data["history"]}
        # 迁移旧假片和旧版重复分组；修复范围仅限插件自带的演示历史。
        if (
            not isinstance(history, list)
            or len(history) != 5
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("media"), dict)
                or catalog_ids.get(row["media"].get("demo_id")) != row["media"].get("tmdb_id")
                or row["media"].get("tmdb_id") not in history_ids
                for row in history
            )
        ):
            store.execute(
                "UPDATE users SET public_history=?,public_updated_at=? WHERE id=? AND role='demo'",
                (
                    json.dumps(data["history"], ensure_ascii=False),
                    data["observed_at"],
                    user["id"],
                ),
            )
