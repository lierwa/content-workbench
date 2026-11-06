"""Fetch only list-level metadata, without visiting entries or images."""
import datetime as dt
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from lxml import html as html_tree

TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT_LIMIT = 40
_specs = json.loads(Path(__file__).with_name('source_catalog.json').read_text(encoding='utf-8'))
CATALOG = {key: spec if isinstance(spec, dict) else {} for key, spec in _specs.items()}
SOURCES = {key: spec.get('name') or key for key, spec in CATALOG.items()}
SOURCE_CATEGORIES = {key: spec.get('category') or '其他' for key, spec in CATALOG.items()}
URLS = {key: spec.get('url', '') for key, spec in CATALOG.items()}


def snapshot_key(source):
    key = CATALOG[source].get('snapshot', source)
    if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', key):
        raise ValueError('渠道快照标识无效')
    if not canonical(URLS[source]):
        raise ValueError('渠道入口必须是有效的 HTTP 或 HTTPS 地址')
    if any(spec.get('snapshot', other) == key and URLS[other] != URLS[source] for other, spec in CATALOG.items()):
        raise ValueError('共用采集快照的渠道须使用相同入口')
    return key


def is_publisher_host(source, host):
    feed_host = urlsplit(URLS.get(source, '')).hostname
    allowed = [feed_host, *CATALOG.get(source, {}).get('article_hosts', [])]
    return bool(host and any(h and host.removeprefix('www.') == h.removeprefix('www.') for h in allowed))


def now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def request_headers(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    if urlsplit(url).hostname == 'news.xbox.com':
        headers = {'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
                   'Accept':'text/html,application/xhtml+xml,*/*;q=0.8'}
    return headers


def fetch(url):
    request = urllib.request.Request(url, headers=request_headers(url))
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(3_000_001)
        if len(body) > 3_000_000:
            raise ValueError("榜单响应超过3 MB")
        return body.decode("utf-8", "replace")


def canonical(url):
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        _ = parts.port  # Reject a malformed port before this entry reaches the pool.
    except ValueError:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def publication_time(stamp, *, rss=False):
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    if rss:
        try:
            return parsedate_to_datetime(stamp).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        value = dt.datetime.fromisoformat(' '.join(stamp.split()))
    except ValueError:
        return None
    if not rss and value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.isoformat()


def item(source, title, url, cover=None, published=None, metrics=None, snapshot=None, source_key=None, source_ordinal=None, summary=None):
    return {"source_id": source, "source_name": SOURCES[source], "source_type": "current_hotlist", "title": title,
            "url": url, "entry_url": url, "original_url": None, "original_status": "unresolved", "resolution_evidence": [],
            "source_key": str(source_key) if source_key else None,
            "cover_url": cover if isinstance(cover, str) and cover else None,
            "published_at": published, "event_time": None, "summary": summary if isinstance(summary, str) else None,
            "rank": (metrics or {}).get('rank'), "metrics": metrics or {}, "platform_snapshot_time": snapshot, "source_ordinal": source_ordinal}


def parse_toutiao(raw, source='toutiao'):
    data = json.loads(raw)
    if data.get("status") != "success" or not isinstance(data.get("data"), list):
        raise ValueError("头条响应状态或结构异常")
    return [item(source, x.get("Title"), x.get("Url"), x["Image"].get("url") if isinstance(x.get("Image"), dict) else None,
                 metrics={"HotValue": x.get("HotValue"), "source_order": n}, source_key=x.get("ClusterIdStr") or x.get("ClusterId"),source_ordinal=n)
            for n, x in enumerate(data["data"], 1) if isinstance(x, dict) and x.get("Title") and x.get("Url")]


def parse_baidu(raw, source='baidu'):
    m = re.search(r"<!--s-data:(.*?)-->", raw, re.S)
    if not m:
        raise ValueError("百度页面无 s-data 榜单")
    data = json.loads(m.group(1))
    cards = data.get("data", {}).get("cards", [])
    card = next((c for c in cards if isinstance(c, dict) and c.get("component") == "hotList"), None)
    if card is None or not isinstance(card.get("content"), list):
        raise ValueError("百度页面无 hotList")
    return [item(source, x.get("word"), x.get("url") or x.get("rawUrl"), x.get("img"),
                 metrics={"hotScore": x.get("hotScore"), "index": x.get("index"), "isTop": x.get("isTop")},source_ordinal=n,summary=x.get('desc') or None)
            for n,x in enumerate(card["content"],1) if isinstance(x, dict) and x.get("word") and (x.get("url") or x.get("rawUrl"))]


def parse_tophub(raw, source):
    node = re.escape(CATALOG[source]['node'])
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


def parse_rss(raw, source):
    rows = []
    for n, node in enumerate(ET.fromstring(raw).findall('./channel/item'), 1):
        title, url = node.findtext('title'), node.findtext('link')
        if not title or not url:
            continue
        published = publication_time(node.findtext('pubDate'), rss=True)
        summary = html.unescape(re.sub(r'<[^>]+>', '', node.findtext('description') or '')).strip()
        row = item(source, ' '.join(title.split()), url, published=published, source_key=node.findtext('guid') or url,
                   source_ordinal=n, summary=summary or None)
        row['source_type'] = 'publisher_feed'
        rows.append(row)
    return rows


def parse_bjnews(raw, source='bjnews_consumer'):
    # Only the channel's article cards: sidebars and pinned multi-article
    # specials are not this channel's current article list.
    document = html_tree.fromstring(raw)
    cards = document.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," pin_demo ")]')
    rows, seen = [], set()
    for card in cards:
        links = card.xpath('./a[div[contains(concat(" ",normalize-space(@class)," ")," pin_tit ")]]')
        if not links:
            continue
        link = links[0]
        url = canonical(link.get('href'))
        if not re.fullmatch(r'https://www\.bjnews\.com\.cn/detail/\d+\.html', url) or url in seen:
            continue
        title = ' '.join(link.text_content().split())
        if not title:
            continue
        covers = card.xpath('.//img/@src')
        row = item(source, title, url, cover=covers[0] if covers else None,
                   source_key=url, source_ordinal=len(rows) + 1)
        # The list has no publication timestamp. Do not derive one from IDs,
        # image paths, or first observation; the article reader must verify it.
        row['source_type'] = 'publisher_list'
        rows.append(row)
        seen.add(url)
    return rows


def parse_thepaper(raw, source='thepaper'):
    document = html_tree.fromstring(raw)
    scripts = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
    if not scripts:
        raise ValueError('澎湃页面缺少栏目数据')
    data = json.loads(scripts[0])['props']['pageProps']['data']
    rows, seen = [], set()
    for channel in data.get('recommendChannels') or []:
        if not isinstance(channel, dict) or not isinstance(channel.get('contentList'), list):
            continue
        for entry in channel.get('contentList') or []:
            if not isinstance(entry, dict):
                continue
            key, title = str(entry.get('contId') or ''), entry.get('name')
            if not key.isdigit() or key in seen or not title or entry.get('trackArticleType') != '文章':
                continue
            # Keep the publisher's explicit outgoing link for syndicated items.
            url = entry.get('link') or 'https://www.thepaper.cn/newsDetail_forward_' + key
            published = publication_time(entry.get('publishTime'))
            row = item(source, title, url, cover=entry.get('pic'), published=published,
                       source_key=key, source_ordinal=len(rows) + 1)
            row['source_type'] = 'publisher_list'
            rows.append(row)
            seen.add(key)
    return rows


READERS = {'rss':parse_rss, 'tophub':parse_tophub, 'toutiao':parse_toutiao, 'baidu':parse_baidu,
           'bjnews':parse_bjnews, 'thepaper':parse_thepaper}


def parse_source(source, raw):
    if CATALOG[source].get('reader') == 'game':
        from . import game_sources
        if source=='game_itch_tools':
            return list(game_sources.itch_tools(raw,source,now()))
        if source in game_sources.PARSERS:
            return list(game_sources.PARSERS[source](raw,source,now()))
        if source in ('game_ps_blog','game_gcores'):
            return parse_rss(raw,source)
        raise ValueError('多页游戏来源须通过采集器读取并保存完整快照')
    reader = READERS.get(CATALOG[source].get('reader'))
    if reader is None:
        raise ValueError('渠道缺少对应的读取器')
    return reader(raw, source)


PARSERS = {key: partial(parse_source, key) for key in CATALOG}
