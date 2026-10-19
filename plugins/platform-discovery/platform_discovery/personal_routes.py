"""Private routes: production inline mode uses the host's verified Principal.

The legacy capability path exists only for isolated compatibility tests; the
production router always supplies account_dependency and ignores profile-token
and user-id headers for authorization. Old capabilities are migration input only.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse


PROFILE_HEADER = "X-Platform-Profile"
PROFILE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
TITLE_REF = re.compile(r"^(?:tmdb:(?:movie|tv):[1-9]\d{0,10}|douban:[1-9]\d{0,10})$")
PRIVATE_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache",
                   "Vary": "Cookie, Authorization, X-Platform-Account, " + PROFILE_HEADER, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer"}
MAX_ANSWERS_BYTES = 32 * 1024
TMDB_BASE = "https://api.themoviedb.org/3"
NATIVE_STATUS_TIMEOUT = 10
NATIVE_UNAVAILABLE = "MovieClaw 自身的影片资料接口暂时不可用。插件详情可继续查看；订阅需宿主资料服务恢复。"
_PRIVATE_KEYS = frozenset({"profile_token", "token", "token_hash", "profile_hash", "profile_key", "scope",
                           "proxy_url", "proxy_address", "tmdb_token", "authorization", "cookie", "password",
                           "library_status", "library_links", "member_id", "user_id"})


def _error(status, message):
    return HTTPException(status, message, headers=PRIVATE_HEADERS)


def _profile_token(request):
    values = request.headers.getlist(PROFILE_HEADER)
    if len(values) != 1 or not PROFILE_TOKEN.fullmatch(values[0]):
        raise _error(404, "请先在当前浏览器创建口味档案。")
    return values[0]


def _browser_request(request):
    # Browsers set this themselves. Native/CLI requests may omit it and still
    # need both host authentication and the unguessable private capability.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise _error(403, "请从平台放映厅页面操作口味档案。")


def _private_projection(value, active_token=None, depth=0):
    """A second guard against accidental credential/inventory fields in results."""
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {str(k): _private_projection(v, active_token, depth + 1) for k, v in value.items()
                if isinstance(k, str) and k.lower() not in _PRIVATE_KEYS}
    if isinstance(value, list):
        return [_private_projection(v, active_token, depth + 1) for v in value[:1000]]
    if isinstance(value, str):
        return value.replace(active_token, "[已隐藏]") if active_token else value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _text(value, limit=1000):
    return value.replace("\x00", "").strip()[:limit] if isinstance(value, str) else ""


def _number(value, lower=0, upper=10):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and lower <= number <= upper else None


def _image(value):
    """Only public metadata images, with no local paths or credential queries."""
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return None
    try:
        parts = urlsplit(value)
        public_host = parts.hostname == "image.tmdb.org" or bool(re.fullmatch(r"img\d*\.doubanio\.com", parts.hostname or ""))
        if parts.scheme not in {"http", "https"} or not public_host or parts.username or parts.password or parts.port not in {None, 80, 443}:
            return None
    except (TypeError, ValueError):
        return None
    return "https://" + parts.hostname + parts.path


def _people(rows):
    result = []
    for row in rows[:30] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not _text(row.get("name"), 200):
            continue
        person_id = _number(row.get("tmdb_person_id"), 1, 999999999)
        role, avatar = _text(row.get("role"), 200) or None, _image(row.get("avatar_url"))
        result.append({"name": _text(row.get("name"), 200), "character": role, "profile_url": avatar,
                       "role": role, "avatar_url": avatar,
                       "tmdb_person_id": int(person_id) if person_id and person_id.is_integer() else None})
    return result


def _images(rows):
    result = []
    for row in rows[:12] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        preview, full = _image(row.get("preview_url")), _image(row.get("full_url"))
        if not preview and not full:
            continue
        width, height = _number(row.get("width"), 1, 50000), _number(row.get("height"), 1, 50000)
        result.append({"preview_url": preview or full, "full_url": full or preview,
                       "width": int(width) if width else None, "height": int(height) if height else None})
    return result


def _runtime_minutes(value):
    value = _text(value, 100)
    minutes = re.fullmatch(r"(\d{1,4})\s*(?:分钟|分|min(?:utes)?)", value, re.I)
    hours = re.fullmatch(r"(\d{1,2})\s*(?:小时|时)(?:\s*(\d{1,2})\s*(?:分钟|分))?", value)
    total = int(minutes.group(1)) if minutes else int(hours.group(1)) * 60 + int(hours.group(2) or 0) if hours else None
    return total if total is not None and 0 < total <= 1440 else None


def public_title_details(payload, title_ref):
    """Project DiscoveredTitleDetailsView without any admin library projection."""
    if not TITLE_REF.fullmatch(title_ref):
        raise ValueError("作品引用无效")
    if not isinstance(payload, dict):
        raise ValueError("作品资料格式暂不可用")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    title = payload.get("title")
    if not isinstance(title, dict) or not _text(title.get("title"), 300):
        raise ValueError("作品资料格式暂不可用")
    if title.get("title_ref") not in {None, title_ref}:
        raise ValueError("作品资料身份不一致")
    parts = title_ref.split(":")
    provider, external_id = parts[0], parts[-1]
    media_type = parts[1] if provider == "tmdb" else title.get("media_type")
    if title.get("provider") not in {None, provider} or (title.get("external_id") is not None and str(title["external_id"]) != external_id):
        raise ValueError("作品资料身份不一致")
    if provider == "tmdb" and title.get("media_type") not in {None, media_type}:
        raise ValueError("作品资料身份不一致")
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    year = _number(title.get("release_year"), 1800, 2200)
    native_url = "/media/" + (parts[1] if provider == "tmdb" else "douban") + "/" + external_id
    return {"id": title_ref, "title_ref": title_ref, "detail_ref": title_ref,
            "title": _text(title.get("title"), 300), "original_title": _text(title.get("original_title"), 300),
            "media_type": media_type if media_type in {"movie", "tv"} else None,
            "year": int(year) if year and year.is_integer() else None,
            "rating": _number(title.get("provider_rating")), "rating_source": "TMDB" if provider == "tmdb" else "豆瓣",
            "genres": [_text(g, 80) for g in (title.get("genres") or [])[:20] if isinstance(g, str)] if isinstance(title.get("genres"), list) else [],
            "overview": _text(title.get("overview"), 8000), "runtime": _text(title.get("extent_label"), 100),
            "runtime_minutes": _runtime_minutes(title.get("extent_label")),
            "poster_url": _image(title.get("poster_url")), "backdrop_url": _image(title.get("backdrop_url")),
            "posters": _images(payload.get("posters")), "backdrops": _images(payload.get("backdrops")),
            "directors": [_text(name, 200) for name in meta.get("directors", [])[:20] if isinstance(name, str)] if isinstance(meta.get("directors"), list) else [],
            "cast": _people(meta.get("cast")), "crew": _people(meta.get("director_credits")),
            "country": _text(meta.get("country"), 300), "language": _text(meta.get("language"), 200),
            "released": _text(meta.get("released"), 100), "network": _text(meta.get("network"), 200),
            "native_url": native_url}


def _native_title_matches(payload, title_ref):
    """Require the host's actual title identity, not a successful empty envelope."""
    if not isinstance(payload, dict) or payload.get("success") is False:
        return False
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    title = payload.get("title")
    if not isinstance(title, dict) or not _text(title.get("title"), 300):
        return False
    parts = title_ref.split(":")
    return (title.get("title_ref") == title_ref
            and title.get("provider") == parts[0]
            and str(title.get("external_id")) == parts[-1]
            and (parts[0] != "tmdb" or title.get("media_type") == parts[1]))


async def _tmdb_details(discovery, title_ref):
    """Read the fixed TMDB title/credits endpoint with the plugin's own token."""
    if not title_ref.startswith("tmdb:") or not getattr(discovery, "_token", ""):
        return None
    _, kind, identity = title_ref.split(":")
    endpoint = f"{TMDB_BASE}/{kind}/{identity}"
    row = await discovery._json(endpoint, {"language": "zh-CN", "append_to_response": "credits"}, tmdb=True)
    if not isinstance(row, dict) or str(row.get("id")) != identity:
        raise ValueError("作品资料身份不一致")
    overview = _text(row.get("overview"), 8000)
    if not overview:
        try:
            english = await discovery._json(endpoint, {"language": "en-US"}, tmdb=True)
            if isinstance(english, dict) and str(english.get("id")) == identity:
                overview = _text(english.get("overview"), 8000)
        except Exception:
            pass  # A missing translation cannot discard valid public metadata.
    def image(path, size):
        return "https://image.tmdb.org/t/p/" + size + path if isinstance(path, str) and re.fullmatch(r"/[A-Za-z0-9._-]+", path) else None
    credits = row.get("credits") if isinstance(row.get("credits"), dict) else {}
    cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
    crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
    directors = [person for person in crew if isinstance(person, dict) and person.get("job") == "Director"]
    def person(person, role):
        return {"name": person.get("name"), "role": role, "avatar_url": image(person.get("profile_path"), "w185"),
                "tmdb_person_id": person.get("id")}
    runtime = row.get("runtime")
    if kind == "tv":
        episode_times = row.get("episode_run_time")
        runtime = next(iter(episode_times), None) if isinstance(episode_times, list) else None
    duration = _number(runtime, 1, 1440)
    released = row.get("release_date" if kind == "movie" else "first_air_date")
    released = released if isinstance(released, str) else ""
    year = int(released[:4]) if re.match(r"^\d{4}-\d{2}-\d{2}$", released) else None
    countries = row.get("origin_country")
    if kind == "movie":
        production_countries = row.get("production_countries")
        countries = [country.get("iso_3166_1") for country in production_countries if isinstance(country, dict)] if isinstance(production_countries, list) else []
    countries = [country for country in countries if isinstance(country, str)] if isinstance(countries, list) else []
    genres = row.get("genres") if isinstance(row.get("genres"), list) else []
    networks = row.get("networks") if isinstance(row.get("networks"), list) else []
    poster, backdrop = image(row.get("poster_path"), "w500"), image(row.get("backdrop_path"), "w1280")
    payload = {"title": {"title_ref": title_ref, "provider": "tmdb", "external_id": identity, "media_type": kind,
                         "title": row.get("title" if kind == "movie" else "name"),
                         "original_title": row.get("original_title" if kind == "movie" else "original_name"),
                         "release_year": year, "provider_rating": row.get("vote_average"),
                         "genres": [genre.get("name") for genre in genres if isinstance(genre, dict)],
                         "extent_label": str(int(duration)) + " 分钟" if duration and duration.is_integer() else "",
                         "overview": overview, "poster_url": poster, "backdrop_url": backdrop},
               "metadata": {"directors": [person.get("name") for person in directors],
                            "director_credits": [person(entry, "Director") for entry in directors],
                            "cast": [person(entry, entry.get("character")) for entry in cast[:30] if isinstance(entry, dict)],
                            "country": " / ".join(countries), "language": row.get("original_language"), "released": released,
                            "network": " / ".join(_text(network.get("name"), 100) for network in networks if isinstance(network, dict))},
               "posters": [{"preview_url": poster, "full_url": image(row.get("poster_path"), "original")}] if poster else [],
               "backdrops": [{"preview_url": backdrop, "full_url": image(row.get("backdrop_path"), "original")}] if backdrop else []}
    return public_title_details(payload, title_ref)


async def _answers(request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _error(415, "请以 JSON 提交口味问卷。")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_ANSWERS_BYTES:
            raise _error(413, "问卷内容过大，请减少选项后重试。")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"answers"} or not isinstance(payload["answers"], dict):
            raise ValueError
        # Bound nested JSON independently of backend questionnaire validation.
        def check(value, depth=0):
            if depth > 5:
                raise ValueError
            if isinstance(value, dict):
                if len(value) > 100 or any(not isinstance(k, str) or len(k) > 100 for k in value):
                    raise ValueError
                for item in value.values():
                    check(item, depth + 1)
            elif isinstance(value, list):
                if len(value) > 100:
                    raise ValueError
                for item in value:
                    check(item, depth + 1)
            elif isinstance(value, str):
                if len(value) > 1000:
                    raise ValueError
            elif value is not None and not isinstance(value, (int, float, bool)):
                raise ValueError
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError
        check(payload["answers"])
        return payload["answers"]
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _error(422, "问卷格式不正确，请重新选择选项。") from None


def build_personal_router(entry_id, personal, get_ops, account_dependency=None):
    """Return a router to mount in the member zone, never the public zone."""
    router = APIRouter(dependencies=[Depends(account_dependency)] if account_dependency else [])
    operation = f"plugins.{entry_id}."
    hidden = {"x-cli-hidden": True}

    async def profile_token(request):
        if account_dependency is None:
            return _profile_token(request)
        identity = getattr(request.state, "platform_account_identity", None)
        if not identity:
            raise _error(403, "请先登录 MovieClaw。")
        asserted = request.headers.get("X-Platform-Account")
        if asserted and asserted != request.state.platform_account_label:
            raise _error(409, "登录账号已切换，请刷新页面后重试。")
        try:
            return await personal.account_token(identity)
        except ValueError:
            raise _error(503, "账号偏好暂时无法读取，请稍后重试。") from None

    async def private_call(method, token, *args, _conflict_message=None, **kwargs):
        try:
            result = await method(token, *args, **kwargs)
        except ValueError as exc:
            status = getattr(exc, "status", 400)
            status = status if status in {400, 403, 404, 409, 503} else 400
            message = (_conflict_message or "另一设备已更新口味，请关闭测试后重新打开。") if status == 409 else "口味档案暂不存在，请在当前浏览器重新创建。" if status == 404 else "私人档案暂时无法读取，请稍后重试。" if status == 503 else "口味选项暂不可用，请检查问卷后重试。"
            raise _error(status, message) from None
        except Exception:
            raise _error(503, "私人推荐暂时无法读取，请稍后重试。") from None
        return JSONResponse(_private_projection(result, token), headers=PRIVATE_HEADERS)

    @router.post("/personal/profile", operation_id=operation + "personal-profile-create", summary="打开当前账号的口味档案", openapi_extra=hidden)
    async def create_profile(request: Request):
        _browser_request(request)
        if account_dependency is not None:
            token = await profile_token(request)
            return JSONResponse({"profile": await personal.get_profile(token), "sync": {"mode": "account"}}, headers=PRIVATE_HEADERS)
        try:
            result = await personal.create_profile()
            token, profile = result["profile_token"], result["profile"]
            if not isinstance(token, str) or not PROFILE_TOKEN.fullmatch(token):
                raise ValueError
        except Exception:
            raise _error(503, "暂时无法创建口味档案，请稍后重试。") from None
        return JSONResponse({"profile_token": token, "profile": _private_projection(profile, token)}, headers=PRIVATE_HEADERS)

    @router.get("/personal/profile", operation_id=operation + "personal-profile", summary="读取当前账号的口味档案", openapi_extra=hidden)
    async def profile(request: Request):
        _browser_request(request)
        return await private_call(personal.get_profile, await profile_token(request))

    @router.get("/personal/questionnaire", operation_id=operation + "personal-questionnaire", summary="读取口味问卷选项", openapi_extra=hidden)
    async def questionnaire():
        try:
            result = await personal.questionnaire()
        except Exception:
            raise _error(503, "口味问卷暂不可用，请稍后重试。") from None
        return JSONResponse(_private_projection(result), headers=PRIVATE_HEADERS)

    @router.get("/personal/shelf", operation_id=operation + "personal-shelf", summary="读取本人收藏、观看与隐藏片单", openapi_extra=hidden)
    async def shelf(request: Request):
        _browser_request(request)
        return await private_call(personal.shelf, await profile_token(request))

    @router.post("/personal/feedback", operation_id=operation + "personal-feedback", summary="更新本人收藏、观看、喜欢或隐藏反馈", openapi_extra=hidden)
    async def feedback(request: Request):
        _browser_request(request)
        token = await profile_token(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise _error(415, "请使用页面中的片单操作。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16 * 1024:
                raise _error(413, "作品资料过大。")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"action", "item"}:
                raise ValueError
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _error(422, "片单操作格式不正确。") from None
        return await private_call(personal.feedback, token, payload["action"], payload["item"], undoable=True)

    async def learning_body(request, key, maximum):
        _browser_request(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _error(415, "请从海报墙操作。")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > maximum:
                raise _error(413, "偏好记录过大。")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {key}:
                raise ValueError
            return value[key]
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise _error(422, "偏好记录格式无效。") from None

    @router.post("/personal/account", operation_id=operation + "personal-account", summary="打开当前登录账号的同步偏好并合并旧浏览器档案", openapi_extra=hidden)
    async def account(request: Request):
        if account_dependency is None:
            raise _error(503, "当前运行模式未提供账号身份。")
        await profile_token(request)
        legacy = await learning_body(request, "legacy_token", 512)
        try:
            result = await personal.open_account(request.state.platform_account_identity, legacy)
        except ValueError:
            raise _error(400, "旧档案暂时无法导入；账号偏好保持不变。") from None
        return JSONResponse(_private_projection(result), headers=PRIVATE_HEADERS)

    @router.get("/personal/filters", operation_id=operation + "personal-filters", summary="读取当前账号的海报墙筛选", openapi_extra=hidden)
    async def wall_filters(request: Request):
        _browser_request(request)
        return await private_call(personal.get_wall_filters, await profile_token(request))

    @router.post("/personal/filters", operation_id=operation + "personal-filters-update", summary="保存当前账号的多选筛选", openapi_extra=hidden)
    async def set_wall_filters(request: Request):
        token = await profile_token(request)
        selection = await learning_body(request, "selection", 4096)
        return await private_call(personal.set_wall_filters, token, selection, _conflict_message="另一设备已更新筛选，请载入最新条件后再应用。")

    @router.get("/personal/presets", operation_id=operation + "personal-presets", summary="读取账号收藏的筛选方案", openapi_extra=hidden)
    async def presets(request: Request):
        _browser_request(request)
        return await private_call(personal.get_presets, await profile_token(request))

    @router.post("/personal/presets", operation_id=operation + "personal-presets-update", summary="收藏、移除或恢复筛选方案", openapi_extra=hidden)
    async def change_preset(request: Request):
        token = await profile_token(request)
        change = await learning_body(request, "change", 8192)
        return await private_call(personal.change_preset, token, change, _conflict_message="另一设备已修改方案，请重新载入。")

    @router.post("/personal/presets/apply", operation_id=operation + "personal-preset-apply", summary="一键应用账号收藏方案并核对版本", openapi_extra=hidden)
    async def apply_preset(request: Request):
        token = await profile_token(request)
        choice = await learning_body(request, "choice", 1024)
        return await private_call(personal.apply_preset, token, choice, _conflict_message="方案或筛选已在其他页面变化，请刷新快捷标签后重新选择。")

    @router.get("/personal/actions", operation_id=operation + "personal-actions", summary="读取账号可撤销的近期操作", openapi_extra=hidden)
    async def recent_actions(request: Request):
        _browser_request(request)
        return await private_call(personal.recent_actions, await profile_token(request))

    @router.post("/personal/actions/undo", operation_id=operation + "personal-action-undo", summary="安全撤销一次影片标记", openapi_extra=hidden)
    async def undo_feedback(request: Request):
        token = await profile_token(request)
        identity = await learning_body(request, "id", 512)
        return await private_call(personal.undo_feedback, token, identity, _conflict_message="撤销已过期，或这部影片的标记后来已被修改，请刷新片单。")

    @router.get("/personal/history", operation_id=operation + "personal-history", summary="读取本人详情浏览和已展示海报墙历史", openapi_extra=hidden)
    async def browse_history(request: Request):
        _browser_request(request)
        return await private_call(personal.get_history, await profile_token(request))

    @router.post("/personal/history", operation_id=operation + "personal-history-add", summary="记录本人实际打开的影片详情", openapi_extra=hidden)
    async def record_view(request: Request):
        token = await profile_token(request)
        item = await learning_body(request, "item", 16384)
        return await private_call(personal.record_view, token, item)

    def filters_revision(request):
        value = request.headers.get("X-Platform-Filter-Revision")
        if value is not None and not re.fullmatch(r"[0-9a-f]{24}|", value):
            raise _error(400, "筛选版本无效。")
        return value

    @router.post("/personal/subscriptions", operation_id=operation + "personal-subscriptions", summary="同步当前浏览器账号可见订阅用于私人口味", openapi_extra=hidden)
    async def sync_subscriptions(request: Request):
        token = await profile_token(request)
        refs = await learning_body(request, "refs", 64 * 1024)
        return await private_call(personal.sync_subscriptions, token, refs)

    @router.post("/personal/learning", operation_id=operation + "personal-learning", summary="开启或关闭行为偏好学习", openapi_extra=hidden)
    async def learning(request: Request):
        token = await profile_token(request)
        enabled = await learning_body(request, "enabled", 1024)
        return await private_call(personal.learning, token, enabled)

    @router.post("/personal/answers", operation_id=operation + "personal-answers", summary="保存当前浏览器的口味问卷", openapi_extra=hidden)
    async def answers(request: Request):
        _browser_request(request)
        token = await profile_token(request)
        expected = request.headers.get("X-Platform-Test-Revision")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{24}|", expected):
            raise _error(400, "口味版本无效，请重新打开测试。")
        options = {"expected_test": expected} if expected is not None else {}
        return await private_call(personal.submit, token, await _answers(request), **options)

    @router.get("/personal/recommendations/resume", operation_id=operation + "personal-resume", summary="恢复当前账号上次展示的海报墙，不记新展示", openapi_extra=hidden)
    @router.get("/personal/recommendations/prepare", operation_id=operation + "personal-prepare", summary="准备下一面，不计入展示历史", openapi_extra=hidden)
    @router.get("/personal/recommendations", operation_id=operation + "personal-recommendations", summary="读取私人推荐海报墙", openapi_extra=hidden)
    async def recommendations(request: Request):
        _browser_request(request)
        nonce = request.query_params.get("nonce", "0")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce):
            raise _error(400, "换一批标识无效，请从页面重新操作。")
        raw_limit = request.query_params.get("limit", "48")
        if not re.fullmatch(r"[1-9]\d{0,2}", raw_limit) or not 3 <= int(raw_limit) <= 96:
            raise _error(400, "海报数量无效，请从页面重新操作。")
        media_type, era = request.query_params.get("media_type", "all"), request.query_params.get("era", "all")
        country = request.query_params.get("country", "all")
        genres, excluded, exploration = (request.query_params.get(key, default) for key, default in (("genres", "all"), ("exclude_genres", "all"), ("exploration", "balanced")))
        if not personal.valid_wall_filters(media_type, era, country, genres, excluded, exploration):
            raise _error(400, "筛选条件无效，请从页面重新选择。")
        filters = {"media_type": media_type, "era": era} if media_type != "all" or era != "all" else {}
        if country != "all": filters["country"] = country
        for key, value, default in (("genres", genres, "all"), ("exclude_genres", excluded, "all"), ("exploration", exploration, "balanced")):
            if value != default: filters[key] = value
        preparing = request.url.path.endswith("/prepare")
        method = personal.resume_wall if request.url.path.endswith("/resume") else personal.prepare_wall if preparing else personal.recommend
        revision = filters_revision(request)
        if revision is not None: filters["expected_filters"] = revision
        if preparing:
            after = request.query_params.get("after", "")
            if after and not re.fullmatch(r"[0-9a-f]{48}", after):
                raise _error(400, "预备队列无效。")
            filters["after"] = after
            filters["queue_id"] = request.headers.get("X-Platform-Queue", "")
        return await private_call(method, await profile_token(request), nonce=nonce, limit=int(raw_limit), _conflict_message="另一设备已更新筛选或偏好，请重试。", **filters)

    @router.post("/personal/recommendations/commit", operation_id=operation + "personal-commit", summary="核对当前账号偏好后展示预备片单", openapi_extra=hidden)
    async def commit_wall(request: Request):
        token = await profile_token(request)
        ticket = await learning_body(request, "ticket", 1024)
        return await private_call(personal.commit_wall, token, ticket, queue_id=request.headers.get("X-Platform-Queue", ""), expected_filters=filters_revision(request), _conflict_message="另一设备已更新筛选或偏好，请重试。")

    @router.post("/personal/recommendations/rotate-preview", operation_id=operation + "personal-rotate-preview", summary="预检屏保轮替，不计展示", openapi_extra=hidden)
    @router.post("/personal/recommendations/rotate", operation_id=operation + "personal-rotate", summary="确认已显示的屏保轮替", openapi_extra=hidden)
    async def rotate_wall(request: Request):
        token = await profile_token(request)
        rotation = await learning_body(request, "rotation", 16384)
        return await private_call(personal.rotate_wall, token, rotation, preview=request.url.path.endswith("/rotate-preview"), queue_id=request.headers.get("X-Platform-Queue", ""), expected_filters=filters_revision(request), _conflict_message="另一设备已更新筛选或偏好，请重试。")

    @router.get("/public-titles/{title_ref}", operation_id=operation + "public-title", summary="读取不含媒体库库存的公开作品资料")
    async def title_details(title_ref: str):
        if not TITLE_REF.fullmatch(title_ref):
            raise _error(400, "作品引用格式不正确。")
        discovery = getattr(personal, "discovery", None)
        if discovery is not None and title_ref.startswith("tmdb:") and getattr(discovery, "_token", ""):
            try:
                direct = await _tmdb_details(discovery, title_ref)
                if direct is not None:
                    return JSONResponse(direct, headers=PRIVATE_HEADERS)
            except Exception:
                pass  # The existing authorized host operation remains fallback.
        try:
            ops = await get_ops()
            payload = await ops.call("discover.get-title-details", {"title_ref": title_ref}, timeout=20)
            result = public_title_details(payload, title_ref)
        except Exception:
            raise _error(503, "作品公开资料暂不可用，可前往 MovieClaw 查看详情。") from None
        return JSONResponse(result, headers=PRIVATE_HEADERS)

    @router.get("/native-title-status/{title_ref}", operation_id=operation + "native-title-status", summary="检查 MovieClaw 原生影片资料是否可用")
    async def native_title_status(title_ref: str):
        if not TITLE_REF.fullmatch(title_ref):
            return JSONResponse({"available": False, "message": "作品引用格式不正确。"}, status_code=400, headers=PRIVATE_HEADERS)
        available = False
        try:
            # This checks the existing host operation only: plugin-owned TMDB
            # metadata cannot make the host's subscription service available.
            async with asyncio.timeout(NATIVE_STATUS_TIMEOUT):
                ops = await get_ops()
                payload = await ops.call("discover.get-title-details", {"title_ref": title_ref}, timeout=NATIVE_STATUS_TIMEOUT)
                available = _native_title_matches(payload, title_ref)
        except Exception:
            pass  # No upstream diagnostics, network configuration, or inventory.
        return JSONResponse({"available": available, "message": "MovieClaw 影片资料可用，可继续前往原生页面。" if available else NATIVE_UNAVAILABLE}, headers=PRIVATE_HEADERS)

    return router
