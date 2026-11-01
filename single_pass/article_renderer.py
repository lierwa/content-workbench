"""Sanitize rich text and render platform HTML with local doocs/md theme assets."""
import html
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import nh3
import cssutils
from markdown_it import MarkdownIt
from premailer import transform


_THEMES = Path(__file__).resolve().parent.parent / 'vendor' / 'doocs-md'
_LOCAL_MEDIA = re.compile(r'^/api/production/media/[0-9a-f]{32}-(?:[0-9a-f]{24}|[0-9a-f]{32})\.(?:png|jpg|jpeg)$')
_TAGS = {'section', 'div', 'span', 'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote',
         'strong', 'b', 'em', 'i', 'u', 's', 'del', 'sub', 'sup', 'mark', 'a', 'img', 'figure',
         'figcaption', 'ul', 'ol', 'li', 'pre', 'code', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td'}
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


def _markdown(value):
    parser = MarkdownIt('commonmark', {'html': False, 'breaks': True}).enable('table').enable('strikethrough')
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
_TOUTIAO = '''
section.article {font-family:sans-serif;color:#303030;font-size:17px;line-height:1.8;}
p {margin:1em 0;} h1,h2,h3,h4,h5,h6 {margin:1.5em 0 0.7em;font-weight:bold;}
blockquote {margin:1em 0;padding-left:1em;border-left:3px solid #aaa;color:#555;}
figure {margin:1em 0;} img {display:block;max-width:100%;height:auto;margin:0 auto;}
figcaption {text-align:center;font-size:0.8em;color:#777;}
pre {white-space:pre-wrap;} table {border-collapse:collapse;} th,td {border:1px solid #ddd;padding:0.35em;}
a {color:#315f91;}
'''


def render_rich_html(value, platform, theme='default', presentation_mode=None):
    """Apply platform defaults to sanitized editable HTML, preserving explicit styles."""
    if platform not in ('wechat', 'toutiao'):
        raise ValueError('不支持的发布平台')
    if presentation_mode not in (None, 'toutiao_native'):
        raise ValueError('不支持的排版模式')
    if presentation_mode == 'toutiao_native' and platform != 'toutiao':
        raise ValueError('头条原生排版模式仅适用于头条')
    safe = sanitize_rich_html(value)
    if presentation_mode == 'toutiao_native':
        # Preview defaults belong to the preview container, not the submitted
        # document; inline styles here are only those supplied by the author.
        return safe
    css = _theme_css(theme) + _COMMON if platform == 'wechat' else _TOUTIAO
    document = '<html><head><style>' + css + '</style></head><body><section class="article">' + safe + '</section></body></html>'
    # No network or filesystem access through CSS imports or links.
    result = transform(document, allow_network=False, disable_link_rewrites=True,
                       remove_classes=True, strip_important=False, cssutils_logging_level=logging.CRITICAL)
    from lxml import html as lxml_html
    root = lxml_html.fromstring(result)
    article = root.xpath('//section')[0]
    return lxml_html.tostring(article, encoding='unicode', method='html')


def import_markdown(value):
    """Convert model/history Markdown into an unthemed editable HTML source."""
    return _markdown(value)


def render_markdown(value, platform, theme='default'):
    """Import Markdown into the same HTML renderer used for editable rich text."""
    return render_rich_html(import_markdown(value), platform, theme)
