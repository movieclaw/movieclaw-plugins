"""从账号资料和登录导航中补读统计；不保存 HTML、账号名或认证信息。"""
import asyncio
import hashlib
import json
import math
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from parsel import Selector

PROFILE_PAGES = ContextVar("pt_profile_pages", default=None)
AUTH_CHECK = ContextVar("pt_auth_check", default=False)
FIELDS = ("uploaded_bytes", "downloaded_bytes", "ratio", "bonus", "seeding_count", "leeching_count")
LABELS = {
    "uploaded_bytes": r"上传量|上传总量|总上传量|累计上传|上传|Uploaded|Upload(?:ed)?\s*(?:Amount|Total)?",
    "downloaded_bytes": r"下载量|下载总量|总下载量|累计下载|下载|Downloaded|Download(?:ed)?\s*(?:Amount|Total)?",
    "ratio": r"分享率|分享比例|Ratio",
    "bonus": r"魔力值|魔力|积分|猫粮|电力值|Bonus(?:\s*Points)?|Seedbonus",
    "seeding_count": r"当前做种|正在做种|做种数|做种中|做种|Seeding|Seeders",
    "leeching_count": r"当前下载|正在下载|下载数|下载中|Leeching|Leechers",
}
SELECTORS = {"uploaded_bytes": "profile_uploaded_css", "downloaded_bytes": "profile_downloaded_css",
             "ratio": "profile_ratio_css", "bonus": "profile_bonus_css",
             "seeding_count": "profile_seeding_css", "leeching_count": "profile_leeching_css"}
NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
SIZE = re.compile(r"^\s*(" + NUM + r")\s*([KMGTPE]i?B|B)\b", re.I)
VALUE = re.compile(r"^\s*(" + NUM + r")(?=\s|$|[()（）,:：;；/])")


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def api_registration(data, value):
    from .anniversary import registration_date
    stamp = registration_date(value)
    if stamp is None or not isinstance(data.get("username"), str) or not data["username"]: return {}
    result = {"_joined_at": stamp.date().isoformat(), "_account": hashlib.sha256(data["username"].encode()).hexdigest()}
    if re.fullmatch(r"[0-9]{1,20}", str(data.get("id", ""))): result["_uid"] = str(data["id"])
    return result


def api_fields(path, payload):
    """只识别宿主已使用的成功账号接口，保留 API 明确返回的零。"""
    if not isinstance(payload, dict): return {}
    if path.endswith("/ajax.php") and payload.get("status")=="success":
        response=payload.get("response")
        if not isinstance(response,dict) or not isinstance(response.get("username"),str):return {}
        community=response.get("community") or {}
        if not isinstance(community,dict):return {}
        stats=response.get("stats")
        result=api_registration(response, stats.get("joinedDate") if isinstance(stats,dict) else None)
        result["_account"]=hashlib.sha256(response["username"].encode()).hexdigest()
        for field,key in (("seeding_count","seeding"),("leeching_count","leeching")):
            value=community.get(key)
            if numeric(value) and float(value).is_integer():result[field]=int(value)
        return result
    data = payload.get("data")
    if not isinstance(data, dict): return {}
    result = {}
    if path.endswith("/api/user/getInfo") and payload.get("status") == 200:
        # Zhuque returns regTime in Unix seconds (not the similarly named
        # seedTime/leechTime durations). Never infer dates from those counters.
        reg = data.get("regTime")
        if type(reg) in (int, float) or isinstance(reg, str) and re.fullmatch(r"[0-9]{10}", reg):
            try:
                if float(reg) >= 946684800:
                    result = api_registration(data, datetime.fromtimestamp(float(reg), timezone.utc))
            except (ValueError, TypeError, OverflowError, OSError): pass
        mapping = dict(uploaded_bytes="upload", downloaded_bytes="download", bonus="bonus", seeding_count="seeding", leeching_count="leeching")
    elif str(payload.get("code")) == "0" and str(payload.get("message", "")).upper() in {"SUCCESS", "OK", "0", ""}:
        if path.endswith("/api/tracker/myPeerStatus"):
            mapping = dict(seeding_count="seeder", leeching_count="leecher")
        elif path.endswith("/api/member/profile"):
            result = api_registration(data, data.get("createdDate"))
            data = data.get("memberCount") or {}
            mapping = dict(uploaded_bytes="uploaded", downloaded_bytes="downloaded", bonus="bonus", ratio="shareRate")
        else: return {}
    else: return {}
    if not isinstance(data, dict): return {}
    for field, key in mapping.items():
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(NUM, value):
            value = float(value.replace(",", ""))
        if numeric(value):
            if field.endswith(("_bytes", "_count")):
                if not float(value).is_integer(): continue
                value = int(value)
            result[field] = value
    return result


def parse_value(field, raw):
    raw = " ".join(str(raw).replace("\xa0", " ").split()).lstrip(":： ")
    if field.endswith("_bytes"):
        m = SIZE.match(raw)
        if not m: return None
        value = float(m[1].replace(",", "")) * 1024 ** "BKMGTPE".index(m[2][0].upper())
        return int(value) if math.isfinite(value) else None
    m = VALUE.match(raw)
    if not m: return None
    value = float(m[1].replace(",", ""))
    if not math.isfinite(value): return None
    if field.endswith("_count"):
        return int(value) if value.is_integer() else None
    return value


def node_text(node):
    value = node.xpath("string(.)").get()
    return " ".join(((node.get() or "") if value is None else value).split())


def extract(pages, selectors=None, user_id=None, username=None):
    """先读取显式选择器，其次精确标签；不从整个页面猜测数字。"""
    fields, sources = {}, {}

    def put(field, raw, source):
        if field in fields: return
        value = parse_value(field, raw)
        if value is not None:
            fields[field], sources[field] = value, source

    for path, html in reversed(pages):
        if isinstance(html, dict):
            if html.get("_account") and html["_account"]!=hashlib.sha256(str(username or "").encode()).hexdigest():continue
            for field in FIELDS:
                if field not in fields and numeric(html.get(field)):
                    fields[field], sources[field] = html[field], "site-api"
            continue
        doc = Selector(text=html)
        parsed=urlparse(path)
        personal_path=parsed.path.rsplit("/", 1)[-1] in {"userdetails.php", "user.php"}
        own_query=bool(user_id and parse_qs(parsed.query).get("id")==[str(user_id)])
        from .anniversary import own_heading as matches_heading
        own_heading=matches_heading(doc, username)
        if user_id and parse_qs(parsed.query).get("id") and not own_query:own_heading=False
        profile_page=personal_path and (own_query or own_heading or user_id is None and username is None)
        if selectors:
            for field, attr in SELECTORS.items():
                css = getattr(selectors, attr, "")
                if not css: continue
                if not profile_page and re.search(r"rowhead|td:nth",css):continue
                # Direct ::text misses values nested in spans, links, or bold tags.
                try:
                    if css.endswith("::next_sibling_text"):
                        nodes = doc.css(css.removesuffix("::next_sibling_text"))
                        if nodes:
                            following = nodes[0].xpath("following-sibling::node()[self::* or normalize-space()][1]")
                            if following: put(field, node_text(following[0]), "selector")
                    else:
                        nodes = doc.css(re.sub(r"::text\s*$", "", css))
                        if nodes: put(field, node_text(nodes[0]), "selector")
                except Exception:
                    continue
        # Only account rows on the authenticated personal profile, never torrent tables.
        if profile_page:
            for cell in doc.css("td.rowhead, th.rowhead"):
                label = node_text(cell).strip(":： ")
                value = cell.xpath("following-sibling::*[1]")
                if not value: continue
                for field, pattern in LABELS.items():
                    if re.fullmatch(pattern, label, re.I): put(field, node_text(value[0]), "profile-label")
        # Logged-in header statistics; excluded from arbitrary body content.
        containers = list(doc.css("#info_block, #userinfo, #user-info, #userbar, #header, .userinfo, .Header-userStats, [id^='header-'], .site-userbar, [class*='site-userbar__compact']"))
        # Several NexusPHP themes put the account strip outside those containers.
        # Bind fallback ancestors to the actual profile UID, not arbitrary site totals.
        if user_id:
            for link in doc.css("a[href*='userdetails.php?id=']"):
                if parse_qs(urlparse(link.attrib.get("href", "")).query).get("id") != [str(user_id)]: continue
                for node in link.xpath("ancestor::td | ancestor::span | ancestor::nav | ancestor::header | ancestor::div"):
                    value = node_text(node)
                    if len(value) <= 2000 and re.search(LABELS["uploaded_bytes"], value, re.I) and re.search(LABELS["downloaded_bytes"], value, re.I):
                        containers.append(node)
        candidates = []
        for container in containers:
            candidates.append(container)
            candidates.extend(container.css("li, td, span, div, a"))
            for arrow in container.css("img.arrowup, img.arrowdown"):
                following=arrow.xpath("following-sibling::node()[self::* or normalize-space()][1]")
                if following and re.fullmatch(r"[\d,]+",node_text(following[0])):
                    field="seeding_count" if "arrowup" in arrow.attrib.get("class", "").split() else "leeching_count"
                    put(field,node_text(following[0]),"account-header")
            for icon in container.css("img[alt], [title]"):
                label = (icon.attrib.get("title") or icon.attrib.get("alt") or "").strip(":： ")
                following = icon.xpath("following-sibling::node()[self::* or normalize-space()][1]")
                for field, pattern in LABELS.items():
                    if re.fullmatch(r"(?:Torrents\s+)?(?:" + pattern + r")", label, re.I):
                        put(field, node_text(icon), "account-header")
                        if following: put(field, node_text(following[0]), "account-header")
        if profile_page:
            candidates.extend(doc.css("td.rowhead + td"))
            if parsed.path.endswith("/user.php"):
                candidates.extend(doc.css("#content .SidebarList-item"))
        for node in candidates[:5000]:
            value = node_text(node)
            if not value or len(value) > 1200: continue
            for field, pattern in LABELS.items():
                # A colon or a separating space is required after an English label.
                match = re.search(r"(?:^|[\s|,，;；])(?:" + pattern + r")(?:\s*\[(?:使用|兑换|use|spend)(?:\s*\|\s*站免池)?\])?[\s:：]+(" + NUM + r"(?:\s*[KMGTPE]i?B\b|\s*B\b)?)(?=\s|$|[()（）,:：;；/])", value, re.I)
                if match: put(field, match[1], "account-header" if not profile_page else "profile-label")
        # Gazelle-style numeric values have field-specific element IDs.
        for field, ids in {
            "uploaded_bytes": ("header-uploaded-value", "header-uploaded", "stats_uploaded"),
            "downloaded_bytes": ("header-downloaded-value", "header-downloaded", "stats_downloaded"),
            "ratio": ("header-ratio-value", "header-ratio", "stats_ratio"),
            "bonus": ("header-bonus-value", "header-bonus", "stats_bonus"),
            "seeding_count": ("header-seeding-value", "header-seeding", "stats_seeding"),
            "leeching_count": ("header-leeching-value", "header-leeching", "stats_leeching"),
        }.items():
            for ident in ids:
                nodes = doc.css("#" + ident)
                if nodes: put(field, node_text(nodes[0]), "account-header")
        for node in doc.css(".HeaderStat-item.is-bp .HeaderStat-link"):
            put("bonus", node_text(node), "account-header")
        # Audiences separates its spendable currency from seeding points.
        for node in doc.css(".site-userbar__compact-metric--bonus"):
            value=node.attrib.get("title","")
            match=re.fullmatch(r"爆米花\s+("+NUM+r")",value)
            if match:put("bonus",match[1],"account-header")
        for field,suffix,arrow in (("seeding_count","seeding","↑"),("leeching_count","leeching","↓")):
            for node in doc.css(".site-userbar__compact-metric-inline-link--"+suffix):
                value=node_text(node).removeprefix(arrow).strip()
                if re.fullmatch(r"[\d,]+",value):put(field,value,"account-header")
    return fields, sources


def enrich(profile, pages, selectors=None):
    from .anniversary import extract_registration, registration_date
    fields, sources = extract(pages, selectors, profile.user_id, profile.username)
    updates = {}
    if registration_date(profile.join_date) is None:
        joined = extract_registration(pages, profile)
        if joined: updates["join_date"] = joined
    for field in FIELDS:
        original = getattr(profile, field, None)
        # Keep existing dedicated API/site parsers when they already have a value.
        if numeric(original) and (original > 0 or field in {"ratio", "bonus"}):
            fields[field], sources[field] = original, "native"
        elif field in fields:
            updates[field] = fields[field]
    if "ratio" not in fields and fields.get("downloaded_bytes", 0) > 0 and "uploaded_bytes" in fields:
        value = fields["uploaded_bytes"] / fields["downloaded_bytes"]
        if math.isfinite(value):
            fields["ratio"], sources["ratio"] = value, "calculated"
            updates["ratio"] = value
    return profile.model_copy(update=updates), fields, sources


class ProfileEvidence:
    def __init__(self, data):
        self.data, self.values, self.lock = data, {}, asyncio.Lock()

    async def start(self):
        self.values = await self.data.get("overview-profile-v1") or {}

    async def observe(self, sid, profile, fields, sources):
        record = {"account": hashlib.sha256(profile.username.encode()).hexdigest(),
                  "observed_at": datetime.now(timezone.utc).isoformat(),
                  "fields": {k: fields[k] for k in FIELDS if k in fields and numeric(fields[k])},
                  "sources": {k: sources[k] for k in FIELDS if k in fields and k in sources}}
        async with self.lock:
            from .anniversary import registration_date
            joined = registration_date(profile.join_date)
            previous = self.values.get(sid, {})
            account_id = hashlib.sha256(str(profile.user_id).encode()).hexdigest() if profile.user_id else None
            # Registration is immutable for this account. An optional page
            # failure must not erase a previously verified date.
            if joined:
                record["join_date"] = joined.date().isoformat()
                record["join_date_observed_at"] = record["observed_at"]
                record["join_date_account_id"] = account_id
            elif previous.get("account") == record["account"] and previous.get("join_date_account_id") in (None, account_id) and registration_date(previous.get("join_date")):
                record["join_date"] = previous["join_date"]
                record["join_date_observed_at"] = previous.get("join_date_observed_at", previous.get("observed_at"))
                record["join_date_account_id"] = account_id
            self.values.pop(sid, None)
            self.values[sid] = record
            while len(self.values) > 100 or len(json.dumps(self.values).encode()) > 60000:
                self.values.pop(next(iter(self.values)))
            try: await self.data.set("overview-profile-v1", self.values, secret=True)
            except Exception: pass  # Statistics storage must never break authentication.

    def lookup(self, sid, profile):
        record = self.values.get(sid)
        if not record or record.get("account") != hashlib.sha256(str(profile.get("username", "")).encode()).hexdigest(): return None
        try:
            stamp = datetime.fromisoformat(profile["fetched_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            observed = datetime.fromisoformat(record["observed_at"])
            if abs((stamp - observed).total_seconds()) > 300: return None
        except (KeyError, ValueError, TypeError, AttributeError): return None
        return record
