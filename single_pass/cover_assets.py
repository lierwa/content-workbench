"""Reuse article images first; search for a related photo only when none exist."""
import hashlib
import json
import re
import threading
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

from PIL import Image, ImageOps

_lock = threading.Lock()
_ALLOWED = {'cc0', 'public domain', 'pdm'}
SEARCH_VERSION = 9


def _source_download(url, source_url):
    from .materials import _validate_url
    class Redirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _validate_url(newurl, 15, entry=True)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    _validate_url(url, 15, entry=True)
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': source_url})
    with build_opener(Redirects()).open(request, timeout=15) as response:
        content = response.read(10_000_001)
    if len(content) > 10_000_000:
        raise ValueError('原帖图片过大')
    return content


def for_materials(media_dir, job_id, materials, query, issues=None):
    """Article photos are cached before review, so failed downloads are not selected."""
    choices, seen = [], set()
    issues = issues if issues is not None else []
    for material in sorted(materials, key=lambda m: m.get('role') != 'original'):
        if material.get('status') != 'read':
            continue
        from .materials import IMAGES_VERSION
        if 'images' not in material or material.get('images_version') != IMAGES_VERSION:
            # Older saved text has no image manifest. Fetch it once without
            # replacing the text snapshot or purchasing another writing call.
            from .materials import fetch_material
            source_url = material.get('final_url') or material['url']
            cache = Path(media_dir).parent / 'asset-cache'
            cache.mkdir(exist_ok=True)
            manifest = cache / (hashlib.sha256(source_url.encode()).hexdigest() + f'.images-v{IMAGES_VERSION}.json')
            if manifest.exists():
                material['images'] = json.loads(manifest.read_text(encoding='utf-8'))
                material['images_version'] = IMAGES_VERSION
            else:
                fresh = fetch_material(source_url)
                if fresh.get('status') == 'read':
                    material['images'] = fresh.get('images') or []
                    material['images_version'] = IMAGES_VERSION
                    material.pop('image_error', None)
                    manifest.write_text(json.dumps(material['images'], ensure_ascii=False), encoding='utf-8')
                else:
                    material['image_error'] = fresh.get('error') or '原帖图片暂时无法读取'
                    issues.append({'stage': 'source_read', 'url': source_url, 'error': material['image_error']})
        for index, image in enumerate(material.get('images') or []):
            url = image['url']
            if url in seen:
                continue
            seen.add(url)
            choice = {'source': material.get('final_url') or material['url'], 'original_url': url,
                      'download_url': url, 'title': f"{material.get('title') or material.get('url')} · 图{index + 1}",
                      'description': image.get('description') or '', 'use': 'source_image',
                      'material_id': material.get('material_id'), 'role': material.get('role')}
            try:
                asset = select(media_dir, job_id, choice)
            except (OSError, ValueError) as exc:
                issues.append({'stage': 'source_download', 'url': url, 'error': str(exc)[:300]})
                continue
            if asset:
                choices.append(dict(choice, asset=asset))
    # Related reporting is resolved and checked by article_images. A generic
    # stock-photo search cannot establish that a photo depicts this event.
    return choices


def _download(url, limit, image=False, timeout=15):
    host = urlsplit(url).hostname
    if host not in ('commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org') or urlsplit(url).scheme != 'https':
        raise ValueError('图片来源不在允许范围')
    request = Request(url, headers={'User-Agent': 'ContentWorkbench/1.0 (editorial asset selection)'})
    with urlopen(request, timeout=timeout) as response:
        if urlsplit(response.url).hostname not in ('commons.wikimedia.org', 'upload.wikimedia.org', 'thumb.wikimedia.org'):
            raise ValueError('素材跳转到未知来源')
        if image and response.headers.get_content_type() not in ('image/jpeg', 'image/png'):
            raise ValueError('素材不是照片格式')
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError('素材过大')
    return content


def _candidates(query, timeout=15):
    query = str(query or '').strip()[:100]
    if not query:
        return []
    params = {'action': 'query', 'generator': 'search', 'gsrsearch': query + ' filetype:bitmap',
              'gsrnamespace': 6, 'gsrlimit': 20, 'prop': 'imageinfo',
              'iiprop': 'url|extmetadata|size', 'iiurlwidth': 1200, 'format': 'json'}
    result = json.loads(_download('https://commons.wikimedia.org/w/api.php?' + urlencode(params), 2_000_000, timeout=timeout))
    choices = []
    for page in result.get('query', {}).get('pages', {}).values():
        for info in page.get('imageinfo', []):
            metadata = info.get('extmetadata', {})
            license_name = metadata.get('LicenseShortName', {}).get('value', '')
            description = metadata.get('ImageDescription', {}).get('value', '')
            if (license_name.lower() in _ALLOWED and info.get('width', 0) >= 800
                    and info.get('height', 0) >= 450 and info['width'] >= info['height']
                    and urlsplit(info.get('url', '')).path.lower().endswith(('.jpg', '.jpeg', '.png'))
                    and not any(term in (description + page['title']).lower() for term in
                        ('ai-generated','ai generated','stable diffusion','midjourney','dall-e'))):
                choices.append({'source': info.get('descriptionurl'), 'original_url': info['url'],
                    'download_url': info.get('thumburl') or info['url'], 'title': page['title'],
                    'description': description[:800], 'license': license_name,
                    'license_url': metadata.get('LicenseUrl', {}).get('value'),
                    'artist': metadata.get('Artist', {}).get('value'), 'query': query,
                    'use': 'topic_illustration_not_event_photo'})
    return choices[:8]


def candidates(query, timeout=30):
    deadline = time.monotonic() + timeout
    initial = _candidates(query, min(15,timeout))
    broad = re.sub(r'\b(?:used|secondhand|second-hand|closeup)\b', '', str(query or ''), flags=re.I)
    broad = ' '.join(broad.split())
    if broad and len(initial) < 5 and time.monotonic() < deadline:
        # Keep the subject intact: reducing "children chemistry experiment kit"
        # to "children" returned beaches and family portraits for a safety story.
        extra = _candidates(broad + ' Unsplash', min(15,deadline-time.monotonic()))
        known = {x['original_url'] for x in initial}
        initial.extend(x for x in extra if x['original_url'] not in known)
    return initial[:8]


def select(media_dir, job_id, choice):
    if not choice or (choice.get('use') != 'source_image' and choice.get('license', '').lower() not in _ALLOWED):
        return None
    media_dir = Path(media_dir)
    cache = media_dir.parent / 'asset-cache'
    cache.mkdir(exist_ok=True)
    key = hashlib.sha256(choice['original_url'].encode()).hexdigest()
    meta_path = cache / (key + '.json')
    with _lock:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            image_path = cache / (key + meta.get('extension', '.png'))
            content = image_path.read_bytes()
            if meta.get('sha256') != hashlib.sha256(content).hexdigest():
                raise ValueError('缓存图片校验不符')
        else:
            raw = (_source_download(choice['download_url'], choice['source']) if choice.get('use') == 'source_image'
                   else _download(choice['download_url'], 10_000_000, image=True))
            with Image.open(BytesIO(raw)) as source:
                if source.width * source.height > 24_000_000:
                    raise ValueError('图片尺寸过大')
                source.load()
                if choice.get('use') == 'source_image' and source.format in ('JPEG', 'PNG'):
                    content, extension = raw, '.jpg' if source.format == 'JPEG' else '.png'
                else:
                    photo = ImageOps.exif_transpose(source).convert('RGB')
                    output = BytesIO(); photo.save(output, format='PNG'); content, extension = output.getvalue(), '.png'
            if choice.get('use') == 'source_image':
                (cache / (key + '.original')).write_bytes(raw)
            meta = dict(choice, sha256=hashlib.sha256(content).hexdigest(), extension=extension,
                        original_sha256=hashlib.sha256(raw).hexdigest())
            image_path = cache / (key + extension)
            image_path.write_bytes(content)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        with Image.open(BytesIO(content)) as image:
            if min(image.size) < 100:
                raise ValueError('图片是占位图或图标，不能用于正文及封面')
        filename = job_id + '-' + meta['sha256'][:24] + meta.get('extension', '.png')
        (media_dir / filename).write_bytes(content)
        return {'cover_url': '/api/production/media/' + filename, 'cover_sha256': meta['sha256'],
                'cover_kind': 'source_image' if choice.get('use') == 'source_image' else 'licensed_photo', 'cover_source': meta}
