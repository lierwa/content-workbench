"""Sanitize rich text and render platform HTML with local doocs/md theme assets."""
import html
import logging
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from lxml import html as lhtml

import nh3
import cssutils
from markdown_it import MarkdownIt
from premailer import transform


_THEMES = Path(__file__).resolve().parent.parent / 'vendor' / 'doocs-md'
_LOCAL_MEDIA = re.compile(r'^/api/production/media/[0-9a-f]{32}-(?:[0-9a-f]{24}|[0-9a-f]{32})\.(?:png|jpg|jpeg)$')
_TAGS = {'section', 'div', 'span', 'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote',
         'strong', 'b', 'em', 'i', 'u', 's', 'del', 'sub', 'sup', 'mark', 'a', 'img', 'figure',
         'figcaption', 'ul', 'ol', 'li', 'pre', 'code', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td'}
_INLINE_MARK = re.compile(r'</?(?:u|strong|b|em|i|s|del|sub|sup|mark)>', re.I)
_VISIBLE_TAG = re.compile(r'</?[a-z][\w:-]*(?:\s+[^<>]*)?\s*/?>|<!--[^>]*-->', re.I)
_MARKDOWN = re.compile(r'(?<!\d)\*\*(?=\S)|(?<=\S)\*\*(?!\d)|(?<!\w)__(?=\S)|(?<=\S)__(?!\w)|~~(?=\S)|'
                       r'\x60[^\x60\n]+\x60|\x60{3}|!\[[^\]\n]*\]|\[[^\]\n]+\]\([^)]+\)|^\s{0,3}(?:#{1,6} |>\s|[-*]\s+\[[ x]\])', re.M)
_EDITOR_NOTE = re.compile(
    r'(?:审稿(?:意见|备注|说明)|(?:编辑|编校|写作)(?:备注|提示|批注)|返修要求)\s*[：:]|'
    r'[【\[]\s*(?:(?:图注|图片说明|标题|来源)?待(?:补充|核实|替换|补)|编辑备注)(?:[：:][^】\]\n]*)?\s*[】\]]')
_BLOCKS = {'div','section','p','br','li','td','th','blockquote','figure','figcaption',
           'h1','h2','h3','h4','h5','h6','ul','ol','table','hr'}
_PROSE_MARKDOWN = MarkdownIt('commonmark', {'html': False}).enable('strikethrough')
_ATTRS = {'*': {'style'}, 'a': {'href', 'title'}, 'img': {'src', 'alt', 'title', 'width', 'height'},
          'ol': {'start'}, 'th': {'colspan', 'rowspan'}, 'td': {'colspan', 'rowspan'}}
_STYLE_PROPERTIES = {'font-family', 'font-size', 'font-weight', 'font-style', 'line-height',
    'color', 'background-color', 'text-align', 'text-decoration', 'text-decoration-line',
    'letter-spacing', 'word-spacing', 'text-indent', 'vertical-align', 'white-space',
    'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
    'border', 'border-top', 'border-right', 'border-bottom', 'border-left',
    'border-color', 'border-style', 'border-width', 'border-radius',
    'width', 'height', 'max-width', 'min-width', 'display', 'list-style-type'}


def _safe_style(value):
    """Keep only simple presentation declarations; no CSS functions with effects."""
    # Escapes, comments and at-rules complicate validation and are unnecessary
    # for the supported editor formatting controls.
    if len(value) > 4096 or any(s in value for s in ('\\', '/*', '*/', '@', '<', '>')):
        return None
    result = []
    for prop in cssutils.parseStyle(value, validate=False):
        name, val = prop.name.lower(), prop.value.strip()
        if name not in _STYLE_PROPERTIES or not val or len(val) > 200 or prop.priority:
            continue
        if not re.fullmatch(r"[\w\s.,#%'\"()\-]+", val, flags=re.UNICODE):
            continue
        # Only numeric rgb()/rgba() colors are accepted as functions.
        remaining = re.sub(r'rgba?\(\s*[\d.%\s,]+\)', '', val, flags=re.I)
        if '(' in remaining or ')' in remaining:
            continue
        if name == 'display' and val.lower() not in ('block', 'inline', 'inline-block', 'table', 'table-cell'):
            continue
        result.append(f'{name}: {val}')
    return '; '.join(result) or None


def _attribute(tag, name, value):
    if name == 'style':
        return _safe_style(value)
    if name in ('href', 'src'):
        if tag == 'img' and name == 'src' and _LOCAL_MEDIA.fullmatch(value):
            return value
        try:
            u = urlsplit(value)
            if u.scheme.lower() not in ('http', 'https') or not u.hostname or u.username or u.password:
                return None
        except (ValueError, TypeError):
            return None
    if name in ('width', 'height', 'colspan', 'rowspan', 'start') and not re.fullmatch(r'[1-9]\d{0,3}', value):
        return None
    return value


def sanitize_rich_html(value):
    """Sanitize an editor document before it is persisted or displayed."""
    return nh3.clean(str(value or ''), tags=_TAGS, attributes=_ATTRS, attribute_filter=_attribute,
        clean_content_tags={'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'form'},
        filter_style_properties=_STYLE_PROPERTIES, url_schemes={'http', 'https'},
        url_relative=lambda url: url if _LOCAL_MEDIA.fullmatch(url) else None,
        link_rel='noopener noreferrer')


def _reader_text_issue(text, literal=False):
    if re.search(r'[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]', text):
        return '出现损坏字符，请按原文恢复'
    if literal:
        return None
    # Inspect escaping without rewriting legitimate comparisons such as “3 < 5”.
    decoded = re.sub('[\u200b\u2060\ufeff]', '', text)
    for _ in range(6):
        expanded = html.unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        if html.unescape(decoded) != decoded:
            return '文字被反复转义，请还原为原句'
    for pattern, instruction in ((_VISIBLE_TAG, '去掉印在文字里的 HTML 标签'),
                                 (_MARKDOWN, '去掉未渲染的 Markdown 标记'),
                                 (_EDITOR_NOTE, '删除写给编辑的备注，保留有出处的事实')):
        match = pattern.search(decoded)
        if match:
            return f'{instruction}「{match.group()[:80]}」'
    if any(c in decoded for c in '*_~`[]'):
        for token in _PROSE_MARKDOWN.parseInline(decoded)[0].children or []:
            if token.type in ('em_open','strong_open','s_open','code_inline','link_open','image'):
                return f'去掉未渲染的 Markdown 标记「{(token.markup or decoded)[:60]}」'
    return None


def _body_reader_fields(body):
    """Join inline text in reading order, keeping block and code boundaries."""
    root = lhtml.fragment_fromstring(body, create_parent='div')
    buffer = []
    def flush(label):
        if buffer:
            yield label, ''.join(buffer), False
            buffer.clear()

    def walk(node, label):
        if node.text:
            buffer.append(node.text)
        for child in node:
            tag = child.tag
            if tag in ('code', 'pre'):
                yield from flush(label)
                yield label, child.text_content(), True
            elif isinstance(tag, str) and tag not in ('style','script'):
                child_label = '图片说明' if tag == 'figcaption' else label
                if tag in _BLOCKS:
                    yield from flush(label)
                yield from walk(child, child_label)
                if tag in _BLOCKS:
                    yield from flush(child_label)
                if tag == 'img':
                    for attr in ('alt','title'):
                        if child.get(attr):
                            yield '图片说明', child.get(attr), False
            if child.tail:
                buffer.append(child.tail)
    yield from walk(root, '正文')
    yield from flush('正文')


def reader_issues(draft):
    fields = [('标题', draft.get('title') or '', False), ('摘要', draft.get('summary') or '', False)]
    for body in dict.fromkeys(draft.get(k) for k in ('body_html','html') if draft.get(k)):
        fields.extend(_body_reader_fields(body))
    issues = []
    for label, text, literal in fields:
        issue = _reader_text_issue(text, literal)
        if issue:
            message = f'{label}：{issue}'
            if message not in issues:
                issues.append(message)
    body, rendered = draft.get('body_html'), draft.get('html')
    if body and rendered and body != rendered:
        from .composition import semantic_html
        if semantic_html(body)['paragraphs'] != semantic_html(rendered)['paragraphs']:
            issues.append('排版后的可见正文与送审正文不一致，请保留原句和顺序')
        if image_descriptions(body) != image_descriptions(rendered):
            issues.append('排版改变了图片说明，请保留送审时的文字')
    return issues


def image_descriptions(body):
    tree = lhtml.fragment_fromstring(body or '', create_parent='section')
    return [(node.get('alt', ''), node.get('title', '')) for node in tree.xpath('.//img[@src or @alt or @title]')]


def _inline_mark(state, silent):
    match = _INLINE_MARK.match(state.src, state.pos, state.posMax)
    if not match:
        return False
    if not silent:
        state.push('html_inline', '', 0).content = match.group().lower()
    state.pos = match.end()
    return True


def _markdown(value):
    parser = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table').enable('strikethrough')
    parser.inline.ruler.before('html_inline', 'editorial_mark', _inline_mark)
    tokens = parser.parse(str(value or ''))
    # A standalone image gets semantic figure/caption markup; all other images
    # remain inline. This keeps the source URL intact for remote reconciliation.
    for i in range(1, len(tokens) - 1):
        token = tokens[i]
        if token.type == 'inline' and token.children and len(token.children) == 1 and token.children[0].type == 'image' and tokens[i - 1].type == 'paragraph_open' and tokens[i + 1].type == 'paragraph_close':
            tokens[i - 1].tag = tokens[i + 1].tag = 'figure'
            token.children[0].meta['standalone'] = True
    original_image = parser.renderer.rules['image']

    def image_rule(renderer, tokens, idx, options, env):
        token = tokens[idx]
        result = original_image(tokens, idx, options, env)
        if token.meta.get('standalone') and token.content.strip():
            result += '<figcaption>' + html.escape(token.content.strip()) + '</figcaption>'
        return result

    parser.add_render_rule('image', image_rule)
    rendered = parser.renderer.render(tokens, parser.options, {})
    return sanitize_rich_html(rendered)


def _theme_css(theme):
    if theme not in ('default', 'simple'):
        raise ValueError('不支持的公众号排版主题')
    css = (_THEMES / 'default.css').read_text(encoding='utf-8')
    if theme == 'simple':
        css += '\n' + (_THEMES / 'simple.css').read_text(encoding='utf-8')
    # Fix the upstream variables at export time so copied inline styles do not
    # depend on custom CSS properties supported by the target editor.
    css = css.replace('var(--md-blockquote-background, var(--blockquote-background))', '#f5f6f7')
    css = css.replace('var(--md-blockquote-background, transparent)', '#f5f6f7')
    css = css.replace('hsl(var(--foreground))', '#303030')
    css = css.replace('var(--md-primary-color)', '#426b62' if theme == 'default' else '#435b83')
    css = css.replace('var(--md-link-color)', '#315f91')
    css = css.replace('var(--md-font-size)', '16px')
    css = css.replace('var(--md-block-spacing)', '1')
    css = re.sub(r'calc\(([-\d.]+(?:em|px))\s*\*\s*1\)', r'\1', css)
    css = re.sub(r'calc\(16px\s*\*\s*([\d.]+)\)', lambda m: f'{16 * float(m.group(1)):g}px', css)
    # color-mix support is uneven in platform editors; fallback styles below
    # replace those declarations on the supported article elements.
    css = re.sub(r'[^;{}]+:\s*color-mix\([^;{}]+\)\s*;', '', css)
    return css


_COMMON = '''
section.article {font-family: sans-serif; color:#303030; font-size:16px; line-height:1.75; text-align:left;}
p {margin:1.25em 8px; letter-spacing:0.04em;}
strong {color:inherit;} blockquote > p {color:inherit;}
blockquote {margin:1.3em 8px; padding:1em; border-left:4px solid #899b96; background:#f5f6f7;}
blockquote > p {margin:0;}
figure {margin:1.5em 8px;} img {display:block;max-width:100%;height:auto;margin:0 auto;}
figcaption {text-align:center;color:#777;font-size:0.8em;line-height:1.5;}
pre {padding:1em;overflow-x:auto;background:#f5f6f7;white-space:pre-wrap;}
code {font-family:monospace;} table {border-collapse:collapse;max-width:100%;}
th,td {border:1px solid #ddd;padding:0.35em 0.5em;} th {background:#f5f6f7;}
a {color:#315f91;text-decoration:underline;}
'''

BRAND_BLUE = '#0438fd'


@lru_cache(maxsize=128)
def cover_accent(path):
    """Pick the dominant non-neutral cover ink, dark enough for text on white."""
    from PIL import Image
    with Image.open(path) as image:
        image.thumbnail((64, 64))
        sample = image.convert('RGB').quantize(colors=12).convert('RGB')
    for _, rgb in sorted(sample.getcolors(), reverse=True):
        if max(rgb) - min(rgb) < 40:
            continue
        while True:
            channels = [((c / 255 + .055) / 1.055) ** 2.4 if c > 10 else c / 3294.6 for c in rgb]
            if sum(v * w for v,w in zip(channels, (.2126,.7152,.0722))) <= .183:
                return '#%02x%02x%02x' % rgb
            rgb = tuple(int(c * .9) for c in rgb)
    return BRAND_BLUE


_WECHAT_HEADINGS = '''
h2 {display:block; text-align:left; font-size:18px; line-height:1.5; color:ACCENT;
    background:transparent; padding:0; margin:24px 0 7px; border:0; border-radius:0; box-shadow:none;}
h2.chapter {text-align:center; margin:32px 0 16px;}
h2.chapter span {display:block; font-size:32px; line-height:1.15;}
p strong,p b {color:inherit; font-weight:bold;}
p u {color:inherit; text-decoration:underline;
    text-decoration-style:dotted; text-decoration-color:ACCENT; text-underline-offset:3px;}
h1.digest-group {display:table; margin:28px auto 12px; padding:3px 15px; color:white;
    background-color:ACCENT; border:0; font-size:18px; font-weight:bold; text-align:center;}
'''
_TOUTIAO = '''
section.article {font-family:sans-serif;color:#303030;font-size:17px;line-height:1.8;}
p {margin:1em 0;} h1,h2,h3,h4,h5,h6 {margin:1.5em 0 0.7em;font-weight:bold;}
blockquote {margin:1em 0;padding-left:1em;border-left:3px solid #aaa;color:#555;}
figure {margin:1em 0;} img {display:block;max-width:100%;height:auto;margin:0 auto;}
figcaption {text-align:center;font-size:0.8em;color:#777;}
pre {white-space:pre-wrap;} table {border-collapse:collapse;} th,td {border:1px solid #ddd;padding:0.35em;}
a {color:#315f91;}
'''


def render_rich_html(value, platform, theme='default', presentation_mode=None, accent=None):
    """Apply platform defaults to sanitized editable HTML, preserving explicit styles."""
    if platform == 'local': return sanitize_rich_html(value)
    if platform not in ('wechat', 'toutiao'):
        raise ValueError('不支持的发布平台')
    if presentation_mode not in (None, 'toutiao_native'):
        raise ValueError('不支持的排版模式')
    if presentation_mode == 'toutiao_native' and platform != 'toutiao':
        raise ValueError('头条原生排版模式仅适用于头条')
    safe = sanitize_rich_html(value)
    from lxml import html as lxml_html
    fragment = lxml_html.fragment_fromstring(safe,create_parent='div')
    # Digest groups are first-level headings immediately followed by a story heading.
    groups = fragment.xpath('.//h1[following-sibling::*[1][self::h2]]')
    if presentation_mode == 'toutiao_native':
        # Preview defaults belong to the preview container, not the submitted
        # document; inline styles here are only those supplied by the author.
        underlines = fragment.xpath('.//u')
        for mark in underlines:
            mark.drop_tag()  # The native draft editor saves this text without its underline.
        if len(groups) < 2:
            if not underlines:
                return safe
            return html.escape(fragment.text or '') + ''.join(
                lxml_html.tostring(el, encoding='unicode', method='html') for el in fragment)
        for heading in fragment.xpath('.//h2'):
            heading.tag = 'p'
            label = lxml_html.Element('strong')
            label.text = heading.text_content()
            heading.clear()
            heading.append(label)
        return lxml_html.tostring(fragment,encoding='unicode',method='html')
    if platform == 'wechat':
        ink = accent if re.fullmatch(r'#[0-9a-fA-F]{6}', accent or '') else BRAND_BLUE
        css = _theme_css(theme) + _COMMON + _WECHAT_HEADINGS.replace('ACCENT',ink)
        if len(groups) >= 2:
            for group in groups:
                group.set('class','digest-group')
        for heading in fragment.xpath('.//h2[not(*)]'):
            match = re.fullmatch(r'(\d{2})(\s+)(.+)',heading.text or '')
            if match:
                heading.set('class','chapter')
                heading.text = None
                number = lxml_html.Element('span')
                number.text = match[1]
                number.tail = match[2] + match[3]
                heading.append(number)
        safe = html.escape(fragment.text or '') + ''.join(
            lxml_html.tostring(el,encoding='unicode',method='html') for el in fragment)
    else:
        css = _TOUTIAO
    document = '<html><head><style>' + css + '</style></head><body><section class="article">' + safe + '</section></body></html>'
    # No network or filesystem access through CSS imports or links.
    result = transform(document, allow_network=False, disable_link_rewrites=True,
                       remove_classes=True, strip_important=False, cssutils_logging_level=logging.CRITICAL)
    root = lxml_html.fromstring(result)
    article = root.xpath('//section')[0]
    return lxml_html.tostring(article, encoding='unicode', method='html')


def import_markdown(value):
    """Convert model/history Markdown into an unthemed editable HTML source."""
    return _markdown(value)


def render_markdown(value, platform, theme='default'):
    """Import Markdown into the same HTML renderer used for editable rich text."""
    return render_rich_html(import_markdown(value), platform, theme)
