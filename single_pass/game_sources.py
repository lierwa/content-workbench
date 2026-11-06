"""List adapters for the game columns. All links returned here are public item pages."""

from datetime import datetime, timedelta
from pathlib import Path
import json
import hashlib
import re
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlencode
from xml.etree import ElementTree

from lxml import html

from . import collect


def clean(value):
    return " ".join(str(value or "").split())


def txt(node, xpath):
    return clean(node.xpath("string(" + xpath + ")"))


def stamp(value, observed=None):
    text = clean(value)
    when = datetime.fromisoformat(observed or collect.now()).astimezone(collect.TZ)
    relative_clock = re.search(r"(昨天|前天|今天|\d+\s*(?:小时|分钟|天)前).*?(\d{1,2}):(\d{2})", text)
    if relative_clock:
        label, hour, minute = relative_clock.groups()
        if "前" in label and label != "前天":
            delta = re.search(r"(\d+)\s*(小时|分钟|天)", label)
            shift = {"小时": 3600, "分钟": 60, "天": 86400}[delta[2]] * int(delta[1])
            day = when - timedelta(seconds=shift)
        else:
            day = when - timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[label])
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0).isoformat()
    full = re.search(
        r"(20\d{2})\s*[./年-]\s*(\d{1,2})\s*[./月-]\s*(\d{1,2})(?:日)?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        text,
    )
    if full:
        try:
            y, m, d, h, minute, sec = full.groups()
            return datetime(
                int(y), int(m), int(d), int(h or 0), int(minute or 0), int(sec or 0), tzinfo=collect.TZ
            ).isoformat()
        except ValueError:
            return None
    md = re.search(r"(\d{1,2})[月-](\d{1,2})(?:日)?(?:\s+(\d{1,2}):(\d{2}))?", text)
    if md:
        mon, day, h, minute = md.groups()
        year = when.year + (
            1 if when.month == 12 and int(mon) < 4 else -1 if when.month < 3 and int(mon) > 10 else 0
        )
        try:
            return datetime(
                year, int(mon), int(day), int(h or 0), int(minute or 0), tzinfo=collect.TZ
            ).isoformat()
        except ValueError:
            return None
    rel = re.search(r"(\d+)\s*(分钟|小时|天)前", text)
    if rel:
        delta = {"分钟": 60, "小时": 3600, "天": 86400}[rel[2]] * int(rel[1])
        return (when - timedelta(seconds=delta)).replace(second=0, microsecond=0).isoformat()
    return None


def row(source, title, url, key, summary="", published=None, event_time=None, **game):
    link = urljoin(collect.URLS[source], url)
    if not collect.is_publisher_host(source, urlsplit(link).hostname or ""):
        raise ValueError("游戏来源出现未注册的详情主机")
    value = collect.item(
        source, clean(title), link, source_key=key, published=published, summary=clean(summary) or None
    )
    value.update(source_type="publisher_list", event_time=event_time, game=game)
    return value


def news_3dm(raw, source, observed):
    tree = html.fromstring(raw)
    for node in tree.xpath('//li[contains(concat(" ",normalize-space(@class)," ")," selectpost ")]'):
        a = node.xpath('.//a[@class="bt"]')
        if not a:
            continue
        link = a[0].get("href", "")
        key = re.search(r"/news/\d{6}/(\d+)\.html$", link)
        date = stamp(txt(node, './/span[@class="time"]'), observed)
        if key and date and date <= observed:
            yield row(
                source,
                a[0].text_content(),
                link,
                key[1],
                txt(node, './/div[@class="miaoshu"]')[:440],
                date,
            )


def steam(raw, source, observed):
    tree = html.fromstring(raw)
    for a in tree.xpath("//a[@data-ds-appid]"):
        app = a.get("data-ds-appid", "")
        if not app.isdigit() or "/app/" not in a.get("href", ""):
            continue
        title = txt(a, './/span[@class="title"]')
        released = txt(a, './/*[contains(@class,"search_released")]')
        price = txt(a, './/*[contains(@class,"discount_final_price")]')
        summary = f"中国区 Steam 热门新品。商店发行日期：{released}。"
        yield row(
            source,
            title,
            f"https://store.steampowered.com/app/{app}/?cc=CN&l=schinese",
            app,
            summary,
            event_time=stamp(released, observed),
            app_id=app,
            market="CN",
            price_observed=price,
        )


def hykb(raw, source, observed):
    tree = html.fromstring(raw)
    seen = set()
    for section in tree.xpath('//div[@class="NG-panel"]'):
        date = stamp(txt(section, './div[@class="label"]'), observed)
        for a in section.xpath('.//div[@class="g-left"]/a[.//div[@class="g-name"]]'):
            url = a.get("href", "")
            ident = re.search(r"/a/(\d+)\.htm", url)
            if not ident:
                continue
            title = txt(a, './/div[@class="g-name"]')
            info = txt(a, './/div[@class="g-info"]')
            # Strip the score widget; the remaining text belongs to the event.
            score = txt(a, './/i[@class="score"]')
            if score and info.startswith(score):
                info = info[len(score) :].strip()
            kinds = [k for k in ("招募", "预下载", "试玩", "上线", "测试", "更新", "预约") if k in info]
            kind = "/".join(kinds) or "动态"
            if info.startswith(("定档", "宣布定档")):
                kind = "定档公告"
            # The same calendar repeats whole day panels. Distinct test rounds
            # need their stage labels rather than the current observation date.
            rounds = re.findall(
                r"(?:第[一二三四五\d]+轮|首测|终测|[一二三四]测|限量|删档|官服|国际服|体验服)",
                title + " " + info,
            )
            identity = ":".join([ident[1], kind, *dict.fromkeys(rounds)])
            if kind == "动态" or (kind == "上线" and "正式上线" not in info) or kind == "更新":
                # Distinct named in-game events share the same game's detail URL.
                # A clock or date change alone should not create a new event.
                label = re.sub(r"(?:\d{1,2}[:：]\d{2}|\d{1,2}月\d{1,2}日)", "", info)
                identity += ":" + hashlib.sha256(clean(label).encode()).hexdigest()[:16]
            signature = (identity, date, info)
            if signature in seen:
                continue
            seen.add(signature)
            summary = (f"排期日 {date[:10]}：" if date else "") + info
            yield row(
                source,
                title,
                url,
                identity,
                summary,
                event_time=date,
                game_id=ident[1],
                event_type=kind,
                calendar=True,
            )


def nova_logs(raw, source, observed):
    tree = html.fromstring(raw)
    for card in tree.xpath('//article[.//div[@class="portfolio-desc"]]'):
        a = card.xpath('.//h4/a[contains(@href,"/blogread/")]')
        if not a:
            continue
        a = a[0]
        date = txt(card, './/small[contains(@class,"pull-left")]')
        author = txt(card, './/div[@class="portfolio-desc"]/span/a')
        published = stamp(date.split("（")[0], observed)
        params = {"author": author}
        if "更新" in date:
            params["changed_at"] = stamp(date.split("更新")[-1], observed)
        yield row(
            source,
            a.text_content(),
            a.get("href"),
            a.get("href").rstrip("/").split("/")[-1],
            (author + "：" if author else "") + txt(card, './/span[contains(@class,"topmargin")]')[:400],
            published,
            **params,
        )


def nintendo(raw, source, observed):
    tree = html.fromstring(raw)
    for a in tree.xpath('//a[contains(@href,"/topics/article/")]'):
        title = txt(a, './/*[contains(@class,"Unit__name")]')
        if title:
            yield row(
                source,
                title,
                a.get("href"),
                a.get("href").split("/")[-1],
                title,
                stamp(txt(a, './/*[contains(@class,"Unit__release")]'), observed),
                market="HK",
            )


def xbox(raw, source, observed):
    tree = html.fromstring(raw)
    for a in tree.xpath('//main//a[contains(@class,"ep") and @href]'):
        url = a.get("href")
        m = re.search(r"/en-us/(20\d\d/\d\d/\d\d)/[^/?]+", url)
        title = txt(a, './/*[contains(concat(" ",@class," ")," ep__t ")]')
        if m and title:
            yield row(source, title, url, url, title, stamp(m[1], observed), market="US")


def ark(raw, source, observed):
    tree = html.fromstring(raw)
    for card in tree.xpath('//*[contains(concat(" ",@class," ")," moment-list-item ")]'):
        a = card.xpath('.//*[contains(@class,"moment-article")]//a[contains(@href,"/moment/")]')
        if not a:
            continue
        a = a[0]
        url = a.get("href", "")
        m = re.search(r"/moment/(\d+)", url)
        if not m:
            continue
        detail = clean(a.text_content())
        title = detail.split(" //")[0].split("//")[0][:85] or detail[:85]
        date = card.xpath('.//*[contains(@class,"moment-card__time")]/@title')
        yield row(
            source,
            title,
            url,
            m[1],
            detail[:450],
            stamp(date[0], observed) if date else None,
            game_name="明日方舟",
        )


def godot(raw, source, observed):
    tree = html.fromstring(raw)
    for card in tree.xpath('//li[@class="asset-item"]'):
        links = card.xpath('.//a[@class="asset-header"]')
        if not links:
            continue
        link = links[0]
        url = link.get("href", "")
        key = url.rstrip("/").split("/")[-1]
        labels = [clean(s) for s in card.xpath('.//span[contains(@class,"label")]/text()')]
        title = txt(link, ".//h4")
        if key.isdigit() and title:
            yield row(
                source,
                title,
                url,
                key,
                clean(card.text_content()),
                stamp(card.text_content(), observed),
                license_label=labels[-1] if labels else None,
            )


def factorio_logs(raw, source, observed):
    cards = html.fromstring(raw).xpath('//*[contains(concat(" ",@class," ")," blog-card ")]')
    if not cards:
        raise ValueError("Factorio 开发日志列表不可读")
    for card in cards:
        links = card.xpath(".//h3/a[@href]")
        if not links:
            continue
        a = links[0]
        url = a.get("href")
        if not re.fullmatch(r"/blog/post/fff-\d+", url):
            continue
        author = txt(card, './/*[@class="posted-by"]//a')
        date = stamp(txt(card, './/*[@class="posted-by"]'), observed)
        yield row(
            source,
            a.text_content(),
            url,
            url.rsplit("/", 1)[-1],
            txt(card, './/*[contains(@class,"snippet")]'),
            published=date,
            author=author,
            game_name="Factorio",
        )


PARSERS = {
    "game_factorio_logs": factorio_logs,
    "game_3dm_news": news_3dm,
    "game_steam_new": steam,
    "game_hykb_calendar": hykb,
    "game_indienova_logs": nova_logs,
    "game_nintendo_hk": nintendo,
    "game_xbox_wire": xbox,
    "game_ark_official": ark,
    "game_godot_tools": godot,
}


def github_pages(source, observed, read):
    cutoff = (datetime.fromisoformat(observed) - timedelta(days=21)).date()
    projects = {}
    use_topic_site = False
    for topic in ("game-development", "pixel-art"):
        qs = urlencode(
            {
                "q": f"topic:{topic} archived:false fork:false pushed:>={cutoff}",
                "sort": "stars",
                "per_page": 12,
            }
        )
        if not use_topic_site:
            try:
                data = json.loads(read("https://api.github.com/search/repositories?" + qs))
                if data.get("incomplete_results"):
                    raise ValueError("GitHub returned incomplete search results")
                repos = data["items"]
            except OSError as exc:
                if not (
                    getattr(exc, "code", None) in (403, 429) or re.search(r"\b(403|429)\b", str(exc))
                ):
                    raise
                use_topic_site = True
        if use_topic_site:
            repos = github_topic(read, topic, cutoff)
        for repo in repos:
            projects[repo["id"]] = repo
    releases_available = True
    for repo in projects.values():
        info = repo["description"] or ""
        meta = {
            "repo_id": repo["id"],
            "stars_observed": repo["stargazers_count"],
            "observed_at": observed,
            "license_label": (repo.get("license") or {}).get("spdx_id"),
            "repository": repo["full_name"],
        }
        yield row(source, repo["full_name"], repo["html_url"], str(repo["id"]), info, **meta)
        # Older frozen trials contain the REST responses. Preserve replay of those exact bytes.
        archived = getattr(read, "archived_urls", None)
        if archived is None or any(url.endswith("/releases.atom") for url in archived):
            try:
                yield from github_atom(source, repo, meta, read)
            except OSError:
                continue  # snapshot() retains the failing feed URL and error.
            continue
        url = "https://api.github.com/repos/" + repo["full_name"] + "/releases?per_page=2"
        if not releases_available:
            continue
        try:
            releases = json.loads(read(url))
        except (OSError, ValueError) as exc:
            if getattr(exc, "code", None) in (403, 429) or re.search(r"\b(403|429)\b", str(exc)):
                releases_available = False
            continue  # read() records the failed subrequest in the source manifest.
        for rel in releases:
            if rel.get("draft") or not rel.get("published_at"):
                continue
            yield row(
                source,
                repo["full_name"] + " " + rel["tag_name"],
                rel["html_url"],
                f"{repo['id']}:release:{rel['id']}",
                info + " " + clean(rel.get("body"))[:360],
                collect.publication_time(rel["published_at"]),
                **meta,
            )


def github_topic(read, topic, cutoff):
    tree = html.fromstring(read(f"https://github.com/topics/{topic}?o=desc&s=stars"))
    cards = tree.xpath("//article[.//h3/a[@data-hydro-click]]")
    if not cards:
        raise ValueError("GitHub topic 页未读到项目卡片")
    for card in cards:
        link = card.xpath(".//h3/a[last()]")[0]
        name = link.get("href", "").strip("/")
        dates = card.xpath(".//relative-time/@datetime")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", name) or not dates or dates[0][:10] < cutoff.isoformat():
            continue
        ident = json.loads(link.get("data-hydro-click"))["payload"]["record_id"]
        stars = card.xpath('.//*[@title and contains(@aria-label,"starred")]/@title')
        yield {
            "id": ident,
            "full_name": name,
            "html_url": "https://github.com/" + name,
            "description": txt(card, ".//p"),
            "stargazers_count": int(stars[0].replace(",", "")) if stars else None,
        }


def github_atom(source, repo, meta, read):
    base = repo["html_url"]
    feed = ElementTree.fromstring(read(base + "/releases.atom"))
    ns = "{http://www.w3.org/2005/Atom}"
    if feed.tag != ns + "feed":
        raise ValueError("GitHub 发布订阅未返回 Atom feed")
    for entry in feed.findall(ns + "entry")[:2]:
        link = entry.find(ns + "link")
        url = link.get("href", "") if link is not None else ""
        prefix = f"tag:github.com,2008:Repository/{repo['id']}/"
        ident = entry.findtext(ns + "id", "")
        if not ident.startswith(prefix) or not url.startswith(base + "/releases/tag/"):
            raise ValueError("GitHub 发行记录与所属仓库不匹配")
        tag = ident[len(prefix) :]
        updated = collect.publication_time(entry.findtext(ns + "updated"))
        published = collect.publication_time(entry.findtext(ns + "published"))
        description = entry.findtext(ns + "content", "")
        excerpt = clean(html.fromstring(description).text_content()) if description.strip() else ""
        yield row(
            source,
            repo["full_name"] + " " + entry.findtext(ns + "title", tag),
            url,
            f"{repo['id']}:tag:{tag}",
            clean(repo["description"]) + " " + excerpt[:360],
            published,
            changed_at=updated,
            release_tag=tag,
            **meta,
        )


def github_trending(source, observed, read):
    # GitHub's Trending list is a separate signal from cumulative repository stars.
    for language in ("", "gdscript"):
        url = "https://github.com/trending" + ("/" + language if language else "") + "?since=weekly"
        raw = read(url)
        cards = html.fromstring(raw).xpath('//article[contains(@class,"Box-row")][.//h2/a]')
        if not cards:
            raise ValueError("未读到 GitHub Trending 的仓库排行")
        for rank, card in enumerate(cards, 1):
            a = card.xpath(".//h2/a")[0]
            repo = a.get("href", "").strip("/")
            desc = txt(card, ".//p")
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                continue
            if not language and not re.search(
                r"\b(game|games|gaming|gamedev|godot|sprites?|pixel art|tilemap|gameplay|unity|unreal)\b",
                repo + " " + desc,
                re.I,
            ):
                continue
            ident = json.loads(a.get("data-hydro-click"))["payload"]["record_id"]
            delta = clean(" ".join(card.xpath('.//span[contains(@class,"float")]/text()')))
            value = row(
                source,
                repo,
                "https://github.com/" + repo,
                str(ident),
                desc,
                repository=repo,
                repo_id=ident,
                discovery_url=url,
            )
            value["metrics"] = {
                "weekly_rank": rank,
                "weekly_stars_label": delta,
                "observed_at": observed,
                "list_url": url,
                "list_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "card": clean(card.text_content()),
            }
            yield value


def itch_tools(raw, source, observed):
    tree = html.fromstring(raw)
    for rank, card in enumerate(tree.xpath('//li[contains(@class,"tool_cell")]'), 1):
        anchors = card.xpath('.//a[starts-with(@href,"/games/made-with-")][normalize-space()]')
        if not anchors:
            continue
        a = anchors[0]
        slug = a.get("href").split("/")[-1]
        projects = [clean(x.text_content()) for x in anchors[1:]]
        project_url = card.xpath('.//*[@class="tool_link"]/a/@href')
        desc = txt(card, './/*[@class="short_text"]')
        value = row(
            source,
            clean(a.text_content()),
            a.get("href"),
            slug,
            desc or clean(a.text_content()) + " 的 itch.io 作品使用目录",
            tool_site=project_url[0] if project_url else None,
        )
        value["metrics"] = {
            "observed_rank": rank,
            "project_counts": projects,
            "observed_at": observed,
            "list_url": collect.URLS[source],
            "list_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "card": clean(card.text_content()),
        }
        yield value


def gcores_material(raw, article_url, api_url, article_id):
    from .materials import IMAGES_VERSION

    document = json.loads(raw)
    resource = document["data"]
    attrs = resource["attributes"]
    if str(resource["id"]) != article_id or resource.get("type") != "articles":
        raise ValueError("机核正文 ID 与文章不匹配")
    if (
        not attrs.get("is-published")
        or attrs.get("is-require-privilege")
        or attrs.get("is-free") is False
    ):
        raise ValueError("机核正文未公开或需要阅读权限")
    content = attrs.get("content")
    blocks = (json.loads(content) if isinstance(content, str) else content or {}).get("blocks", [])
    body = "\n".join(
        block["text"].strip()
        for block in blocks
        if block.get("type") != "atomic" and (block.get("text") or "").strip()
    )
    if len(body) < 80:
        raise ValueError("机核公开正文不足")
    author_id = (resource.get("relationships", {}).get("user", {}).get("data") or {}).get("id")
    author = next(
        (
            x.get("attributes", {}).get("nickname")
            for x in document.get("included", [])
            if x.get("type") == "users" and x.get("id") == author_id
        ),
        None,
    )
    return {
        "final_url": article_url,
        "title": attrs["title"],
        "published_at": attrs.get("published-at"),
        "publisher": "机核",
        "author": author,
        "content": body,
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "images": [],
        "images_version": IMAGES_VERSION,
        "links": [],
        "article_links": [],
        "payload_url": api_url,
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
    }


def editorial_index(source, observed, read):
    # The RSS publication date belongs to the article, not the products in it.
    feed = game_feed(read(collect.URLS[source]), source)
    for article in feed:
        if "/indie-game-news/" not in article["url"]:
            continue
        if not re.search(r"/guide-\d+", article["url"]):
            continue
        try:
            tree = html.fromstring(read(article["url"]))
        except (OSError, ValueError):
            continue
        body = tree.xpath('//article//*[contains(concat(" ",@class," ")," single-post ")]')
        if not body:
            continue
        seen = set()
        for link in body[0].xpath(".//iframe[@src] | .//a[@href]"):
            url = link.get("src") or link.get("href", "")
            m = re.search(r"^https://store\.steampowered\.com/(?:app|widget)/(\d+)", url)
            headings = link.xpath("preceding::h4[1]")
            name = (
                clean(link.text_content())
                if link.tag == "a"
                else (clean(headings[0].text_content()) if headings else "")
            )
            if not m or len(name) < 2 or m[1] in seen:
                continue
            seen.add(m[1])
            description = ""
            if link.tag == "iframe" and headings:
                # Only prose before this specific widget within its own heading.
                fragments = []
                sibling = headings[0].getnext()
                while (
                    sibling is not None
                    and sibling.tag not in ("h3", "h4")
                    and not sibling.xpath(".//iframe")
                ):
                    if sibling.tag == "p":
                        fragments.append(clean(sibling.text_content()))
                    sibling = sibling.getnext()
                description = " ".join(fragments)[:300]
            yield row(
                source,
                name,
                f"https://store.steampowered.com/app/{m[1]}/?cc=CN&l=schinese",
                m[1],
                description or f"编辑选品：{article['title']}。产品的类型、是否已经可玩待查。",
                article["published_at"],
                app_id=m[1],
                discovery_url=article["url"],
                market="CN",
            )


def collect_rows(source, observed, read):
    if source == "game_github_trending":
        return list(github_trending(source, observed, read))
    if source == "game_itch_tools":
        return list(itch_tools(read(collect.URLS[source]), source, observed))
    if source == "game_github_tools":
        return list(github_pages(source, observed, read))
    if source == "game_indienova_news":
        return list(editorial_index(source, observed, read))
    raw = read(collect.URLS[source])
    if source == "game_xbox_wire":
        # The permalink contains a US civil date; one edition can straddle Beijing midnight.
        # Bind each headline to the publisher's timestamp, including items below the homepage.
        cards = {entry["url"].rstrip("/"): entry for entry in xbox(raw, source, observed)}
        feed = game_feed(read("https://news.xbox.com/en-us/feed/"), source)
        for entry in cards.values():
            entry["published_at"] = None
        for entry in feed:
            key = entry["url"].rstrip("/")
            if key in cards:
                cards[key]["published_at"] = entry["published_at"]
                cards[key]["summary"] = entry["summary"] or cards[key]["summary"]
            else:
                cards[key] = entry
        return list(cards.values())
    if source not in PARSERS:
        return game_feed(raw, source)
    return list(PARSERS[source](raw, source, observed))


def game_feed(raw, source):
    from xml.etree import ElementTree

    if ElementTree.fromstring(raw).find("./channel") is None:
        raise ValueError("响应不是预期的游戏订阅列表")
    return collect.parse_rss(raw, source)


def snapshot(source, root, observed):
    """Capture every requested page once. Partial multi-page results retain their evidence."""
    from .screen import write_json

    directory = root / source
    directory.mkdir(exist_ok=True)
    pages = {}
    cooldown_path = root.parent / "github-search-cooldown.json"

    def read(url):
        if not collect.is_publisher_host(source, urlsplit(url).hostname):
            raise ValueError("未声明的游戏列表主机")
        path = directory / (hashlib.sha256(url.encode()).hexdigest() + ".txt")
        if path.exists():
            body = path.read_bytes()
            pages[url] = {"file": path.name, "sha256": hashlib.sha256(body).hexdigest()}
            return body.decode("utf-8")
        search = (
            urlsplit(url).hostname == "api.github.com" and urlsplit(url).path == "/search/repositories"
        )
        if search and cooldown_path.is_file():
            reset = json.loads(cooldown_path.read_text(encoding="utf-8"))["reset"]
            if time.time() < reset:
                message = f"HTTP 429: GitHub 搜索冷却至 {reset}，转读公开主题页"
                pages[url] = {"error": message, "retry_after_epoch": reset}
                raise OSError(message)
        try:
            raw = collect.fetch(url)
            path.write_bytes(raw.encode("utf-8"))
            pages[url] = {"file": path.name, "sha256": hashlib.sha256(raw.encode()).hexdigest()}
            return raw
        except Exception as exc:
            pages[url] = {"error": str(exc)}
            if search and isinstance(exc, HTTPError) and exc.code in (403, 429):
                now = int(time.time())
                limits = exc.headers or {}
                retry = limits.get("Retry-After", "")
                reset = limits.get("X-RateLimit-Reset", "")
                until = max(
                    now + (int(retry) if retry.isdecimal() else 60),
                    int(reset)
                    if limits.get("X-RateLimit-Remaining") == "0" and reset.isdecimal()
                    else 0,
                )
                write_json(cooldown_path, {"reset": until})
                pages[url]["retry_after_epoch"] = until
            raise

    try:
        rows = collect_rows(source, observed, read)
    finally:
        write_json(directory / "pages.json", {"source": source, "observed_at": observed, "pages": pages})
    # Some sites repeat entries on overlapping sections or across weekly issues.
    # Retain the newest evidence for each source identity.
    return unique_rows(rows), [v["error"] for v in pages.values() if "error" in v]


def unique_rows(rows):
    unique = {}
    for value in rows:
        key = value.get("source_key") or value["entry_url"]
        if key not in unique or (value.get("published_at") or "") > (
            unique[key].get("published_at") or ""
        ):
            unique[key] = value
    return list(unique.values())


def game_detail(url, page):
    """Keep only the subject's own description and availability on mixed product pages."""
    parts = urlsplit(url)
    if parts.hostname == "itch.io" and parts.path.startswith("/games/made-with-"):
        tree = html.fromstring(page)
        projects = tree.xpath(
            '//*[contains(concat(" ", @class, " "), " game_cell ")]//a[contains(@class,"title")]'
        )
        if projects:
            examples = [(clean(a.text_content()), a.get("href")) for a in projects[:12]]
            return {
                "content": txt(tree, "//h1")
                + "\nitch.io 在此工具名下列出的游戏项目：\n"
                + "\n".join(name + " " + url for name, url in examples),
                "fields": {"examples": examples},
            }
    if parts.hostname == "github.com":
        segments = parts.path.strip("/").split("/")
        if (
            len(segments) >= 5
            and segments[2] == "blob"
            and re.match(r"(?i)^(license|licence|copying)(\.|$)", segments[-1])
        ):
            tree = html.fromstring(page)
            for script in tree.xpath('//script[@type="application/json"]/text()'):
                payload = json.loads(script).get("payload", {})
                blob = payload.get("codeViewBlobLayoutRoute", {}).get("StyledBlob", {})
                if isinstance(blob.get("rawLines"), list):
                    return {
                        "content": "\n".join(blob["rawLines"]),
                        "fields": {"repository": "/".join(segments[:2]), "kind": "license"},
                    }
            return {"content": "", "fields": {}}
        if len(segments) == 2:
            tree = html.fromstring(page)
            articles = tree.xpath('//article[contains(@class,"markdown-body")]')
            if articles:
                text = "\n".join(
                    clean(p.text_content())
                    for p in articles[0].xpath(".//h1|.//h2|.//h3|.//p|.//li|.//pre")
                )
                stars = tree.xpath(f'//a[@href="/{segments[0]}/{segments[1]}/stargazers"]')
                if stars:
                    text += "\n" + clean(stars[0].text_content())
                return {"content": text, "fields": {"kind": "readme", "repository": "/".join(segments)}}
    if parts.hostname == "store.steampowered.com" and re.match(r"^/app/\d+", parts.path):
        tree = html.fromstring(page)
        name = txt(tree, '//*[@id="appHubAppName"]')
        about = txt(tree, '//*[@id="game_area_description"]')
        fields = {
            "name": name,
            "date": txt(tree, '//*[contains(@class,"release_date")]//*[@class="date"]'),
        }
        app_id = parts.path.split("/")[2]
        demo = tree.xpath(
            '//*[@id="demoGameBtn"] | //a[contains(@class,"btn") and contains(@href,"/demo/")]'
        )
        actions = tree.xpath(
            '//*[@id="game_area_purchase"]//*[contains(concat(" ",@class," ")," game_area_purchase_game ")]'
        )
        offers = []
        for offer in actions:
            heading = txt(offer, ".//h1") or txt(offer, ".//h2")
            normalized_name = "".join(name.casefold().split())
            if (
                name
                and normalized_name in "".join(heading.casefold().split())
                and not any(
                    w in heading.casefold()
                    for w in ("礼包", "bundle", "合集", "捆绑包", "soundtrack", "原声带")
                )
            ):
                action = " ".join(
                    clean(a.text_content())
                    for a in offer.xpath(
                        './/*[contains(concat(" ",@class," ")," game_purchase_action ")]'
                    )
                )
                if action and ("demo" in heading.casefold() or "试玩" in heading):
                    demo = True
                elif action:
                    offers.append(heading + " " + action)
        tags = [clean(a.text_content()) for a in tree.xpath('//a[contains(@class,"app_tag")]')]
        media = steam_gallery(tree, app_id, name, url)
        fields.update(
            **media,
            app_id=app_id,
            tags=tags,
            offers=offers,
            demo=bool(demo),
            dlc=bool(tree.xpath('//*[contains(@class,"game_area_dlc_bubble")]')),
            early_access=bool(
                tree.xpath(
                    '//*[@id="earlyAccessHeader"] | //div[contains(@class,"early_access_header")]'
                )
            ),
        )
        body = [
            name,
            fields["date"],
            txt(tree, '//*[contains(@class,"game_description_snippet")]'),
            about,
        ]
        if name and about:
            body.extend(["标签：" + "、".join(tags), *offers])
            if demo:
                body.append("该作品的商店页提供可下载试玩。")
            if fields["early_access"]:
                body.append("该作品的商店页标注抢先体验。")
            if media.get("trailer"):
                body.append("Steam 商店官方视频：" + media["trailer"]["title"] + "。")
        return {"content": "\n".join(filter(None, body)) if name and about else "", "fields": fields}
    is_nova_log = parts.hostname == "indienova.com" and "/blogread/" in parts.path
    is_factorio_log = parts.hostname in ("factorio.com", "www.factorio.com") and re.fullmatch(
        r"/blog/post/fff-\d+/?", parts.path
    )
    if is_nova_log or is_factorio_log:
        tree = html.fromstring(page)
        nodes = tree.xpath(
            '//article[contains(@class,"game-intro")]'
            if is_nova_log
            else '//div[contains(concat(" ",@class," ")," blog-post ")]'
        )
        if nodes:
            author = txt(tree, '//meta[@name="author"]/@content')
            title = txt(tree, "//h1") or txt(tree, "//title")
            body = nodes[0]
            paragraphs = [
                clean(p.text_content())
                for p in body.xpath(".//h2|.//h3|.//p|.//li")
                if clean(p.text_content())
            ]
            images = []
            for img in body.xpath(".//img[@src]"):
                link = urljoin(url, img.get("src"))
                host = urlsplit(link).hostname or ""
                if (
                    is_nova_log
                    and host.endswith(".indienova.com")
                    or is_factorio_log
                    and host == "cdn.factorio.com"
                ) and not any(x["url"] == link for x in images):
                    label = img.get("alt", "")
                    images.append(
                        {
                            "url": link,
                            "description": "" if label == "Image title" else label,
                            "source_url": url,
                        }
                    )
            return {
                "content": "\n".join(filter(None, [title, author, *paragraphs]))
                if paragraphs
                else clean(body.text_content()),
                "fields": {"author": author, "author_images": images},
            }
    if parts.hostname == "m.3839.com" and re.fullmatch(r"/a/\d+\.htm", parts.path):
        tree = html.fromstring(page)
        description = txt(tree, '//*[@id="zinfo3"]')
        actions = [clean(a.text_content()) for a in tree.xpath('//*[@id="game_down_btn"]//a')]
        name = txt(tree, "//h1") or txt(tree, "//title")
        return {
            "content": "\n".join(filter(None, [name, description, *actions])),
            "fields": {"name": name, "mobile_actions": actions},
        }
    return None


def steam_gallery(tree, app_id, name, url):
    players = tree.xpath('//*[@data-featuretarget="gamehighlight-desktopcarousel"]/@data-props')
    fields = {"gallery": [], "trailer": None}
    if not players:
        return fields
    data = json.loads(players[0])
    if data.get("appName") != name:
        return fields
    for shot in data.get("screenshots", []):
        link = shot.get("standard") or ""
        parts = urlsplit(link)
        if (
            parts.scheme == "https"
            and (parts.hostname or "").endswith(".steamstatic.com")
            and f"/apps/{app_id}/" in parts.path
        ):
            fields["gallery"].append(
                {"url": link, "description": name + " 官方商店实机截图", "source_url": url}
            )
    for movie in data.get("trailers", []):
        stream = movie.get("hlsManifest") or ""
        parts = urlsplit(stream)
        if (
            parts.scheme == "https"
            and (parts.hostname or "").endswith(".steamstatic.com")
            and f"/store_trailers/{app_id}/" in parts.path
            and movie.get("title")
            and tree.xpath('//*[@id="game_highlights"]')
        ):
            fields["trailer"] = {
                "title": movie["title"],
                "stream": stream,
                "player": url.split("#")[0] + "#game_highlights",
            }
            break
    return fields


def repo_observation(item, cancel):
    """Use the saved API response for this repository, including its observation time."""
    model_root = getattr(cancel, "model_root", None)
    snapshot = item.get("snapshot_ref")
    if not model_root or not snapshot or Path(snapshot).is_absolute():
        return {}
    data = Path(model_root).resolve().parents[1]
    manifest = (data / snapshot).resolve()
    if (
        not manifest.is_relative_to(data / "collections")
        or manifest.name != "pages.json"
        or not manifest.is_file()
    ):
        return {}
    index = json.loads(manifest.read_text(encoding="utf-8"))
    for url, entry in index.get("pages", {}).items():
        if (
            urlsplit(url).hostname != "api.github.com"
            or urlsplit(url).path != "/search/repositories"
            or not entry.get("file")
        ):
            continue
        path = (manifest.parent / entry["file"]).resolve()
        if path.parent != manifest.parent:
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            continue
        repo = next(
            (r for r in json.loads(raw)["items"] if r["id"] == item["game"].get("repo_id")), None
        )
        if repo and repo["full_name"] == item["game"].get("repository"):
            return {
                "list_url": url,
                "list_sha256": entry["sha256"],
                "observed_at": index["observed_at"],
                "card": f"GitHub 仓库 {repo['full_name']}，截至 {index['observed_at']} 累计 {repo['stargazers_count']} 星。",
            }
    return {}


def tool_references(item, original, cancel):
    """Follow the asset author's project link and its own license/demo links only."""
    from . import materials, screening_policy

    repo = (item.get("game") or {}).get("repository")
    documents = []
    metrics = item.get("metrics") or {}
    if not metrics.get("card") and item.get("source_id") == "game_github_tools":
        metrics = repo_observation(item, cancel)
    if metrics.get("card") and metrics.get("list_url") and metrics.get("list_sha256"):
        evidence = metrics["card"]
        material = materials.bind_material(
            {
                "url": metrics["list_url"],
                "final_url": metrics["list_url"],
                "title": item["title"] + " · 榜单观察",
                "content": evidence,
                "content_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
                "payload_sha256": metrics["list_sha256"],
                "fetched_at": metrics["observed_at"],
                "images": [],
                "status": "read",
                "content_kind": "publisher_ranking",
            },
            "reference",
            [item["ref"]],
        )
        material["material_id"] = hashlib.sha256((material["url"] + item["ref"]).encode()).hexdigest()[
            :20
        ]
        documents.append(material)
    if not repo and item.get("source_id") == "game_itch_tools":
        site = (item.get("game") or {}).get("tool_site") or ""
        if urlsplit(site).scheme not in ("http", "https"):
            return documents
        project = materials.fetch_material(site, min(15, screening_policy.remaining(cancel)))
        if project.get("status") != "read":
            return documents
        documents.append(materials.bind_material(project, "reference", [item["ref"]]))
        slug = item["source_key"].removeprefix("made-with-").replace("-", "").lower()
        for link in [site, *project.get("links", [])]:
            p = urlsplit(link)
            if (
                p.hostname == "github.com"
                and re.fullmatch(r"/[\w.-]+/[\w.-]+/?", p.path)
                and p.path.rstrip("/").split("/")[-1].lower().replace("-", "") == slug
            ):
                repo = p.path.strip("/")
                break
        if not repo:
            return documents
    if not repo:
        for link in original.get("links", []):
            parts = urlsplit(link)
            if parts.hostname == "github.com" and re.fullmatch(r"/[\w.-]+/[\w.-]+/?", parts.path):
                repo = parts.path.strip("/")
                break
    if not repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return documents
    canonical = "https://github.com/" + repo

    def fetch(url):
        doc = materials.fetch_material(url, min(15, screening_policy.remaining(cancel)))
        final = urlsplit(doc.get("final_url") or url)
        if final.hostname != "github.com" or not (
            final.path.strip("/") == repo or final.path.startswith("/" + repo + "/")
        ):
            return None
        bound = materials.bind_material(doc, "reference", [item["ref"]])
        documents.append(bound)
        return bound

    project = original if original["url"].rstrip("/") == canonical else fetch(canonical)
    if not project:
        return documents
    links = [link for link in project.get("links", []) if link.startswith(canonical + "/blob/")]
    license_link = next(
        (url for url in links if re.search(r"(?i)/(license|licence|copying)(\.\w+)?$", url)), None
    )
    if license_link:
        fetch(license_link)
    example = next(
        (
            url
            for url in links
            if re.search(r"(?i)/(examples?|demos?|tutorials?)/", url)
            and re.search(r"(?i)\.(md|rst)$", url)
        ),
        None,
    )
    if example:
        fetch(example)
    return documents
