"""Toutiao browser adapter. All readbacks come from the creator backend.

The editor and list/detail URLs were observed in the creator backend and are
also documented by https://github.com/SuperWangYuan/toutiaohao-mcp-server.
No code or status mappings from that project are incorporated here.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import base64
from collections import Counter
import gzip
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

from lxml import html as lxml_html

from . import composition


_BASE = "https://mp.toutiao.com"
_EDITOR = _BASE + "/profile_v4/graphic/publish"
_STATEMENTS = ("取材网络", "引用站内", "个人观点，仅供参考", "引用AI", "虚构演绎，故事经历", "投资观点，仅供参考", "健康医疗分享，仅供参考")
_ALLOWED_STATEMENTS = {"取材网络", "引用AI"}


def _report_cleanup(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        self._cleanup_state.status = None
        result = method(self, *args, **kwargs)
        if self._cleanup_state.status == "needs_attention" and isinstance(result, dict):
            result["cleanup_status"] = "needs_attention"
            result["cleanup_message"] = "任务窗口清理未完全确认，请先检查浏览器资源"
        return result
    return run


class ToutiaoPublisher:
    def __init__(self, adapter):
        self.adapter = adapter
        self._cleanup_state = threading.local()

    def _eval(self, session, expression):
        result = json.loads(self.adapter._bsk("evaluate", "--json", "--session", session, expression))
        if not result.get("ok"):
            raise RuntimeError("头条页面读取或编辑失败")
        return result.get("value")

    def _wait_title_ref(self, session):
        deadline = time.monotonic() + 15
        while True:
            obs = self.adapter._bsk("observe", "--session", session)
            refs = re.findall(r'(@e\d+)\s+textbox\s+"请输入文章标题（2～30个字）"(?=\s|$)', obs)
            if len(refs) == 1:
                return refs[0]
            if len(refs) > 1:
                raise RuntimeError("头条文章标题控件不唯一")
            if time.monotonic() >= deadline:
                raise RuntimeError("等待头条文章标题控件就绪超时")
            time.sleep(0.5)

    @contextmanager
    def _session(self, account, remote=None):
        if account.get("platform") != "toutiao" or account.get("mode") != "browser":
            raise ValueError("头条目前需要使用已连接的浏览器账号")
        if account.get("status") != "connected" or not (account.get("identity") or {}).get("key"):
            raise ValueError("请先连接头条账号")
        session = None
        try:
            status = json.loads(self.adapter._bsk("status", "--json"))
            browsers = [b for b in status.get("browsers", []) if not b.get("unresponsive")]
            if len(browsers) != 1:
                raise RuntimeError("需要恰好一个已连接的浏览器")
            if status.get("sessions") or browsers[0].get("session_count", 0):
                raise RuntimeError("已有浏览器自动化会话，请等待现有任务结束")
            session = json.loads(self.adapter._bsk("session", "start", "--browser", browsers[0]["instance_id"], "--json"))["session_id"]
            identity = self.adapter._browser_identity(account, session)
            if identity.get("key") != account["identity"]["key"]:
                raise RuntimeError("当前头条账号与绑定账号不一致")
            if remote is not None and identity.get("key") != (remote.get("account_identity") or {}).get("key"):
                raise RuntimeError("当前头条账号与草稿账号不一致")
            yield session, identity
        finally:
            if session:
                try:
                    self.adapter._close_agent_session(session)
                    self._cleanup_state.status = "clean"
                except Exception:
                    # Cleanup is a separate resource state; it must not mask the
                    # first error or change a confirmed business outcome.
                    self._cleanup_state.status = "needs_attention"

    def _json_page(self, session, url):
        self.adapter._bsk("navigate", url, "--session", session)
        raw = self._eval(session, "document.body.innerText")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("code") != 0:
            raise RuntimeError("头条后台未返回可核验的数据")
        return data

    def _list(self, session, status="draft"):
        result = []
        expected_total = None
        for page in range(1, 11):
            data = self._json_page(session, f"{_BASE}/mp/agw/article/list/?page={page}&page_size=20&status={status}")
            payload = data.get("data")
            articles = payload.get("articles") if isinstance(payload, dict) else None
            if not isinstance(articles, list):
                raise RuntimeError("头条作品列表结构无法识别")
            if any(not isinstance(x, dict) for x in articles):
                raise RuntimeError("头条作品列表存在无法识别的记录")
            raw_total = payload.get('total', payload.get('count'))
            if isinstance(raw_total, bool) or not re.fullmatch(r'\d+', str(raw_total if raw_total is not None else '')):
                raise RuntimeError("头条作品列表未提供可核验的总数")
            total = int(raw_total)
            if expected_total is not None and total != expected_total:
                raise RuntimeError("头条作品列表分页总数发生变化")
            expected_total = total
            result.extend(articles)
            if len(result) > total:
                raise RuntimeError("头条作品列表数量超过平台返回的总数")
            has_more = payload.get('has_more')
            if has_more is not None and has_more not in (True, False, 0, 1):
                raise RuntimeError("头条作品分页状态无法识别")
            if has_more is not None and bool(has_more) != (len(result) < total):
                raise RuntimeError("头条作品分页状态与总数不一致")
            if len(result) == total:
                return result
            if len(articles) < 20:
                raise RuntimeError("头条作品列表短页与总数不一致，不能判定已穷尽")
        raise RuntimeError("头条草稿列表超过可核验范围")

    @staticmethod
    def _id(item):
        value = str(item.get("id") or "")
        return value if re.fullmatch(r"\d+", value) else ""

    def _read_editor(self, session, draft_id):
        if not re.fullmatch(r"\d+", str(draft_id)):
            raise ValueError("远端草稿 ID 无效")
        self.adapter._bsk("navigate", _EDITOR + "?pgc_id=" + str(draft_id), "--session", session)
        time.sleep(3)
        value = self._eval(session, """(() => {
            const title = document.querySelector('textarea[placeholder*=\"文章标题\"]');
            const body = document.querySelector('.ProseMirror[contenteditable=true]');
            const covers = [...document.querySelectorAll('.article-cover-images img')].map(x => x.currentSrc || x.src).filter(Boolean);
            return {title: title ? title.value : null, html: body ? body.innerHTML : null, covers,
                    page_id: new URL(location.href).searchParams.get('pgc_id')};
        })()""")
        if (not isinstance(value, dict) or not isinstance(value.get("title"), str)
                or not isinstance(value.get("html"), str) or value.get("page_id") != str(draft_id)):
            raise RuntimeError("头条草稿编辑页无法读取")
        covers = value.get("covers") or []
        if len(covers) != 1 or urlparse(covers[0]).scheme not in ("http", "https"):
            raise RuntimeError("头条草稿封面无法唯一识别")
        return {"title": value["title"], "html": value["html"], "cover_media_id": covers[0],
                **self._read_declarations(session)}

    @staticmethod
    def _desired_declarations(snapshot):
        statements = snapshot.get("work_statements")
        if (not isinstance(statements, list) or any(not isinstance(x, str) or x not in _ALLOWED_STATEMENTS for x in statements)
                or len(statements) != len(set(statements)) or snapshot.get("first_publish") is not False
                or snapshot.get("original") is not False):
            raise ValueError("请在系统审稿中明确作品声明；首发和原创当前只支持关闭")
        return [x for x in _STATEMENTS if x in statements]

    def _read_declarations(self, session):
        labels = list(_STATEMENTS) + ["头条首发", "发布得更多收益"]
        script = """(() => {
            const names = NAMES;
            const all = [...document.querySelectorAll('label')];
            return names.map(name => {
                const matches = all.filter(x => (x.innerText || x.textContent || '').trim() === name);
                const input = matches.length === 1 ? matches[0].querySelector('input') : null;
                return {name, count:matches.length, type:input?.type || null,
                        group:input?.name || '', checked:input ? input.checked : null};
            });
        })()""".replace("NAMES", json.dumps(labels, ensure_ascii=False))
        rows = self._eval(session, script)
        if (not isinstance(rows, list) or len(rows) != len(labels)
                or any(not isinstance(x, dict) or x.get("name") != name or x.get("count") != 1
                       or x.get("type") not in ("checkbox", "radio") or not isinstance(x.get("checked"), bool)
                       for x, name in zip(rows, labels))):
            raise RuntimeError("无法可靠读取头条作品声明和首发状态")
        original = self._eval(session, """(() => {
            const matches=[...document.querySelectorAll('label')].filter(x =>
              ['原创','声明原创','原创声明'].includes((x.innerText || x.textContent || '').trim()));
            if (!matches.length) return null;
            const input=matches.length===1 ? matches[0].querySelector('input') : null;
            return input && ['checkbox','radio'].includes(input.type) ? input.checked : 'unknown';
        })()""")
        if original is not None:
            raise RuntimeError("头条出现独立原创控件，请重新核验该渠道能力")
        return {"work_statements": [x["name"] for x in rows[:-2] if x["checked"]],
                "first_publish": rows[-2]["checked"], "publish_micro": rows[-1]["checked"],
                "original": original,
                "_choices": [{"name":x["name"],"type":x["type"],"group":x["group"]} for x in rows[:-2]]}

    @staticmethod
    def _check_statement_compatibility(state, desired):
        chosen = [x for x in state.get("_choices", []) if x.get("name") in desired and x.get("type") == "radio"]
        for i, left in enumerate(chosen):
            for right in chosen[i + 1:]:
                if not left.get("group") or not right.get("group") or left["group"] == right["group"]:
                    raise RuntimeError("头条作品声明可能互斥，请先在系统审稿选择平台支持的声明")

    def _set_declarations(self, session, desired):
        # Every action is preceded by a fresh observation, followed by a fresh
        # DOM read. Never repeatedly click a control whose effect is unknown.
        state = self._read_declarations(session)
        for name in (*_STATEMENTS, "头条首发", "发布得更多收益"):
            selected = state["first_publish"] if name == "头条首发" else (state["publish_micro"] if name == "发布得更多收益" else name in state["work_statements"])
            required = name in desired if name in _STATEMENTS else False
            if selected != required:
                obs = self.adapter._bsk("observe", "--session", session)
                ref = self.adapter._ref(obs, "button", name)
                self.adapter._bsk("click", ref, "--session", session)
                state = self._read_declarations(session)
                selected = state["first_publish"] if name == "头条首发" else (state["publish_micro"] if name == "发布得更多收益" else name in state["work_statements"])
                if selected != required:
                    raise RuntimeError("头条声明选择结果无法确认")
        if state["work_statements"] != desired or state["first_publish"] or state["publish_micro"] or state["original"] is True:
            raise RuntimeError("头条声明互斥或未完整生效")
        return state

    @staticmethod
    def _paste_expression(html, plain):
        # bsk has no HTML-paste command. The editor's ProseMirror paste handler
        # receives both clipboard MIME types through a browser ClipboardEvent.
        # Compress before passing argv on Windows; never truncate the article.
        payload = base64.b64encode(gzip.compress(json.dumps({"html":html, "plain":plain},
            ensure_ascii=False).encode("utf-8"))).decode("ascii")
        if len(payload) > 24000:
            raise ValueError("富文本超过浏览器命令传输上限，请缩减稿件或使用可分块的编辑器接口")
        return """(async () => {
          const e=document.querySelector('.ProseMirror[contenteditable=true]');
          if (!e || !window.DecompressionStream) return false;
          const bytes=Uint8Array.from(atob(PAYLOAD), c => c.charCodeAt(0));
          const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
          const data=JSON.parse(await new Response(stream).text());
          const transfer=new DataTransfer();
          transfer.setData('text/html', data.html);
          transfer.setData('text/plain', data.plain);
          e.focus();
          const range=document.createRange(); range.selectNodeContents(e);
          const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          const event=new ClipboardEvent('paste', {bubbles:true,cancelable:true,clipboardData:transfer});
          e.dispatchEvent(event);
          return event.defaultPrevented;
        })()""".replace("PAYLOAD", json.dumps(payload))

    @staticmethod
    def _prepare_body_images(html, snapshot, cover):
        root = lxml_html.fromstring(html)
        images = root.xpath('.//img')
        if not images:
            return html, [], {}
        assets = snapshot.get("body_images")
        if not isinstance(assets, dict):
            raise ValueError("正文图片缺少已授权的本地文件映射")
        srcs = [str(image.get("src") or "") for image in images]
        if len(set(srcs)) != len(srcs):
            raise ValueError("同一正文图片多次出现尚不能可靠映射，请使用独立图片")
        entries = []
        for image, src in zip(images, srcs):
            item = assets.get(src)
            if not re.fullmatch(r"/api/production/media/[^/?#\\]+", src) or not isinstance(item, dict):
                raise ValueError("正文图片必须使用本系统上传的本地素材")
            path = Path(str(item.get("path") or "")).resolve()
            if (path.parent != cover.resolve().parent or path.name != src.rsplit('/', 1)[-1]
                    or not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png")
                    or path.stat().st_size <= 0 or path.stat().st_size > 20 * 1024 * 1024):
                raise ValueError("正文图片路径、格式或大小不符合要求")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or "")) or digest != item["sha256"]:
                raise ValueError("正文图片内容已变化，请重新审稿")
            parent = image.getparent()
            if parent is None or parent.tag not in ("figure", "p") or len(parent.xpath('.//img')) != 1:
                raise ValueError("正文图片须为独立段落或单图图注，暂不支持嵌套图文")
            if parent.tag == "p" and (len(parent) != 1 or (parent.text or '').strip() or (image.tail or '').strip()):
                raise ValueError("暂不支持文字与图片混排在同一段落")
            if parent.tag == "figure":
                if image.getparent() is not parent or any(x.tag not in ('img', 'figcaption') for x in parent):
                    raise ValueError("暂不支持复杂图片布局")
                captions = [x for x in parent if x.tag == 'figcaption']
                if len(captions) > 1:
                    raise ValueError("每张正文图片最多一个图注")
                for caption in captions:
                    caption.tag = 'p'
            marker = 'CWIMAGE' + uuid.uuid4().hex + 'END'
            if marker in html:
                raise ValueError("正文图片占位标识冲突")
            replacement = lxml_html.Element('p') if parent.tag == 'figure' else None
            if replacement is not None:
                replacement.text = marker; replacement.tail = image.tail
                parent.replace(image, replacement)
            else:
                parent.remove(image); parent.text = marker
            entries.append({"src":src, "path":str(path), "sha256":digest, "marker":marker})
        return lxml_html.tostring(root, encoding='unicode', method='html'), entries, {}

    @staticmethod
    def _mapped_html(html, image_map):
        root = lxml_html.fromstring(html)
        for image in root.xpath('.//img'):
            src = image.get('src') or ''
            if src not in image_map:
                raise ValueError("正文图片缺少平台媒体映射")
            image.set('src', image_map[src])
        return lxml_html.tostring(root, encoding='unicode', method='html')

    def _editor_images(self, session):
        values = self._eval(session, "[...document.querySelectorAll('.ProseMirror[contenteditable=true] img')].map(x => x.currentSrc || x.src).filter(Boolean)")
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            raise RuntimeError("无法读取正文图片")
        return values

    @staticmethod
    def _redact_body_html(value):
        root = lxml_html.fragment_fromstring(value, create_parent='div')
        for node in list(root.iterdescendants()):
            if not isinstance(node.tag, str):
                node.drop_tree(); continue
            if node.tag.lower() in ('script', 'style', 'iframe', 'object', 'embed', 'form', 'input'):
                node.drop_tree(); continue
            for key in list(node.attrib):
                if key.lower().startswith('on') or re.search(r'(?i)token|cookie|secret|authorization|session|ticket|signature', key):
                    del node.attrib[key]; continue
                if key.lower() in ('href', 'src'):
                    parsed = urlsplit(node.attrib[key])
                    if parsed.query or parsed.fragment or parsed.username or parsed.password:
                        host = parsed.netloc.rsplit('@', 1)[-1]
                        node.attrib[key] = urlunsplit((parsed.scheme, host, parsed.path, '', ''))
                if key.lower() == 'style' and re.search(r'(?i)url\s*\(', node.attrib[key]):
                    del node.attrib[key]
        return (root.text or '') + ''.join(lxml_html.tostring(node, encoding='unicode', method='html') for node in root)

    def _capture_body_evidence(self, session, operation_id, phase):
        if not re.fullmatch(r'[0-9a-f]{32}', str(operation_id or '')):
            raise RuntimeError("缺少可关联的同步操作 ID，不能保存正文证据")
        result = self._eval(session, """(() => {
          const body=document.querySelector('.ProseMirror[contenteditable=true]');
          if(!body) return null;
          const styles=[], walker=document.createTreeWalker(body,NodeFilter.SHOW_TEXT);
          let node, offset=0;
          while((node=walker.nextNode())) {
            if(!node.nodeValue.trim()) continue;
            const p=node.parentElement, s=getComputedStyle(p), n=node.nodeValue.length;
            const b=p.closest('p,h1,h2,h3,h4,h5,h6,li,blockquote,figcaption') || p;
            const bs=getComputedStyle(b);
            styles.push({offset, length:n, tag:p.tagName.toLowerCase(),
              font_family:s.fontFamily, font_size:s.fontSize, font_weight:s.fontWeight,
              font_style:s.fontStyle, line_height:s.lineHeight, color:s.color,
              background_color:s.backgroundColor, text_align:s.textAlign,
              text_decoration:s.textDecorationLine, letter_spacing:s.letterSpacing,
              block_tag:b.tagName.toLowerCase(), block_text_align:bs.textAlign,
              block_margin_top:bs.marginTop, block_margin_bottom:bs.marginBottom,
              block_padding_top:bs.paddingTop, block_padding_bottom:bs.paddingBottom});
            offset+=n;
          }
          return {html:body.innerHTML, styles};
        })()""")
        if not isinstance(result, dict) or not isinstance(result.get('html'), str) or not isinstance(result.get('styles'), list):
            raise RuntimeError("无法采集编辑器正文和计算样式")
        evidence_dir = self.adapter.root.parent / 'production' / 'evidence'
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path = evidence_dir / ('toutiao-' + operation_id + '-' + phase + '.json')
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps({'operation_id': operation_id, 'phase': phase,
            'body_html':self._redact_body_html(result['html']), 'computed_styles':result['styles'],
            'redactions':'只保存编辑器正文；移除脚本、事件及敏感属性，URL 查询参数和片段'},
            ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
        return result['html'], str(path)

    @staticmethod
    def _readback_diagnostic(expected_html, observed_html, phase):
        """Report structure/style differences without recording prose or URLs."""
        if not isinstance(observed_html, str):
            return {"phase": phase, "readback": "not_text"}
        expected = composition.semantic_html(expected_html)
        observed = composition.semantic_html(observed_html)
        result = {"phase": phase,
                  "paragraph_count": [len(expected['paragraphs']), len(observed['paragraphs'])],
                  "paragraphs_match": expected['paragraphs'] == observed['paragraphs'],
                  "links_match": expected['links'] == observed['links'],
                  "images_match": expected['images'] == observed['images'],
                  "tables_match": expected['table_events'] == observed['table_events'],
                  "unsafe_count": len(observed['unsafe'])}
        left = composition.Presentation(); left.feed(expected_html); left.close()
        right = composition.Presentation(); right.feed(observed_html); right.close()
        result['text_character_count'] = [len(left.characters), len(right.characters)]
        aligned = len(left.characters) == len(right.characters) and all(a[0] == b[0] for a, b in zip(left.characters, right.characters))
        result['text_characters_match'] = aligned
        if aligned:
            missing_css = set(); missing_marks = set()
            for (_, css, marks), (_, found_css, found_marks) in zip(left.characters, right.characters):
                missing_css.update(key for key, value in css if (key, value) not in found_css)
                missing_marks.update(marks - found_marks)
            result['missing_or_changed_style_properties'] = sorted(missing_css)
            result['missing_marks'] = sorted(missing_marks)
        return result

    def _insert_body_image(self, session, entry):
        # Only the file checked against this draft's snapshot is disclosed.
        if hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError("正文图片在上传前已发生变化")
        before = self._editor_images(session)
        marker = json.dumps(entry['marker'])
        selected = self._eval(session, """(() => {
          const e=document.querySelector('.ProseMirror[contenteditable=true]');
          const nodes=e?[...e.querySelectorAll('p')].filter(x => x.textContent.trim() === MARKER && !x.querySelector('img')):[];
          if(nodes.length!==1)return false;
          e.focus(); const r=document.createRange(); r.selectNodeContents(nodes[0]);
          const s=window.getSelection();s.removeAllRanges();s.addRange(r); return true;
        })()""".replace('MARKER', marker))
        if selected is not True:
            raise RuntimeError("正文图片位置无法唯一定位")
        self.adapter._bsk('press', 'Backspace', '--session', session)
        if self._eval(session, "document.querySelector('.ProseMirror[contenteditable=true]')?.innerText.includes(" + marker + ")") is not False:
            raise RuntimeError("正文图片位置未能清除占位内容")
        self.adapter._bsk('observe', '--session', session)
        count = self._eval(session, "document.querySelectorAll('.syl-toolbar-tool.image button').length")
        if count != 1:
            raise RuntimeError("无法唯一识别正文插图工具")
        self.adapter._bsk('click', '.syl-toolbar-tool.image button', '--session', session)
        obs = self.adapter._bsk('observe', '--session', session)
        match = re.search(r'(@e\d+)\s+button\s+"本地上传[^\"]*"', obs)
        if not match:
            raise RuntimeError("无法识别正文图片的本地上传入口")
        self.adapter._bsk('upload', match.group(1), '--file', entry['path'], '--session', session)
        self._finish_cover_dialog(session)
        time.sleep(2)
        after = self._editor_images(session)
        added = list((Counter(after) - Counter(before)).elements())
        if len(after) != len(before) + 1 or len(added) != 1:
            raise RuntimeError("正文图片上传后的平台资源无法唯一识别")
        parsed = urlparse(added[0])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.hostname in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError("正文图片没有返回可核验的平台资源地址")
        return added[0]

    def _cover_source(self, session):
        covers = self._eval(session, "[...document.querySelectorAll('.article-cover-images img')].map(x => x.currentSrc || x.src).filter(Boolean)")
        if not isinstance(covers, list) or len(covers) != 1 or urlparse(covers[0]).scheme not in ("http", "https"):
            raise RuntimeError("上传的头条封面未显示唯一平台图片")
        return covers[0]

    def _submission_receipt(self, session):
        # Do not mistake words in an article body or navigation for a receipt.
        values = self._eval(session, """[...document.querySelectorAll('[role=alert],[role=status],.byte-message-notice-content,.byte-toast-content')]
          .filter(x => {const r=x.getBoundingClientRect(), s=getComputedStyle(x);
                        return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';})
          .map(x => (x.innerText || '').trim())""")
        return isinstance(values, list) and any(isinstance(x, str) and re.fullmatch(r"(?:文章)?(?:提交成功|发布成功)[！!。.]?", x) for x in values)

    def _finish_cover_dialog(self, session):
        for _ in range(3):
            time.sleep(2)
            obs = self.adapter._bsk("observe", "--session", session)
            if "L1 modal" not in obs:
                return
            modal = obs.split("L2 ", 1)[0]
            if (not re.search(r"封面|裁剪|图片", modal)
                    or re.search(r"验证码|安全验证|扫码验证|人机验证|发布", modal)):
                raise RuntimeError("封面上传后出现无法识别的页面")
            refs = re.findall(r'(@e\d+)\s+button\s+"(?:确定|完成|确认|使用)"(?:\s|$)', modal)
            if len(refs) != 1:
                raise RuntimeError("封面裁剪或选择尚未出现唯一确认控件")
            self.adapter._bsk("click", refs[0], "--session", session)
        time.sleep(2)
        if "L1 modal" not in self.adapter._bsk("observe", "--session", session):
            return
        raise RuntimeError("封面确认步骤超过可识别范围")

    def _upload_cover(self, session, cover, previous=None):
        if previous is None:
            self.adapter._bsk('click', '.article-cover-add', '--session', session)
        else:
            # The filled slot may either open a chooser directly or reveal the
            # image dialog. Do not retry an upload with an unknown effect.
            self.adapter._bsk('observe', '--session', session)
            if self._eval(session, "document.querySelectorAll('.article-cover-images .article-cover-img-wrap').length") != 1:
                raise RuntimeError("已有封面替换入口不唯一")
            self.adapter._bsk('hover', '.article-cover-images .article-cover-img-wrap', '--session', session)
            try:
                self.adapter._bsk('upload', '.article-cover-images .article-cover-img-wrap', '--file', str(cover.resolve()), '--session', session)
            except Exception as exc:
                if getattr(exc, 'reason', '') != 'file_input_not_activated' or getattr(exc, 'effect_state', '') != 'none':
                    raise
                obs = self.adapter._bsk('observe', '--session', session)
                match = re.search(r'(@e\d+)\s+button\s+"本地上传[^\"]*"', obs)
                if not match or 'L1 modal' not in obs:
                    raise RuntimeError("未识别已有封面的本地替换弹窗")
                self.adapter._bsk('upload', match.group(1), '--file', str(cover.resolve()), '--session', session)
            self._finish_cover_dialog(session)
            value = self._cover_source(session)
            if value == previous:
                raise RuntimeError("平台未显示不同的新封面，替换结果待核验")
            return value
        obs = self.adapter._bsk('observe', '--session', session)
        match = re.search(r'(@e\d+)\s+button\s+"本地上传[^\"]*"', obs)
        if not match:
            raise RuntimeError("未识别头条封面本地上传入口")
        self.adapter._bsk('upload', match.group(1), '--file', str(cover.resolve()), '--session', session)
        self._finish_cover_dialog(session)
        return self._cover_source(session)

    @_report_cleanup
    def save_draft(self, account_id, snapshot, existing_remote=None):
        title = str(snapshot.get("title") or "").strip()
        html = str(snapshot.get("html") or "")
        cover = Path(str(snapshot.get("cover_path") or ""))
        operation_id = str(snapshot.get("operation_id") or "")
        if not re.fullmatch(r'[0-9a-f]{32}', operation_id):
            return {"status":"needs_attention", "message":"缺少同步操作 ID，无法记录真实正文证据"}
        if existing_remote:
            if (not isinstance(existing_remote, dict) or not re.fullmatch(r"\d+", str(existing_remote.get("draft_id") or ""))
                    or not existing_remote.get("fingerprint") or not existing_remote.get("cover_media_id")):
                return {"status": "needs_attention", "message": "已有草稿缺少可核验的 ID、指纹或封面"}
        try:
            desired = self._desired_declarations(snapshot)
        except ValueError as exc:
            return {"status": "needs_attention", "message": str(exc)}
        if not 2 <= composition.title_units(title) <= 30 or not html.strip():
            return {"status": "needs_attention", "message": "头条标题须为 2–30 字且正文不能为空，请编辑稿件"}
        if not cover.is_file() or cover.suffix.lower() not in (".jpg", ".jpeg", ".png") or cover.stat().st_size > 20 * 1024 * 1024:
            return {"status": "needs_attention", "message": "头条封面需要不超过 20MB 的 JPEG 或 PNG"}
        cover_hash = hashlib.sha256(cover.read_bytes()).hexdigest()
        if cover_hash != snapshot.get("cover_sha256"):
            return {"status": "needs_attention", "message": "封面文件已变化，请重新审稿"}
        semantic = composition.semantic_html(html)
        if semantic["unsafe"]:
            return {"status": "needs_attention", "message": "正文含不支持的内容"}
        try:
            prepared_html, body_images, image_map = self._prepare_body_images(html, snapshot, cover)
            prepared_semantic = composition.semantic_html(prepared_html)
            paste_expression = self._paste_expression(prepared_html, "\n\n".join(prepared_semantic["paragraphs"]))
        except (ValueError, OSError) as exc:
            return {"status": "needs_attention", "message": str(exc)}
        started = False
        draft_id = ""
        identity = None
        uploaded_cover = None
        comparison_detail = None
        evidence_paths = []
        try:
            with self.adapter._lock(account_id), self.adapter._browser_lock:
                account = self.adapter._read(account_id)
                with self._session(account, existing_remote) as (session, identity):
                    before_items = self._list(session)
                    if any(not self._id(x) or not isinstance(x.get('title'), str) for x in before_items):
                        raise RuntimeError("草稿列表存在无法核对 ID 或标题的作品，不能安全新建")
                    before = {self._id(x) for x in before_items}
                    if len(before) != len(before_items):
                        raise RuntimeError("草稿列表存在重复 ID，不能安全判定是否已穷尽")
                    matching_title = [x for x in before_items if composition.normalize_text(x['title']) == composition.normalize_text(title)]
                    if len(matching_title) > 1:
                        raise RuntimeError("存在多篇同标题草稿，不能自动判断或新建")
                    if matching_title and not existing_remote:
                        draft_id = self._id(matching_title[0])
                        candidate = self._read_editor(session, draft_id)
                        candidate['html'], evidence_path = self._capture_body_evidence(session, operation_id, 'candidate')
                        evidence_paths.append(evidence_path)
                        candidate_semantic = composition.semantic_html(candidate['html'])
                        if (composition.normalize_text(candidate['title']) != composition.normalize_text(title)
                                or any(candidate_semantic[k] != semantic[k] for k in ('paragraphs', 'links', 'table_events'))
                                or [x['position'] for x in candidate_semantic['images']] != [x['position'] for x in semantic['images']]):
                            raise RuntimeError("发现同标题草稿但正文或图片位置不一致，不能自动复用或新建")
                        # A CDN URL cannot establish which local bytes were uploaded.
                        # Reuse needs the previous confirmed upload mapping, which is
                        # absent here even when text and positions happen to match.
                        raise RuntimeError("发现同标题正文相符的草稿，但缺少可信封面或正文图片上传映射，不能自动复用或新建")
                    if existing_remote:
                        draft_id = str(existing_remote["draft_id"])
                        if matching_title and self._id(matching_title[0]) != draft_id:
                            raise RuntimeError("另一篇草稿使用相同标题，不能自动更新或新建")
                        if sum(self._id(x) == draft_id for x in before_items) != 1:
                            raise RuntimeError("草稿箱中没有唯一匹配的待更新作品")
                        previous = self._read_editor(session, draft_id)
                        previous['html'], evidence_path = self._capture_body_evidence(session, operation_id, 'candidate')
                        evidence_paths.append(evidence_path)
                        old_actual = {"status":"ok", "draft_id":draft_id, "account_identity":identity,
                                      "title":previous["title"], "html":previous["html"],
                                      "cover_media_id":previous["cover_media_id"], "summary_supported":False,
                                      "work_statements":previous["work_statements"], "first_publish":previous["first_publish"],
                                      "publish_micro":previous["publish_micro"], "original":previous["original"]}
                        if (composition.remote_fingerprint(old_actual) != existing_remote["fingerprint"]
                                or previous["cover_media_id"] != existing_remote["cover_media_id"]):
                            raise RuntimeError("已有草稿已发生外部变化，请先重新核验")
                        old_images = existing_remote.get('image_map') or {}
                        mapped_source = (self._mapped_html(html, old_images) if all(x['src'] in old_images for x in semantic['images']) else None) if semantic['images'] else html
                        if (mapped_source is not None and composition.normalize_text(previous['title']) == composition.normalize_text(title)
                                and composition.semantic_html(previous['html']) == composition.semantic_html(mapped_source)
                                and composition.presentation_preserved(mapped_source, previous['html'])
                                and existing_remote.get('cover_sha256') == cover_hash
                                and previous['work_statements'] == desired and not previous['first_publish']
                                and not previous['publish_micro'] and previous['original'] is None):
                            return {"status":"saved", "draft_id":draft_id, "account_identity":identity,
                                    "cover_media_id":previous['cover_media_id'], "cover_sha256":cover_hash,
                                    "image_map":existing_remote.get('image_map') or {},
                                    "evidence":"同账号唯一已有草稿的正文、格式、封面映射和声明重新核验一致，已复用",
                                    "evidence_paths":evidence_paths}
                    else:
                        self.adapter._bsk("navigate", _EDITOR, "--session", session)
                    title_ref = self._wait_title_ref(session)
                    editor = self._eval(session, """(() => {
                        const all = document.querySelectorAll('.ProseMirror[contenteditable=true]');
                        const copy = all[0]?.cloneNode(true);
                        copy?.querySelectorAll('.syl-placeholder.ProseMirror-widget').forEach(x => x.remove());
                        return {count:all.length, text:copy?.innerText || copy?.textContent || '',
                          media:copy?.querySelectorAll('img,video,iframe,object').length || 0,
                          title:document.querySelector('textarea[placeholder*=\"文章标题\"]')?.value || ''};
                    })()""")
                    if not isinstance(editor, dict) or editor.get("count") != 1:
                        raise RuntimeError("未能唯一识别头条编辑器")
                    if not existing_remote and (editor.get("text", "").strip() or editor.get("media") or editor.get("title", "").strip()):
                        raise RuntimeError("编辑器已有内容，不能覆盖")
                    self._check_statement_compatibility(self._read_declarations(session), desired)
                    title_ref = self._wait_title_ref(session)
                    started = True
                    try:
                        self.adapter._bsk("fill", title_ref, "--value", title, "--session", session)
                    except Exception:
                        try:
                            _body, evidence_path = self._capture_body_evidence(session, operation_id, 'title-error')
                            evidence_paths.append(evidence_path)
                        except Exception:
                            pass
                        raise
                    try:
                        paste_received = self._eval(session, paste_expression)
                    except Exception:
                        try:
                            _body, evidence_path = self._capture_body_evidence(session, operation_id, 'paste-error')
                            evidence_paths.append(evidence_path)
                        except Exception:
                            pass
                        raise
                    time.sleep(1)
                    local_readback, evidence_path = self._capture_body_evidence(session, operation_id, 'initial-paste')
                    evidence_paths.append(evidence_path)
                    if paste_received is not True:
                        raise RuntimeError("编辑器未确认接收 text/html 粘贴事件")
                    if (not isinstance(local_readback, str) or composition.semantic_html(local_readback) != prepared_semantic
                            or not composition.presentation_preserved(prepared_html, local_readback)):
                        comparison_detail = self._readback_diagnostic(prepared_html, local_readback, "initial_paste")
                        raise RuntimeError("编辑器未完整保留富文本或排版，已停止后续封面和发布步骤")
                    for entry in body_images:
                        image_map[entry['src']] = self._insert_body_image(session, entry)
                    expected_html = self._mapped_html(html, image_map) if body_images else html
                    expected_semantic = composition.semantic_html(expected_html)
                    local_readback, evidence_path = self._capture_body_evidence(session, operation_id, 'body-images')
                    evidence_paths.append(evidence_path)
                    if (not isinstance(local_readback, str) or composition.semantic_html(local_readback) != expected_semantic
                            or not composition.presentation_preserved(expected_html, local_readback)):
                        comparison_detail = self._readback_diagnostic(expected_html, local_readback, "body_images")
                        raise RuntimeError("正文图片、图注、顺序或排版未能完整回读")
                    self._set_declarations(session, desired)
                    if hashlib.sha256(cover.read_bytes()).hexdigest() != cover_hash:
                        raise RuntimeError("封面在上传前已变化")
                    filled = self._eval(session, "document.querySelectorAll('.article-cover-images .article-cover-img-wrap').length")
                    if existing_remote and existing_remote.get("cover_sha256") == cover_hash:
                        uploaded_cover = self._cover_source(session)
                        if uploaded_cover != previous["cover_media_id"]:
                            raise RuntimeError("原有封面被平台改变，请核验")
                    elif filled:
                        current_cover = self._cover_source(session)
                        same_body = [x for x in body_images if x['sha256'] == cover_hash and image_map.get(x['src']) == current_cover]
                        verified_auto_cover = len(same_body) == 1 and (not existing_remote or current_cover != previous["cover_media_id"])
                        uploaded_cover = current_cover if verified_auto_cover else self._upload_cover(session, cover, current_cover)
                    else:
                        uploaded_cover = self._upload_cover(session, cover)
                    time.sleep(10)
                    after = self._list(session)
                    matches = [x for x in after if self._id(x) and (self._id(x) == draft_id if existing_remote else self._id(x) not in before) and composition.normalize_text(x.get("title")) == composition.normalize_text(title)]
                    if len(matches) != 1:
                        raise RuntimeError("无法唯一确认新草稿 ID")
                    draft_id = self._id(matches[0])
                    actual = self._read_editor(session, draft_id)
                    _body, evidence_path = self._capture_body_evidence(session, operation_id, 'saved-draft')
                    evidence_paths.append(evidence_path)
                    actual['html'] = _body
                    if composition.normalize_text(actual["title"]) != composition.normalize_text(title) or composition.semantic_html(actual["html"]) != expected_semantic:
                        comparison_detail = self._readback_diagnostic(expected_html, actual["html"], "saved_draft")
                        raise RuntimeError("保存后的标题或正文与稿件不一致")
                    if not composition.presentation_preserved(expected_html, actual["html"]):
                        comparison_detail = self._readback_diagnostic(expected_html, actual["html"], "saved_draft")
                        raise RuntimeError("头条回读缺少稿件的排版样式或结构，不能确认排版已同步")
                    if actual["work_statements"] != desired or actual["first_publish"] or actual["publish_micro"] or actual["original"] is True:
                        raise RuntimeError("保存后的作品声明或首发状态与稿件不一致")
                    if actual["cover_media_id"] != uploaded_cover:
                        raise RuntimeError("重新打开的封面与本次上传显示的图片不一致")
                    return {"status": "saved", "draft_id": draft_id, "account_identity": identity,
                            "cover_media_id": actual["cover_media_id"], "cover_sha256": cover_hash,
                            "image_map": image_map, "evidence": "头条草稿列表及重新打开编辑页核对",
                            "evidence_paths": evidence_paths}
        except Exception as exc:
            result = {"status": "unknown" if started else "needs_attention",
                      "message": ("头条草稿写入结果未完全确认，请先核验，禁止重复创建：" if started else "无法开始头条草稿同步：") + str(exc)}
            if draft_id:
                result["draft_id"] = draft_id
            if identity:
                result["account_identity"] = identity
            if draft_id and uploaded_cover:
                result.update(cover_media_id=uploaded_cover, cover_sha256=cover_hash, image_map=image_map)
            if comparison_detail:
                result['diagnostic'] = comparison_detail
                result['message'] += "；脱敏差异=" + json.dumps(comparison_detail, ensure_ascii=False, separators=(',', ':'))
            if evidence_paths:
                result['evidence_paths'] = evidence_paths
            return result

    @_report_cleanup
    def inspect_draft(self, account_id, remote):
        try:
            with self.adapter._lock(account_id), self.adapter._browser_lock:
                account = self.adapter._read(account_id)
                with self._session(account, remote) as (session, identity):
                    draft_id = str(remote.get("draft_id") or "")
                    articles = self._list(session)
                    if sum(self._id(x) == draft_id for x in articles) != 1:
                        raise RuntimeError("草稿列表没有唯一对应作品")
                    actual = self._read_editor(session, draft_id)
                    return {"status": "ok", "draft_id": draft_id, "account_identity": identity,
                            "title": actual["title"], "html": actual["html"],
                            "cover_media_id": actual["cover_media_id"], "summary_supported": False,
                            "work_statements": actual["work_statements"], "first_publish": actual["first_publish"],
                            "publish_micro": actual["publish_micro"], "original": actual["original"],
                            "evidence": "重新打开头条草稿编辑页读取标题、正文和封面"}
        except Exception as exc:
            return {"status": "needs_attention", "message": "头条草稿回读失败：" + str(exc)}

    @_report_cleanup
    def publish(self, account_id, remote, snapshot, approval, operation_id):
        started = False
        phase = "preflight"
        try:
            desired = self._desired_declarations(snapshot)
            if (not isinstance(remote, dict) or not isinstance(approval, dict) or not operation_id
                    or snapshot.get("account_id") != account_id or snapshot.get("platform") != "toutiao"
                    or approval.get("account_id") != account_id or not approval.get("approved_at")
                    or not isinstance(snapshot.get("revision"), int) or approval.get("revision") != snapshot["revision"]
                    or not snapshot.get("content_hash") or approval.get("content_hash") != snapshot["content_hash"]
                    or remote.get("verified_hash") != snapshot["content_hash"]
                    or remote.get("cover_sha256") != snapshot.get("cover_sha256")):
                raise ValueError("发布确认与当前稿件或账号不一致")
            with self.adapter._lock(account_id), self.adapter._browser_lock:
                account = self.adapter._read(account_id)
                with self._session(account, remote) as (session, identity):
                    draft_id = str(remote.get("draft_id") or "")
                    if sum(self._id(x) == draft_id for x in self._list(session)) != 1:
                        raise RuntimeError("未能在草稿箱唯一定位已确认作品")
                    editor = self._read_editor(session, draft_id)
                    actual = {"status":"ok", "draft_id":draft_id, "account_identity":identity,
                              "title":editor["title"], "html":editor["html"],
                              "cover_media_id":editor["cover_media_id"], "summary_supported":False,
                              "work_statements":editor["work_statements"], "first_publish":editor["first_publish"],
                              "publish_micro":editor["publish_micro"], "original":editor["original"]}
                    source_html = snapshot.get("html") or ""
                    mapped_html = self._mapped_html(source_html, remote.get("image_map") or {}) if composition.semantic_html(source_html)['images'] else source_html
                    if (approval.get("draft_id") != draft_id or approval.get("account_identity") != identity["key"]
                            or approval.get("remote_fingerprint") != composition.remote_fingerprint(actual)
                            or composition.normalize_text(editor["title"]) != composition.normalize_text(snapshot.get("title"))
                            or composition.semantic_html(editor["html"]) != composition.semantic_html(mapped_html)
                            or not composition.presentation_preserved(mapped_html, editor["html"])
                            or editor["cover_media_id"] != remote.get("cover_media_id")
                            or editor["work_statements"] != desired or editor["first_publish"] or editor["publish_micro"] or editor["original"] is True):
                        raise RuntimeError("发布前回读与已确认版本不一致")
                    obs = self.adapter._bsk("observe", "--session", session)
                    if "L1 modal" in obs or re.search(r"验证码|安全验证|扫码验证|人机验证", obs):
                        raise RuntimeError("发布前出现平台验证或未知弹窗")
                    refs = re.findall(r'(@e\d+)\s+button\s+"预览并发布"(?:\s|$)', obs)
                    if len(refs) != 1:
                        raise RuntimeError("未能唯一识别头条预览并发布入口")
                    # A command error after dispatch does not establish whether the
                    # platform received the click; from this point never retry.
                    started = True
                    phase = "preview_requested"
                    self.adapter._bsk("click", refs[0], "--session", session)
                    time.sleep(2)
                    obs = self.adapter._bsk("observe", "--session", session)
                    if re.search(r"验证码|安全验证|扫码验证|人机验证", obs):
                        return {"status":"unknown", "operation_id":operation_id, "draft_id":draft_id,
                                "phase":phase, "message":"平台要求验证；发布结果待核验，请勿重复提交"}
                    if self._submission_receipt(session):
                        return {"status":"accepted", "operation_id":operation_id, "draft_id":draft_id,
                                "evidence":"头条页面显示提交或审核回执；最终发布状态待核验"}
                    # Only an explicit publish button in a preview modal whose
                    # title matches the approved draft is a confirmation target.
                    modal = obs.split("L2 ", 1)[0] if "L1 modal" in obs else ""
                    confirm = re.findall(r'(@e\d+)\s+button\s+"(?:确认发布|发布)"(?:\s|$)', modal)
                    if not modal or "预览" not in modal or snapshot["title"] not in modal or len(confirm) != 1:
                        return {"status":"unknown", "operation_id":operation_id, "draft_id":draft_id,
                                "phase":phase, "message":"平台确认页面无法唯一识别；请核验状态，勿重复提交"}
                    phase = "confirmation_requested"
                    self.adapter._bsk("click", confirm[0], "--session", session)
                    for _ in range(4):
                        time.sleep(2)
                        obs = self.adapter._bsk("observe", "--session", session)
                        if self._submission_receipt(session):
                            return {"status":"accepted", "operation_id":operation_id, "draft_id":draft_id,
                                    "evidence":"头条页面显示提交或审核回执；最终发布状态待核验"}
                        if re.search(r"验证码|安全验证|扫码验证|人机验证", obs):
                            break
                    return {"status":"unknown", "operation_id":operation_id, "draft_id":draft_id,
                            "phase":phase, "message":"未收到明确提交回执，请核验状态，勿重复提交"}
        except Exception as exc:
            return {"status":"unknown" if started else "needs_attention", "operation_id":operation_id,
                    "draft_id":str((remote or {}).get("draft_id") or ""), "phase":phase,
                    "message": ("发布结果待核验，请勿重复提交：" if started else "发布前检查未通过：") + str(exc)}

    @_report_cleanup
    def verify(self, account_id, remote, operation_id=None):
        if not isinstance(remote, dict) or not re.fullmatch(r"\d+", str(remote.get("draft_id") or "")):
            return {"status": "unknown", "message": "缺少可信的头条作品 ID"}
        try:
            with self.adapter._lock(account_id), self.adapter._browser_lock:
                account = self.adapter._read(account_id)
                with self._session(account, remote) as (session, _identity):
                    draft_id = str(remote["draft_id"])
                    matches = [x for x in self._list(session, "all") if self._id(x) == draft_id]
                    if len(matches) != 1:
                        return {"status": "unknown", "message": "没有找到唯一对应的头条作品"}
                    observed = str(matches[0].get("status") or "").strip()
                    mapped = {"草稿": "draft", "草稿箱": "draft", "已提交": "submitted", "审核中": "reviewing", "已发布": "published", "未通过": "rejected"}.get(observed)
                    if not mapped:
                        return {"status": "unknown", "message": "头条返回的作品状态尚无法可靠识别"}
                    result = {"status": mapped, "evidence": "头条作品列表状态：" + observed}
                    url = matches[0].get("article_url")
                    if isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).hostname in ("www.toutiao.com", "toutiao.com"):
                        result["url"] = url
                    return result
        except Exception:
            return {"status": "unknown", "message": "头条作品状态暂时无法读取"}
