"""纯播放策略：只使用可靠的外部编号，不按片名猜测、也不串成员。"""

import hashlib
from datetime import datetime, timezone


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso_time(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def trakt_minute(value: str) -> int:
    """Trakt #694 floors watched_at to the UTC minute; local chronology stays precise."""
    return int(timestamp(value) // 60) * 60


def identity(event: dict) -> dict | None:
    media = event.get("media") or {}
    ids = {}
    if isinstance(media.get("tmdb_id"), int) and media["tmdb_id"] > 0:
        ids["tmdb"] = media["tmdb_id"]
    imdb = media.get("imdb_id")
    if isinstance(imdb, str) and imdb.startswith("tt") and imdb[2:].isdigit():
        ids["imdb"] = imdb
    if not ids:
        return None
    if media.get("kind") == "movie":
        return {"movie": {"ids": ids}}
    unit = event.get("unit") or {}
    if (
        media.get("kind") == "tv"
        and isinstance(unit.get("season"), int)
        and isinstance(unit.get("episode"), int)
        and unit["season"] >= 0
        and unit["episode"] > 0
    ):
        return {
            "show": {"ids": ids},
            "episode": {"season": unit["season"], "number": unit["episode"]},
        }
    return None


def session_key(event: dict) -> str:
    # 不把设备名、影片标题或文件路径放进持久队列。
    fields = [
        event.get("member_id"),
        (event.get("media") or {}).get("id"),
        event.get("unit"),
        (event.get("client") or {}).get("device_id"),
    ]
    if event.get("viewing_id"):
        fields.append(event["viewing_id"])
    return hashlib.sha256(repr(fields).encode()).hexdigest()[:24]


def matches(item: dict, row: dict) -> bool:
    target = item.get("movie", item.get("show", {}))
    actual = row.get("movie", row.get("show", {}))
    if not any(actual.get("ids", {}).get(k) == value for k, value in target.get("ids", {}).items()):
        return False
    return "episode" not in item or all(
        row.get("episode", {}).get(k) == value for k, value in item["episode"].items()
    )


def progress(event: dict) -> float | None:
    position, duration = event.get("position_ms"), event.get("duration_ms")
    if not isinstance(position, (int, float)) or not isinstance(duration, (int, float)):
        return None
    if duration <= 0 or position < 0:
        return None
    return round(min(100.0, position / duration * 100), 3)


def history_body(item: dict, watched_at: str) -> dict:
    watched_at = iso_time(trakt_minute(watched_at))
    if "movie" in item:
        return {"movies": [{**item["movie"], "watched_at": watched_at}]}
    episode = item["episode"]
    return {
        "shows": [
            {
                **item["show"],
                "seasons": [
                    {
                        "number": episode["season"],
                        "episodes": [{"number": episode["number"], "watched_at": watched_at}],
                    }
                ],
            }
        ]
    }
