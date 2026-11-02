"""Update only paragraph layout/emphasis in a previously verified platform draft."""
import copy
import html
import json
import re
from pathlib import Path
from io import BytesIO
from urllib.error import HTTPError
import uuid
from contextlib import nullcontext
from urllib.parse import urlsplit, parse_qs, urlunsplit
from . import composition


def words(body):
    return re.sub(r'\s+','',''.join(composition.semantic_html(body)['paragraphs']))


def eligible(snapshot,remote):
    previous=(remote or {}).get('readback') or {}
    return (previous.get('html') and snapshot['title']==previous.get('title')
        and words(snapshot['html'])==words(previous['html'])
        and snapshot['cover_sha256']==remote.get('cover_sha256'))


def resource(value):
    url=urlsplit(str(value or ''))
    host=url.netloc
    # Toutiao rotates signed CDN shards for the same immutable cover resource.
    if re.fullmatch(r'p\d+-sign\.toutiaoimg\.com',host):host='p-sign.toutiaoimg.com'
    return urlunsplit((url.scheme,host,url.path,'',''))


def unchanged(left,right):
    def normalized(value):
        value=copy.deepcopy(value)
        for k in ('cover_url','cover_media_id'):value[k]=resource(value.get(k))
        value['html']=re.sub(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)',lambda m:m[1]+resource(html.unescape(m[2])),value.get('html',''))
        return composition.remote_fingerprint(value)
    return normalized(left)==normalized(right)


def visible_photo(adapter,snapshot,remote,url,photo,browser_locked=False):
    """Compare the authenticated editor's rendered photo when its CDN denies HTTP reads."""
    from PIL import Image
    from .asset_verification import compare_photo_bytes
    with adapter._lock(snapshot['account_id']), (nullcontext() if browser_locked else adapter._browser_lock):
        session,owned=adapter._acquire_browser_session()
        if owned:raise ValueError('图片核验需要已有编辑页')
        tabs=json.loads(adapter._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
        tab=next((str(t['tab_id']) for t in tabs if urlsplit(t['url']).hostname=='mp.toutiao.com'
            and parse_qs(urlsplit(t['url']).query).get('pgc_id')==[remote['draft_id']]),None)
        if tab is None:raise ValueError('图片核验未找到对应草稿')
        adapter._bsk('tab','select',tab,'--session',session)
        def evaluate(script):
            value=json.loads(adapter._bsk('evaluate','--json','--session',session,'--tab-id',tab,script))
            if not value.get('ok'):raise ValueError('图片位置读取失败')
            return value['value']
        find="const matches=[...document.querySelectorAll('.ProseMirror img')].filter(x=>!x.closest('templ')&&x.src===URL);if(matches.length!==1)throw Error('image not unique');const i=matches[0];".replace('URL',json.dumps(url))
        evaluate('(()=>{'+find+"i.scrollIntoView({block:'center'});return true})()")
        adapter._observe_stable(session,tab)
        adapter._bsk('hover','--selector','[placeholder="请输入文章标题（2～30个字）"]','--session',session,'--tab-id',tab)
        # Hover may scroll the title into view. The article photo must still fit.
        box=evaluate('(()=>{'+find+"const r=i.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight,nw:i.naturalWidth,nh:i.naturalHeight,ready:i.complete}})()")
        if not box['ready'] or min(box['w'],box['h'],box['nw'],box['nh'])<=0 or box['x']<0 or box['y']<0 or box['x']+box['w']>box['vw'] or box['y']+box['h']>box['vh']:
            raise ValueError('正文图片未完整显示，不能核验')
        path=adapter.root.parent/'production'/'evidence'/('layout-photo-'+uuid.uuid4().hex+'.png')
        path.parent.mkdir(parents=True,exist_ok=True)
        adapter._bsk('screenshot','--session',session,'--tab-id',tab,'--out',str(path),'--json')
        with Image.open(path) as shot:
            sx=shot.width/box['vw'];sy=shot.height/box['vh']
            crop=shot.crop(tuple(round(v) for v in (box['x']*sx,box['y']*sy,(box['x']+box['w'])*sx,(box['y']+box['h'])*sy)))
            out=BytesIO();crop.save(out,format='PNG')
        check=compare_photo_bytes(out.getvalue(),photo)
        check.update(comparison='rendered_photo_color_and_structure',evidence_path=str(path))
        return check


def checked_images(snapshot,remote,observed,adapter=None,browser_locked=False):
    from .asset_verification import verify_photo
    expected=composition.semantic_html(snapshot['html'])['images']
    actual=composition.semantic_html(observed)['images']
    if len(expected)!=len(actual):raise ValueError('排版后图片数量改变')
    mapping=dict(remote.get('image_map') or {});checks={}
    for source,saved in zip(expected,actual):
        if source['position']!=saved['position']:raise ValueError('排版后图片位置改变')
        if resource(mapping.get(source['src']))!=resource(saved['src']):
            photo=Path(snapshot['body_images'][source['src']]['path']).read_bytes()
            try:checks[source['src']]=verify_photo(saved['src'],photo,snapshot['platform'])
            except HTTPError as exc:
                if exc.code!=403 or adapter is None or snapshot['platform']!='toutiao':raise
                checks[source['src']]=visible_photo(adapter,snapshot,remote,saved['src'],photo,browser_locked)
            mapping[source['src']]=saved['src']
    return mapping,checks


def reconcile(snapshot,remote,actual,adapter=None):
    """Accept a saved layout only after unchanged metadata and actual photo checks."""
    if not eligible(snapshot,remote) or not unchanged(dict(actual,html=remote['readback']['html']),remote['readback']):
        raise ValueError('排版更新之外的字段发生变化')
    mapping,checks=checked_images(snapshot,remote,actual['html'],adapter)
    return dict(remote,image_map=mapping,layout_image_checks=checks)
