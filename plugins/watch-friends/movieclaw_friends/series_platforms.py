"""只读核对已绑定平台的正片目录，不导入新事实、不修改外部已看状态。"""

import re

from .external_watch import positive
from .peers import PeerError


async def read_catalog(client, source, base, token, episode_id, number):
    """从已导入单集定位整剧，校验 TMDB 身份后读取完整目录（包含未看单集）。"""
    platform = source["platform"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", episode_id):
        raise PeerError("单集来源编号无效")
    if platform == "emby":
        user = await client.emby_account(base, token, source["account_id"])
        if str(user["Id"]) != source["account_id"]:
            raise PeerError("外部账号已变化")
        prefix = f"/Users/{source['account_id']}/Items"
        episode = await client.get(base, prefix + "/" + episode_id, token, platform)
        parent = str(episode.get("SeriesId", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parent):
            raise PeerError("无法定位来源剧集")
        show = await client.get(base, prefix + "/" + parent, token, platform)
        if positive((show.get("ProviderIds") or {}).get("Tmdb")) != number:
            raise PeerError("来源剧集身份不一致")
        path = prefix
        params = dict(ParentId=parent, Recursive="true", IncludeItemTypes="Episode")
    else:
        if not episode_id.isdigit():
            raise PeerError("Plex 单集编号无效")
        episode = await client.get(base, "/library/metadata/" + episode_id, token, platform)
        metadata = episode.get("MediaContainer", {}).get("Metadata", [])
        parent = str(metadata[0].get("grandparentRatingKey", "")) if metadata else ""
        if not parent.isdigit():
            raise PeerError("无法定位来源剧集")
        detail = await client.get(
            base, "/library/metadata/" + parent, token, platform, includeGuids=1
        )
        shows = detail.get("MediaContainer", {}).get("Metadata", [])
        guids = shows[0].get("Guid", []) if shows else []
        if not any(row.get("id") == f"tmdb://{number}" for row in guids):
            raise PeerError("来源剧集身份不一致")
        path, params = "/library/metadata/" + parent + "/allLeaves", {}
    units, start, seen = set(), 0, set()
    while True:
        paging = (
            {"StartIndex": start, "Limit": 200}
            if platform == "emby"
            else {"X-Plex-Container-Start": start, "X-Plex-Container-Size": 200}
        )
        page = await client.get(base, path, token, platform, **params, **paging)
        container = page if platform == "emby" else page.get("MediaContainer", {})
        rows = container.get("Items" if platform == "emby" else "Metadata", [])
        total = container.get("TotalRecordCount" if platform == "emby" else "totalSize")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PeerError("来源目录格式不兼容")
        if total is not None and (type(total) is not int or total < 0):
            raise PeerError("来源目录总数格式不兼容")
        ids = tuple(str(row.get("Id" if platform == "emby" else "ratingKey")) for row in rows)
        if rows and ids in seen:
            raise PeerError("来源目录分页重复")
        seen.add(ids)
        for row in rows:
            season = row.get("ParentIndexNumber" if platform == "emby" else "parentIndex")
            episode = row.get("IndexNumber" if platform == "emby" else "index")
            if season == 0:
                continue
            if type(season) is not int or type(episode) is not int or season < 1 or episode < 1:
                raise PeerError("来源目录缺少准确季集号")
            units.add((season, episode))
        start += len(rows)
        if start > 30000 or (not rows and type(total) is int and start < total):
            raise PeerError("来源目录不完整或超过核对上限")
        if (type(total) is int and start >= total) or (total is None and len(rows) < 200):
            break
    return sorted(units)
