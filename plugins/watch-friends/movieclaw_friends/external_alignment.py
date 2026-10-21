"""回写前后核验真实单个条目，绝不以整剧/整季接口级联标记。"""

import re

from .external_watch import canonical, positive
from .peers import PeerError


def plex_tmdb(row):
    for entry in row.get("Guid", []):
        match = re.fullmatch(r"tmdb://([1-9][0-9]*)", str(entry.get("id", "")))
        if match:
            return int(match[1])
    return None


async def inspect_item(client, source, base, token, external_id, expected, *, include_resume=False):
    """仅返回身份、库归属与已看布尔值，身份或来源不确定时停止写入。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", external_id):
        raise PeerError("外部条目编号无效")
    platform = source["platform"]
    if platform == "emby":
        account = source["account_id"]
        user = await client.emby_account(base, token, account)
        if str(user["Id"]) != account:
            raise PeerError("Emby 账号已变化，请重新预览")
        row = await client.get(
            base,
            f"/Users/{account}/Items/{external_id}",
            token,
            platform,
            Fields="ProviderIds,SeriesProviderIds,UserDataPlaybackPositionTicks,UserDataPlayCount,UserDataLastPlayedDate",
        )
        if str(row.get("Id")) != external_id or row.get("Type") not in {"Movie", "Episode"}:
            raise PeerError("外部条目已变化，请重新预览")
        episode = row["Type"] == "Episode"
        ids = row.get("SeriesProviderIds") if episode else row.get("ProviderIds")
        if episode and not positive((ids or {}).get("Tmdb")):
            parent = str(row.get("SeriesId", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parent):
                raise PeerError("无法确认剧集身份")
            show = await client.get(base, f"/Users/{account}/Items/{parent}", token, platform)
            ids = show.get("ProviderIds")
        media = {
            "kind": "tv" if episode else "movie",
            "tmdb_id": positive((ids or {}).get("Tmdb")),
            "season_number": row.get("ParentIndexNumber"),
            "episode_number": row.get("IndexNumber"),
        }
        played = (row.get("UserData") or {}).get("Played")
        ancestors = await client.get(
            base, f"/Items/{external_id}/Ancestors", token, platform, UserId=account
        )
        # Emby 此接口返回数组，统一请求层仅对这个固定路径封装为 Items。
        libraries = ancestors.get("Items", [])
        library_ids = {str(item.get("Id")) for item in libraries if isinstance(item, dict)}
    else:
        if not external_id.isdigit():
            raise PeerError("Plex 条目编号无效")
        detail = await client.get(
            base, "/library/metadata/" + external_id, token, platform, includeGuids=1
        )
        rows = detail.get("MediaContainer", {}).get("Metadata", [])
        if len(rows) != 1 or str(rows[0].get("ratingKey")) != external_id:
            raise PeerError("外部条目已变化，请重新预览")
        row = rows[0]
        if row.get("type") not in {"movie", "episode"}:
            raise PeerError("仅支持电影或准确单集")
        episode = row["type"] == "episode"
        number = plex_tmdb(row)
        if episode:
            parent = str(row.get("grandparentRatingKey", ""))
            if not parent.isdigit():
                raise PeerError("无法确认剧集身份")
            show = await client.get(
                base, "/library/metadata/" + parent, token, platform, includeGuids=1
            )
            shows = show.get("MediaContainer", {}).get("Metadata", [])
            number = plex_tmdb(shows[0]) if len(shows) == 1 else None
        media = {
            "kind": "tv" if episode else "movie",
            "tmdb_id": number,
            "season_number": row.get("parentIndex"),
            "episode_number": row.get("index"),
        }
        played = bool(positive(row.get("viewCount")))
        library_ids = {str(row.get("librarySectionID", ""))} - {""}
    if canonical(media) != canonical(expected) or not canonical(media) or type(played) is not bool:
        raise PeerError("作品身份或已看状态无法确认，请重新预览")
    if not library_ids:
        raise PeerError("无法确认条目所属媒体库，请重新预览")
    if not include_resume:
        return {"played": played, "library_ids": library_ids}
    progress = (
        {
            "position_ms": int((row.get("UserData") or {}).get("PlaybackPositionTicks", 0))
            // 10000,
            "duration_ms": int(row.get("RunTimeTicks") or 0) // 10000,
            "edition": str(row.get("EditionName") or ""),
            "version_count": len(row.get("MediaSources") or [None]),
            "userdata": row.get("UserData") or {},
        }
        if platform == "emby"
        else {
            "position_ms": int(row.get("viewOffset") or 0),
            "duration_ms": int(row.get("duration") or 0),
            "edition": str(row.get("editionTitle") or ""),
            "version_count": len(row.get("Media") or [None]),
        }
    )
    return {"played": played, "library_ids": library_ids, **progress}
