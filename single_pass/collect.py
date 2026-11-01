"""Fetch only list-level metadata, without visiting entries or images."""
import datetime as dt
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from lxml import html as html_tree

TZ = dt.timezone(dt.timedelta(hours=8))
SOURCES = {"toutiao": "今日头条", "baidu": "百度热搜", "weibo": "微博（TopHub）", "zhihu": "知乎（TopHub）", "ithome": "IT之家",
           "chinanews_society": "中新网社会", "bjnews_consumer": "新京报消费",
           "geekpark": "极客公园", "ifanr": "爱范儿", "solidot": "Solidot", "sspai": "少数派",
           "36kr": "36氪", "thepaper": "澎湃推荐", "chinanews_china": "中新网国内", "chinanews_finance": "中新网财经"}
DEFAULT_LIMIT = 40
SOURCE_CATEGORIES = {**{s: '综合热榜' for s in ('toutiao', 'baidu', 'weibo', 'zhihu')},
                     **{s: '科技' for s in ('ithome', 'geekpark', 'ifanr', 'solidot', 'sspai', '36kr')},
                     'chinanews_society': '社会民生', 'bjnews_consumer': '社会民生',
                     'chinanews_china': '国内', 'chinanews_finance': '财经', 'thepaper': '综合新闻'}
URLS = {"toutiao": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", "baidu": "https://top.baidu.com/board?tab=realtime", "tophub": "https://tophub.today/"}
URLS['ithome'] = 'https://www.ithome.com/rss/'
URLS['chinanews_society'] = 'https://www.chinanews.com.cn/rss/society.xml'
URLS['bjnews_consumer'] = 'https://www.bjnews.com.cn/industrial'
URLS.update({'geekpark': 'https://www.geekpark.net/rss', 'ifanr': 'https://www.ifanr.com/feed',
             'solidot': 'https://www.solidot.org/index.rss', 'sspai': 'https://sspai.com/feed',
             '36kr': 'https://www.36kr.com/feed', 'thepaper': 'https://www.thepaper.cn/',
             'chinanews_china': 'https://www.chinanews.com.cn/rss/china.xml',
             'chinanews_finance': 'https://www.chinanews.com.cn/rss/finance.xml'})


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


def item(source, title, url, cover=None, published=None, metrics=None, snapshot=None, source_key=None, source_ordinal=None, summary=None):
    return {"source_id": source, "source_name": SOURCES[source], "source_type": "current_hotlist", "title": title,
            "url": url, "entry_url": url, "original_url": None, "original_status": "unresolved", "resolution_evidence": [],
            "source_key": str(source_key) if source_key else None,
            "cover_url": cover or None, "published_at": published, "event_time": None, "summary": summary,
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
                 metrics={"hotScore": x.get("hotScore"), "index": x.get("index"), "isTop": x.get("isTop")},source_ordinal=n,summary=x.get('desc') or None)
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


def parse_rss(raw, source):
    rows = []
    for n, node in enumerate(ET.fromstring(raw).findall('./channel/item'), 1):
        title, url = node.findtext('title'), node.findtext('link')
        if not title or not url:
            continue
        stamp = node.findtext('pubDate')
        published = None
        if stamp:
            try:
                published = parsedate_to_datetime(stamp).isoformat()
            except (ValueError, TypeError):
                # 36Kr publishes an explicit ISO-style timestamp with a zone.
                published = dt.datetime.fromisoformat(' '.join(stamp.split())).isoformat()
        summary = html.unescape(re.sub(r'<[^>]+>', '', node.findtext('description') or '')).strip()
        row = item(source, ' '.join(title.split()), url, published=published, source_key=node.findtext('guid') or url,
                   source_ordinal=n, summary=summary or None)
        row['source_type'] = 'publisher_feed'
        rows.append(row)
    return rows


def parse_ithome(raw):
    return parse_rss(raw, 'ithome')


def parse_bjnews(raw):
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
        row = item('bjnews_consumer', title, url, cover=covers[0] if covers else None,
                   source_key=url, source_ordinal=len(rows) + 1)
        # The list has no publication timestamp. Do not derive one from IDs,
        # image paths, or first observation; the article reader must verify it.
        row['source_type'] = 'publisher_list'
        rows.append(row)
        seen.add(url)
    return rows


def parse_thepaper(raw):
    document = html_tree.fromstring(raw)
    scripts = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
    if not scripts:
        raise ValueError('澎湃页面缺少栏目数据')
    data = json.loads(scripts[0])['props']['pageProps']['data']
    rows, seen = [], set()
    for channel in data.get('recommendChannels') or []:
        for entry in channel.get('contentList') or []:
            key, title = str(entry.get('contId') or ''), entry.get('name')
            if not key.isdigit() or key in seen or not title or entry.get('trackArticleType') != '文章':
                continue
            # Keep the publisher's explicit outgoing link for syndicated items.
            url = entry.get('link') or 'https://www.thepaper.cn/newsDetail_forward_' + key
            stamp = entry.get('publishTime')
            published = dt.datetime.fromisoformat(stamp).replace(tzinfo=TZ).isoformat() if stamp else None
            row = item('thepaper', title, url, cover=entry.get('pic'), published=published,
                       source_key=key, source_ordinal=len(rows) + 1)
            row['source_type'] = 'publisher_list'
            rows.append(row)
            seen.add(key)
    return rows


PARSERS = {"toutiao": parse_toutiao, "baidu": parse_baidu, "ithome": parse_ithome,
           "chinanews_society": lambda s: parse_rss(s, 'chinanews_society'), "bjnews_consumer": parse_bjnews,
           "weibo": lambda s: parse_tophub(s, "weibo"), "zhihu": lambda s: parse_tophub(s, "zhihu")}
PARSERS.update({source: (lambda raw, source=source: parse_rss(raw, source)) for source in
                ('geekpark', 'ifanr', 'solidot', 'sspai', '36kr', 'chinanews_china', 'chinanews_finance')})
PARSERS['thepaper'] = parse_thepaper
