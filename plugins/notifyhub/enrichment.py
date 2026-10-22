"""Read-only media snapshots from v0.40.0 public host operations."""
import asyncio
import time
from urllib.parse import urljoin
from wecom_client import valid_link

OPERATIONS = ("discover.get-title-details", "library.items.get", "subscriptions.get", "members.show", "jobs.list", "jobs.events", "library.list")


class Enricher:
    def __init__(self, ctx, key):
        self.ctx, self.key = ctx, key
        self.client = None
        self.cache = {}

    async def call(self, operation, args):
        try:
            if self.client is None:
                self.client = await self.ctx.use(self.key).client(self.ctx)
            return await asyncio.wait_for(self.client.call(operation, args, timeout=3), timeout=4)
        except Exception as exc:
            self.ctx.logger.info("通知补充资料暂不可用（%s，%s），使用已有字段", operation, type(exc).__name__)
            return None

    async def get(self, name, data):
        media = data.get("media") or {}
        if not media or not self.ctx.config.enrich_metadata:
            return {}
        key = (media.get("id"), data.get("library_id"))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            result = dict(cached[1])
        else:
            result = {}
            if media.get("tmdb_id") and media.get("kind") in {"movie", "tv"}:
                detail = await self.call("discover.get-title-details", {"title_ref": f"tmdb:{media['kind']}:{media['tmdb_id']}"})
                if isinstance(detail, dict):
                    title, metadata = detail.get("title") or {}, detail.get("metadata") or {}
                    result.update(vote_average=title.get("provider_rating"), genres=title.get("genres"), overview=title.get("overview"),
                                  poster=valid_link(title.get("poster_url")), backdrop=valid_link(title.get("backdrop_url")))
                    result["actors"] = [v.get("name") for v in metadata.get("cast", []) if isinstance(v, dict) and v.get("name")][:5]
            library_id = data.get("library_id")
            if library_id is not None and media.get("id") is not None:
                detail = await self.call("library.items.get", {"library_id": library_id, "media_item_id": media["id"]})
                if isinstance(detail, dict):
                    local = detail.get("local_meta") or {}
                    for key2, value in (("vote_average", local.get("rating")), ("genres", local.get("genres")), ("overview", local.get("plot"))):
                        if value:
                            result[key2] = value
                    actors = [v.get("name") for v in local.get("actors", []) if isinstance(v, dict) and v.get("name")][:5]
                    if actors:
                        result["actors"] = actors
                    # Public external image URLs only; authenticated local artwork is not a usable WeCom picurl.
                    for field in ("poster", "backdrop"):
                        external = valid_link(detail.get(field + "_url"))
                        if external:
                            result[field] = external
            if len(self.cache) >= 200:
                self.cache.clear()
            self.cache[key] = (time.monotonic() + 60, dict(result))
        if name == "subscription.created" and data.get("subscription_id") is not None:
            sub = await self.call("subscriptions.get", {"subscription_id": data["subscription_id"]})
            if isinstance(sub, dict):
                result["seasons"] = sorted({row["season_number"] for row in sub.get("wanted", []) if isinstance(row, dict) and isinstance(row.get("season_number"), int) and row["season_number"] > 0})
            member_id = data.get("member_id")
            if member_id is None or member_id == 0:
                result["username"] = "管理员"
            else:
                member = await self.call("members.show", {"member_id": member_id})
                if isinstance(member, dict):
                    result["username"] = member.get("nickname") or member.get("username")
        return result


def detail_link(name, data, cfg):
    media = data.get("media") or {}
    if cfg.movieclaw_url:
        if data.get("library_id") is not None and media.get("id") is not None:
            return cfg.movieclaw_url.rstrip("/") + f"/library/{data['library_id']}/item/{media['id']}"
        if data.get("subscription_id") is not None:
            return cfg.movieclaw_url.rstrip("/") + f"/subscriptions/{data['subscription_id']}"
    if media.get("tmdb_id") and media.get("kind") in {"movie", "tv"}:
        return f"https://www.themoviedb.org/{media['kind']}/{media['tmdb_id']}"
    return cfg.movieclaw_url
