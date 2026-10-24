"""H&R 的明确站点证据与一次性详情核对；缺失标记始终保持未知。"""
import re
import secrets
import time
from contextvars import ContextVar
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from parsel import Selector

CAPTURE = ContextVar("pt_hr_capture", default=None)
PREFIX = "__pt_island_hr__:"
TICKETS, REPORTS = {}, {}
MARKER = re.compile(r"H\s*&\s*R|Hit[\s-]*(?:and|&)[\s-]*Run", re.I)
NO = re.compile(r"无(?:需)?考核|不考核|无需|免除|豁免|免\s*H\s*&\s*R|no\s*(?:H\s*&\s*R|hit.and.run)|^(?:否|无|no|false)$", re.I)
YES = re.compile(r"需要考核|有考核|^(?:是|有|yes|true)$", re.I)


def torrent_id(url):
    p = urlparse(str(url or ""))
    args = parse_qs(p.query)
    tid = (args.get("torrentid") or args.get("id") or [None])[0]
    if tid is None:
        m = re.search(r"/(?:details?|torrents?|t)/(\d+)(?:/|$)", p.path)
        tid = m[1] if m else None
    return tid if tid and re.fullmatch(r"\d{1,20}", str(tid)) else None


def target_url(site, value):
    try:
        p = urlparse(str(value or ""))
        roots = [urlparse(str(site.get(k) or "")) for k in ("website_url", "base_url")]
        if p.scheme != "https" or not p.hostname or p.username or p.password: return None
        if site.get("site_id")=="mteam" and p.hostname=="tp.m-team.cc" and (p.port or 443)==443:
            current=next((a for a in roots if a.scheme=="https" and a.hostname=="kp.m-team.cc"),None)
            if current:p=p._replace(netloc=current.netloc)
        if not any(p.hostname.lower().removeprefix("www.") == (a.hostname or "").lower().removeprefix("www.") and (p.port or 443) == (a.port or 443) for a in roots): return None
        if not (p.path.endswith(("details.php", "torrents.php")) or re.fullmatch(r"/(?:details?|torrents?|t)/\d+/?", p.path)): return None
        if not torrent_id(value): return None
        args = parse_qs(p.query)
        query = urlencode({k:args[k][0] for k in ("id", "torrentid") if k in args and re.fullmatch(r"\d{1,20}",args[k][0])})
        return urlunparse((p.scheme,p.netloc,p.path,"",query,""))
    except ValueError: return None


def issue(site_id, url):
    for key, ticket in list(TICKETS.items()):
        if ticket["expires"] < time.monotonic():
            TICKETS.pop(key,None);REPORTS.pop(key,None)
    token=secrets.token_hex(24)
    TICKETS[token]={"site_id":site_id,"url":url,"expires":time.monotonic()+90}
    return token,PREFIX+token


def take(keyword, site_id):
    token=keyword.removeprefix(PREFIX)
    ticket=TICKETS.pop(token,None)
    if not ticket or ticket["site_id"]!=site_id or ticket["expires"]<time.monotonic():
        from movieclaw_tracker.exceptions import TrackerParseError
        raise TrackerParseError("H&R 核对请求无效或已过期")
    return token,ticket


def flag(value):
    if type(value) is bool:return value
    if isinstance(value,str) and value.lower() in {"true","false"}:return value.lower()=="true"
    return None


def html_flag(node):
    value=" ".join(node.xpath("string(.)").get("").split())
    if NO.search(value): return False
    if YES.search(value): return True
    for marker in node.css("img[alt], img[title], span[title], div[title]"):
        label=(marker.attrib.get("alt","")+" "+marker.attrib.get("title","")).strip()
        if MARKER.search(label):return False if NO.search(label) else True
    return None


class Capture:
    def __init__(self, selectors=None, expected=None):
        self.selectors,self.expected,self.values=selectors,expected,{}

    def record(self,tid,value,source):
        if tid and type(value) is bool:
            old=self.values.get(str(tid))
            if old and old[0] is not value:self.values[str(tid)]=(None,"conflicting")
            elif not old:self.values[str(tid)]=(value,source)

    def response(self,response):
        if response.status_code!=200 or len(response.content)>2*1024*1024:return
        p=urlparse(str(response.url))
        body=response.text.lstrip()
        if body.startswith("{"):
            if not p.path.endswith(("/api/torrent/search","/api/torrent/advancedSearch","/api/torrent/detail","/api/torrent/info")):return
            raw=response.json()
            if not isinstance(raw,dict):return
            success=raw.get("status")==200 or str(raw.get("code"))=="0"
            if not success:return
            data=raw.get("data")
            if not isinstance(data,dict):return
            rows=data.get("data") or data.get("torrents")
            if not isinstance(rows,list):rows=[data.get("torrent") or data]
            for row in rows[:200]:
                if not isinstance(row,dict):continue
                tid=str(row.get("id") or "")
                if not re.fullmatch(r"\d{1,20}",tid):continue
                for obj in (row,row.get("status")):
                    if not isinstance(obj,dict):continue
                    for key in ("hit_and_run","hitAndRun","isHR","isHr","hr"):
                        self.record(tid,flag(obj.get(key)),"site-api")
        elif body.startswith("<"):
            doc=Selector(text=body)
            if self.expected and (p.path.endswith(("details.php","torrents.php")) or re.search(r"/(?:detail|torrent)/",p.path)):
                for label in doc.css("td.rowhead, th, dt"):
                    name=" ".join(label.xpath("string(.)").get("").split()).strip(":： ")
                    if not MARKER.fullmatch(name):continue
                    siblings=label.xpath("following-sibling::*[1]")
                    if siblings:self.record(self.expected,html_flag(siblings[0]),"detail-label")
            css=getattr(self.selectors,"torrent_row_css","") if self.selectors else ""
            if css:
                for row in doc.css(css)[:200]:
                    css_url=getattr(self.selectors,"torrent_detail_url_css","")
                    href=row.css(css_url).get() if css_url else None
                    tid=torrent_id(href)
                    # Only explicit per-torrent icons or yes/no labels, never a
                    # page-wide user H&R ledger or the absence of an icon.
                    for marker in row.css("img[alt], img[title], span[title], div[title]"):
                        label=(marker.attrib.get("alt","")+" "+marker.attrib.get("title","")).strip()
                        if MARKER.search(label):self.record(tid,False if NO.search(label) else True,"list-marker")
