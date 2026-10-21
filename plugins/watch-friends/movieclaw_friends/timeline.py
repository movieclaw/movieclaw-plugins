"""跨来源时间线：真实时间排序，缺失日期置后，不使用导入时间冒充观影时间。"""

from datetime import datetime

from .external_watch import canonical, timestamp


def moment(value):
    value = timestamp(value)
    return datetime.fromisoformat(value).timestamp() if value else None


def order_key(row):
    value = moment(row.get("watched_at") or row.get("ended_at") or row.get("started_at"))
    return value is not None, value or 0, row.get("id") or 0


def merge_history(*groups):
    rows = sorted((row for group in groups for row in group), key=order_key, reverse=True)
    seen, result = set(), []
    for row in rows:
        media = row["media"]
        key = canonical(media)
        if key is None:
            key = (
                ("host", media["media_item_id"])
                if media.get("media_item_id")
                else ("record", row.get("id"))
            )
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result
