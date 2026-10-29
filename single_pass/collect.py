"""Fetch only list-level metadata, without visiting entries or images."""
import datetime as dt
import html
import json
import re
import urllib.request
from urllib.parse import urlsplit, urlunsplit

TZ = dt.timezone(dt.timedelta(hours=8))
SOURCES = {"toutiao": "今日头条", "baidu": "百度热搜", "weibo": "微博（TopHub）", "zhihu": "知乎（TopHub）"}
URLS = {"toutiao": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "baidu": "https://top.baidu.com/board?tab=realtime", "tophub": "https://tophub.today/"}


def now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(3_000_001)
        if len(body) > 3_000_000:
            raise ValueError("榜单响应超过3 MB")
        return body.decode("utf-8", "replace")


def canonical(url):
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def item(source, title, url, cover=None, published=None, metrics=None, snapshot=None, source_key=None, source_ordinal=None):
    return {"source_id": source, "source_name": SOURCES[source], "source_type": "current_hotlist", "title": title,
            "url": url, "source_key": str(source_key) if source_key else None,
            "cover_url": cover or None, "published_at": published, "event_time": None,
            "rank": (metrics or {}).get('rank'), "metrics": metrics or {}, "platform_snapshot_time": snapshot, "source_ordinal": source_ordinal}


def parse_toutiao(raw):
    data = json.loads(raw)
    if data.get("status") != "success" or not isinstance(data.get("data"), list):
        raise ValueError("头条响应状态或结构异常")
    return [item("toutiao", x.get("Title"), x.get("Url"), (x.get("Image") or {}).get("url"),
                 metrics={"HotValue": x.get("HotValue"), "source_order": n}, source_key=x.get("ClusterIdStr") or x.get("ClusterId"),source_ordinal=n)
            for n, x in enumerate(data["data"], 1) if x.get("Title") and x.get("Url")]


def parse_baidu(raw):
    m = re.search(r"<!--s-data:(.*?)-->", raw, re.S)
    if not m:
        raise ValueError("百度页面无 s-data 榜单")
    data = json.loads(m.group(1))
    cards = data.get("data", {}).get("cards", [])
    card = next((c for c in cards if c.get("component") == "hotList"), None)
    if card is None or not isinstance(card.get("content"), list):
        raise ValueError("百度页面无 hotList")
    return [item("baidu", x.get("word"), x.get("url") or x.get("rawUrl"), x.get("img"),
                 metrics={"hotScore": x.get("hotScore"), "index": x.get("index"), "isTop": x.get("isTop")},source_ordinal=n)
            for n,x in enumerate(card["content"],1) if x.get("word") and (x.get("url") or x.get("rawUrl"))]


def parse_tophub(raw, source):
    node = {"weibo": "1", "zhihu": "6"}[source]
    m = re.search(r'<div class="cc-cd" id="node-' + node + r'">(.*?)(?=<div class="cc-cd" id="node-|\Z)', raw, re.S)
    if not m:
        raise ValueError("TopHub 缺少对应榜卡")
    block = m.group(1)
    stamp = re.search(r'<div class="i-h[^\"]*">\s*(.*?)\s*</div>', block, re.S)
    snapshot = html.unescape(re.sub(r"<[^>]+>", "", stamp.group(1))).strip() if stamp else None
    rows = []
    for n,match in enumerate(re.finditer(r'<a\s+href="(https?://[^"]+)"[^>]*itemid="([^"]+)"[^>]*>(.*?)</a>', block, re.S),1):
        url, _, body = match.groups()
        title = re.search(r'<span class="t">(.*?)</span>', body, re.S)
        rank = re.search(r'<span class="s[^\"]*">(.*?)</span>', body, re.S)
        value = re.search(r'<span class="e">(.*?)</span>', body, re.S)
        clean = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x.group(1))).strip() if x else None
        if title:
            rows.append(item(source, clean(title), html.unescape(url), metrics={"display": clean(value), "rank": clean(rank)}, snapshot=snapshot,source_ordinal=n))
    if not rows:
        raise ValueError("TopHub 榜卡未解析到条目")
    return rows


PARSERS = {"toutiao": parse_toutiao, "baidu": parse_baidu,
           "weibo": lambda s: parse_tophub(s, "weibo"), "zhihu": lambda s: parse_tophub(s, "zhihu")}
