"""只使用有效注册日期计算站点年龄，不从流量或服务器接入时间猜测 P 龄。"""
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse, urljoin
from parsel import Selector


def registration_date(value, now=None):
    if not isinstance(value, (str, datetime)): return None
    now = now or datetime.now(timezone.utc)
    try:
        if isinstance(value, str):
            match = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:[T\s]|$)", value.strip())
            if not match: return None
            value = datetime(*map(int, match.groups()), tzinfo=timezone.utc)
        if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)
        if not 2000 <= value.year or value > now: return None
        return value
    except (TypeError, ValueError, OverflowError): return None


def age_view(value, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = registration_date(value, now)
    if stamp is None: return {"age_days": None, "join_date": None, "age_source": None}
    return {"age_days": max(0, (now.date() - stamp.date()).days), "join_date": stamp.date().isoformat(), "age_source": "站点注册记录"}


LABEL = r"(?:注册|註冊|加入)(?:日期|时间|時間)|Join(?:ed|\s+date)?|Registration\s+date"


def own_heading(doc, username):
    """Match a whole account name, never a substring of a different username."""
    expected = " ".join(str(username or "").split()).casefold()
    return bool(expected and any(" ".join(n.xpath("string(.)").get("").split()).casefold() == expected for n in doc.css("h1,h2")))


def own_profile_id(pages, profile, base_url):
    """Resolve only an exact current-account link, with no arbitrary URL requests."""
    expected = " ".join(str(profile.username or "").split()).casefold()
    base = urlparse(base_url)
    found = set()
    for _, html in pages:
        if not isinstance(html, str): continue
        for link in Selector(text=html).css("a[href*='userdetails.php']"):
            if " ".join(link.xpath("string(.)").get("").split()).casefold() != expected or not expected: continue
            url = urlparse(urljoin(base_url + "/", link.attrib.get("href", "")))
            ids = parse_qs(url.query).get("id", [])
            if url.scheme == base.scheme and url.netloc == base.netloc and url.path == "/userdetails.php" and len(ids) == 1 and re.fullmatch(r"[0-9]{1,20}", ids[0]):
                found.add(ids[0])
    uid = str(profile.user_id or "")
    if len(found) == 1 and (not uid or uid in found): return next(iter(found))
    # SDK adapters that already resolve the authenticated UID need no second
    # account-strip match, but conflicting links are never adopted.
    return uid if not found and re.fullmatch(r"[0-9]{1,20}", uid) else None


def already_requested(pages, uid):
    return any(urlparse(path).path.rsplit("/", 1)[-1] == "userdetails.php" and parse_qs(urlparse(path).query).get("id") == [uid]
               for path, _ in pages)


def node_date(node):
    # Relative text ("3 years ago") is never estimated. Use the exact date
    # carried by the same value's datetime/title attribute when available.
    for candidate in [node, *node.css("time[datetime], [title]")]:
        for attr in ("datetime", "title"):
            value = registration_date(candidate.attrib.get(attr))
            if value: return value
    return registration_date(node.xpath("string(.)").get("").strip())


def extract_registration(pages, profile):
    """仅在 UID 或用户名匹配的个人页识别明确注册日期标签。"""
    for path, html in reversed(pages):
        if isinstance(html, dict):
            account = hashlib.sha256(str(profile.username or "").encode()).hexdigest()
            if html.get("_account") != account: continue
            uid = html.get("_uid")
            if uid and str(profile.user_id or "") != uid: continue
            value = registration_date(html.get("_joined_at"))
            if value: return value
            continue
        if not isinstance(html, str): continue
        url = urlparse(path)
        if url.path.rsplit("/", 1)[-1] not in {"userdetails.php", "user.php"}: continue
        doc = Selector(text=html); uid = str(profile.user_id or ""); username = str(profile.username or "")
        ids = parse_qs(url.query).get("id")
        if ids and ids != [uid]: continue
        heading = own_heading(doc, username)
        if not (ids == [uid] and uid or heading): continue
        for cell in doc.css("td.rowhead, th.rowhead, dt, tr > th:first-child, tr > td:first-child"):
            label = " ".join(cell.xpath("string(.)").get("").split()).strip(":： ")
            if not re.fullmatch(LABEL, label, re.I): continue
            following = cell.xpath("following-sibling::*[1]")
            if following:
                value = node_date(following[0])
                if value: return value
        # Modern Nexus themes use a compact span/div card instead of a table.
        # Anchor the label at the start and stay inside its smallest value row.
        for row in doc.css("span,li,p,div"):
            text = " ".join(row.xpath("string(.)").get("").split())
            if len(text) > 200: continue
            match = re.match(r"^(?:" + LABEL + r")[\s:：]+(.*)$", text, re.I)
            if match:
                value = registration_date(match[1])
                if value: return value
                # A separate label followed by a relative-date span.
                children = row.xpath("./*")
                if children and re.fullmatch(r"(?:" + LABEL + r")[\s:：]*", children[0].xpath("string(.)").get("").strip(), re.I):
                    for child in children[1:2]:
                        value = node_date(child)
                        if value: return value
    return None
