"""Bounded public detail retrieval; no redirects or private network targets."""
import html.parser
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

class ArticleText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.suppressed = 0; self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == "article": self.depth += 1
        elif self.depth and tag in ("script", "style", "nav"): self.suppressed += 1
    def handle_endtag(self, tag):
        if self.depth and tag in ("script", "style", "nav") and self.suppressed: self.suppressed -= 1
        if tag == "article" and self.depth: self.depth -= 1
    def handle_data(self, data):
        if self.depth and not self.suppressed and data.strip(): self.parts.append(data.strip())


def fetch_detail(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return ("blocked", None, "仅允许不含凭据的 HTTP(S) 公网地址")
        host = p.hostname.lower(); port = p.port or (443 if p.scheme == "https" else 80)
        if host == "localhost" or host.endswith(".localhost"):
            return ("blocked", None, "本机及私有地址不可访问")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            return ("blocked", None, "地址包含非公网 IP")
        # Connect to the already validated address to avoid DNS re-resolution.
        sock = socket.create_connection(addresses[0][4], timeout=8)
        if p.scheme == "https": sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=8)
        conn.sock = sock
        path = p.path or "/"
        if p.query: path += "?" + p.query
        try:
            conn.request("GET", path, headers={"User-Agent":"Mozilla/5.0", "Accept":"text/html"})
            resp=conn.getresponse()
            if resp.status in (401, 403, 429): return ("blocked", None, f"HTTP {resp.status} 访问受限")
            if 300 <= resp.status < 400: return ("needs_source", None, f"HTTP {resp.status} 重定向未跟随，请提供最终公开报道链接")
            if resp.status != 200: return ("needs_source", None, f"HTTP {resp.status}")
            if "text/html" not in resp.getheader("Content-Type", "").lower(): return ("needs_source", None, "不是可解析的 HTML 报道")
            raw=resp.read(1_000_001)
            if len(raw)>1_000_000: return ("needs_source",None,"正文响应超过 1 MB")
            page=raw.decode("utf-8","replace")
            if any(s in page for s in ("环境异常", "安全验证", "请先登录", "验证码")):
                return ("blocked",None,"页面要求验证或登录")
            parser=ArticleText(); parser.feed(page); text="\n".join(parser.parts)
            if len(text)<100: return ("needs_source",None,"未取得可识别的 article 正文，可能是搜索、话题或脚本页")
            return ("obtained",text,None)
        finally: conn.close()
    except (ValueError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
        return ("needs_source",None,f"{type(exc).__name__}: {exc}")


def is_topic(url):
    p=urlsplit(url); host=(p.hostname or "").lower(); path=p.path.lower()
    return (host.endswith("toutiao.com") and "/trending/" in path or
            host.endswith("baidu.com") and path.startswith("/s") or
            host.endswith("weibo.com") or host.endswith("zhihu.com") and "/question/" in path or
            host.endswith("bilibili.com") and "/video/" in path)
