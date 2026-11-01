"""Read bounded text sources and verify quotations against the saved text."""
import concurrent.futures
import datetime as dt
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

try:
    import trafilatura
except ImportError:
    trafilatura = None

MAX_BYTES = 2_000_000
IMAGES_VERSION = 3
FAKE_IP = ipaddress.ip_network('198.18.0.0/15')


class _PublishedDate(HTMLParser):
    """Only trust explicit publication metadata, not dates guessed from URLs."""
    def __init__(self):
        super().__init__()
        self.value = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'meta' or self.value:
            return
        fields = {key.lower(): value for key, value in attrs if key}
        key = (fields.get('property') or fields.get('name') or fields.get('itemprop') or '').lower()
        if key in ('article:published_time', 'datepublished', 'pubdate', 'publishdate'):
            self.value = fields.get('content') or None


def _host_is(host, suffix):
    return host == suffix or host.endswith('.' + suffix)


def _validate_url(url, timeout, entry=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('仅支持不含凭据的 HTTP(S) 链接')
    host = parsed.hostname.lower().rstrip('.')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    path = parsed.path.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('不访问本机或内网地址')
    if any(_host_is(host, h) for h in ('bilibili.com', 'b23.tv', 'youtube.com', 'youtu.be')):
        raise ValueError('不读取视频平台')
    if path.endswith(('.mp3', '.mp4', '.m4a', '.wav', '.avi', '.mov', '.m3u8', '.webm')):
        raise ValueError('不下载音频或视频')
    if not entry and ((host in ('baidu.com','www.baidu.com','m.baidu.com','top.baidu.com') and (host == 'top.baidu.com' or path.startswith('/s') or path.startswith('/board'))) or
        (_host_is(host, 'bing.com') and path.startswith('/search')) or
        (_host_is(host, 'google.com') and path.startswith('/search')) or
        (host == 's.weibo.com') or
        (any(_host_is(host, h) for h in ('weibo.com', 'weibo.cn')) and
         ('/search' in path or path.startswith('/p/100808'))) or
        (_host_is(host, 'zhihu.com') and path.startswith('/question/') and '/answer/' not in path) or
        _host_is(host, 'tophub.today')):
        raise ValueError('搜索、话题、提问或榜单页不能作为原文')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise ValueError('不支持非标准 IP 地址')
        addresses = {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        # A local proxy can expose synthetic DNS addresses; validate their real
        # A and AAAA answers using a public DNS-over-HTTPS endpoint.
        if addresses and all(a.version == 4 and a in FAKE_IP for a in addresses):
            addresses = set()
            for kind in ('A', 'AAAA'):
                query = urllib.parse.urlencode({'name': host, 'type': kind})
                req = urllib.request.Request('https://cloudflare-dns.com/dns-query?' + query,
                    headers={'Accept': 'application/dns-json'})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    answer = json.loads(response.read(262145))
                if answer.get('Status') != 0:
                    raise ValueError('DNS 查询失败')
                for item in answer.get('Answer', []):
                    if item.get('type') in (1, 28):
                        addresses.add(ipaddress.ip_address(item['data']))
        if not addresses or any(not a.is_global or a.is_multicast or a.is_reserved for a in addresses):
            raise ValueError('域名含非公网地址或无法确认公网地址')
    else:
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError('不访问本机或内网地址')
        addresses = {address}
    return parsed, sorted(addresses, key=lambda value: (value.version, str(value)))[0]


def _read(url, timeout, entry=False):
    if trafilatura is None:
        raise ValueError('缺少 trafilatura，无法提取正文')
    deadline = time.monotonic() + timeout
    final_url = url
    for redirects in range(4):
        parsed, address = _validate_url(final_url, max(0.1, deadline - time.monotonic()), entry=entry)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('抓取超时')
        # Connect to the verified IP itself, keeping the original host for TLS
        # and HTTP. An intermediary must not resolve the host again.
        sock = socket.create_connection((str(address), port), timeout=remaining)
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
        try:
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            conn.sock = sock
            path = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            host_header = parsed.hostname if port in (80, 443) else f'{parsed.hostname}:{port}'
            conn.request('GET', path, headers={'Host': host_header, 'User-Agent': 'Mozilla/5.0',
                         'Accept': 'text/html', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if redirects == 3 or not response.getheader('Location'):
                    raise ValueError('重定向超过 3 次或缺少目标')
                final_url = urllib.parse.urljoin(final_url, response.getheader('Location'))
                continue
            if response.status != 200:
                raise ValueError(f'HTTP {response.status}')
            if response.headers.get_content_type() not in ('text/html', 'application/xhtml+xml'):
                raise ValueError('响应不是 HTML 文字页面')
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise ValueError('不支持压缩正文')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('页面超过 2 MB')
            charset = response.headers.get_content_charset()
            page = raw.decode(charset, errors='replace') if charset else trafilatura.utils.decode_file(raw)
            break
        finally:
            conn.close()
            sock.close()
    return parse_material(page, final_url, entry=entry)


def parse_material(page, final_url, entry=False):
    """Use the same extractor for HTTP HTML and a browser-rendered document."""
    if trafilatura is None:
        raise ValueError('缺少 trafilatura，无法提取正文')
    text = trafilatura.extract(page, url=final_url, output_format='txt', include_comments=False,
                               include_tables=True, favor_precision=True)
    if not entry and (not text or len(text.strip()) < 80):
        raise ValueError('未取得足够的正文，可能是导航、脚本、登录或验证页')
    text = (text or '').strip()
    if any(marker in text[:250] for marker in ('请先登录', '登录后查看', '安全验证', '请输入验证码', '访问异常',
                                             '正在进行安全检测', '系统正在检测当前网络环境')):
        raise ValueError('页面要求登录或验证')
    metadata = trafilatura.extract_metadata(page, default_url=final_url)
    published = _PublishedDate()
    published.feed(page)
    if not published.value:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(page)
        host = urllib.parse.urlsplit(final_url).hostname
        nodes = (tree.xpath('//*[@id="pubtime_baidu"]') if host in ('www.chinanews.com.cn', 'chinanews.com.cn') else
                 tree.xpath('//span[contains(concat(" ",normalize-space(@class)," ")," timer ")]') if host == 'www.bjnews.com.cn' else [])
        if nodes:
            stamp = nodes[0].text_content().strip()
            if re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?', stamp):
                published.value = dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat()
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.urls = []
        def handle_starttag(self, tag, attrs):
            href = dict(attrs).get('href')
            if tag == 'a' and href: self.urls.append(urllib.parse.urljoin(final_url, href))
    links = Links(); links.feed(page)
    # Reuse the article extractor so navigation logos and recommendations do
    # not become article illustrations. Keep lazy-loaded URLs and source order.
    # The extractor otherwise prefers a placeholder src over data-original.
    from lxml import html as lxml_html
    image_page = lxml_html.fromstring(page, parser=lxml_html.HTMLParser(remove_comments=True))
    for image in image_page.iter('img'):
        lazy = image.get('data-original') or image.get('data-src')
        if lazy and urllib.parse.urlsplit(urllib.parse.urljoin(final_url, lazy)).scheme in ('http', 'https'):
            image.set('src', lazy)
    # On CNS pages the generic extractor includes the adjacent client-download
    # banner. Only images inside the site's observed article body are eligible.
    allowed_images = None
    if urllib.parse.urlsplit(final_url).hostname in ('www.chinanews.com.cn', 'chinanews.com.cn'):
        bodies = image_page.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," left_zw ")]')
        allowed_images = {urllib.parse.urljoin(final_url, i.get('src') or '')
                          for body in bodies for i in body.iter('img')}
    image_xml = trafilatura.extract(image_page, url=final_url, output_format='xml', include_images=True,
                                   include_comments=False, include_tables=True, favor_precision=True)
    images = []
    if image_xml:
        for graphic in ElementTree.fromstring(image_xml).iter('graphic'):
            url = urllib.parse.urljoin(final_url, graphic.get('src') or '')
            if allowed_images is not None and url not in allowed_images:
                continue
            if urllib.parse.urlsplit(url).scheme in ('http', 'https') and url != final_url and not any(i['url'] == url for i in images):
                images.append({'url': url, 'description': graphic.get('alt') or graphic.get('title') or '',
                               'source_url': final_url})
    return {'final_url': final_url, 'title': getattr(metadata, 'title', None),
            'links': list(dict.fromkeys(links.urls)),
            'images': images, 'images_version': IMAGES_VERSION,
            'publisher': getattr(metadata, 'sitename', None), 'author': getattr(metadata, 'author', None),
            'published_at': published.value, 'event_at': None,
            'content': text, 'content_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def fetch_material(url, timeout=15, entry=False):
    """Return one source or a clear failure; caller persists the returned record."""
    result = {'url': url, 'final_url': None, 'status': 'failed', 'title': None,
              'publisher': None, 'author': None, 'published_at': None, 'event_at': None,
              'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
              'content': None, 'content_sha256': None, 'error': None}
    try:
        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError('超时必须大于零')
        # Resolver and response reads may block; return at the overall deadline.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_read, url, timeout, entry)
        try:
            result.update(future.result(timeout=timeout))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        result['status'] = 'read'
    except concurrent.futures.TimeoutError:
        result['error'] = '抓取或正文提取超时'
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    return result


def source_defaults(item):
    return dict(item, entry_url=item.get('entry_url') or item.get('url'),
        original_url=item.get('original_url'), original_status=item.get('original_status') or 'unresolved',
        resolution_evidence=item.get('resolution_evidence') or [])


def direct_post(url):
    p = urllib.parse.urlsplit(url or '')
    host, path = (p.hostname or '').lower(), p.path
    return bool((host.endswith('zhihu.com') and re.fullmatch(r'/question/\d+/answer/\d+/?', path))
        or (host == 'zhuanlan.zhihu.com' and re.fullmatch(r'/p/\d+/?', path))
        or (host.endswith('toutiao.com') and re.fullmatch(r'/(?:article|w)/\d+/?', path))
        or (host in ('weibo.com','www.weibo.com','m.weibo.cn') and
            re.fullmatch(r'/(?:\d+/[A-Za-z0-9]+|detail/\d+|status/[A-Za-z0-9]+)/?', path)))


def bind_material(value, role, refs):
    identity = value.get('final_url') or value['url']
    return dict(value, material_id=hashlib.sha256(identity.encode()).hexdigest()[:20], role=role, origin_ref=refs)


def verify_material_quote(material, quote):
    """Locate quotes in saved text; author/publisher can support source credits."""
    return any(verify_quote(material.get(field) or '', quote) for field in ('title', 'content', 'author', 'publisher'))


def resolve_original(item, cancel):
    """Only collected direct posts or actual question-to-answer links establish provenance."""
    from . import collect, screening_policy as policy
    row = source_defaults(item)
    entry = row['entry_url'] or ''
    source_host = urllib.parse.urlsplit(collect.URLS.get(item.get('source_id'), '')).hostname or ''
    entry_host = urllib.parse.urlsplit(entry).hostname or ''
    publisher_entry = (item.get('source_type') in ('publisher_feed', 'publisher_list') and source_host
                       and entry_host.removeprefix('www.') == source_host.removeprefix('www.'))
    target = entry if direct_post(entry) or publisher_entry else None
    evidence = [{'kind': 'collected_direct_link', 'entry_url': entry}] if target else []
    parts = urllib.parse.urlsplit(entry)
    question = re.fullmatch(r'/question/(\d+)/?', parts.path) if (parts.hostname or '').endswith('zhihu.com') else None
    if question:
        page = fetch_material(entry, min(15, policy.remaining(cancel)), entry=True)
        row['resolution_attempt'] = {k: page.get(k) for k in ('fetched_at','status','error','final_url')}
        target = next((u for u in page.get('links', []) if direct_post(u) and
            urllib.parse.urlsplit(u).path.startswith('/question/' + question[1] + '/answer/')), None)
        if target:
            evidence = [{'kind': 'question_answer_link', 'entry_url': entry, 'question_url': page['final_url'],
                'answer_url': target, 'page_sha256': page.get('content_sha256'), 'fetched_at': page['fetched_at']}]
    if not target:
        row.update(original_url=None, original_status='unresolved', resolution_evidence=[])
        return row, None
    material = fetch_material(target, min(15, policy.remaining(cancel)))
    evidence.append({'kind':'fetch', 'url':target, 'final_url':material.get('final_url'), 'fetched_at':material['fetched_at']})
    row.update(original_url=target, original_status='resolved' if material['status']=='read' else 'unavailable',
        resolution_evidence=evidence)
    return row, bind_material(material, 'original', [row['ref']])


def verify_quote(content, quote):
    """Accept only a nonempty exact substring after whitespace normalization."""
    if not isinstance(content, str) or not isinstance(quote, str):
        return False
    normalized = ' '.join(quote.split())
    return bool(normalized) and normalized in ' '.join(content.split())
