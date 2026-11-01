"""Update only paragraph layout/emphasis in a previously verified platform draft."""
import base64
import copy
import gzip
import html
import json
import re
import time
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


def save(adapter,account,snapshot,remote):
    from .platforms import _safe_bsk_error
    from .toutiao_publisher import ToutiaoPublisher
    started=False
    try:
        actual=adapter.inspect_draft(account['id'],dict(remote,title=snapshot['title']))
        if actual.get('status')!='ok':
            raise ValueError(actual.get('message') or '平台草稿回读失败')
        if not unchanged(actual,remote['readback']):
            raise ValueError('平台草稿已有外部修改，不能覆盖排版')
        if not eligible(snapshot,remote):raise ValueError('排版更新不得改写内容或替换封面')
        platform=account['platform'];identity=actual['account_identity']
        with adapter._lock(account['id']),adapter._browser_lock:
            session,owned=adapter._acquire_browser_session()
            if owned:raise ValueError('需要复用已核验的任务窗口')
            tabs=json.loads(adapter._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
            field='appmsgid' if platform=='wechat' else 'pgc_id'
            domain='mp.weixin.qq.com' if platform=='wechat' else 'mp.toutiao.com'
            tab=next((str(t['tab_id']) for t in tabs if urlsplit(t['url']).hostname==domain
                and parse_qs(urlsplit(t['url']).query).get(field)==[remote['draft_id']]),None)
            if tab is None:raise ValueError('已核验的草稿编辑页不存在')
            adapter._bsk('tab','select',tab,'--session',session)
            def evaluate(script):
                value=json.loads(adapter._bsk('evaluate','--json','--session',session,'--tab-id',tab,script))
                if not value.get('ok'):raise ValueError('草稿页面读取失败')
                return value.get('value')
            selector='.ProseMirror[contenteditable=true]:not([data-placeholder])' if platform=='wechat' else '.ProseMirror[contenteditable=true]'
            current=evaluate('document.querySelector('+json.dumps(selector)+')?.innerHTML')
            current=ToutiaoPublisher._canonical_body(current) if platform=='toutiao' else current
            if not unchanged(dict(actual,html=current),actual):raise ValueError('编辑页在回读后发生变化')
            rendered=snapshot['html']
            for src,url in remote.get('image_map',{}).items():rendered=rendered.replace(html.escape(src,quote=True),html.escape(url,quote=True))
            if any(x['src'].startswith('/api/') for x in composition.semantic_html(rendered)['images']):raise ValueError('缺少既有图片映射')
            data={'html':rendered,'plain':'\n\n'.join(composition.semantic_html(rendered)['paragraphs']),
                  'selected_words':words(current)}
            payload=base64.b64encode(gzip.compress(json.dumps(data,ensure_ascii=False).encode())).decode()
            script=r"""(async()=>{const b=document.querySelector(SELECTOR);if(!b)return false;
                const stream=new Blob([Uint8Array.from(atob(PAYLOAD),c=>c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip'));
                const d=JSON.parse(await new Response(stream).text());b.focus();
                const range=document.createRange();range.selectNodeContents(b);
                const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
                await new Promise(resolve=>setTimeout(resolve,150));
                if(!b.contains(selection.anchorNode)||!b.contains(selection.focusNode)
                    ||selection.toString().replace(/\s/g,'')!==d.selected_words)return false;
                const t=new DataTransfer();t.setData('text/html',d.html);t.setData('text/plain',d.plain);
                const e=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:t});b.dispatchEvent(e);return e.defaultPrevented})()""".replace('SELECTOR',json.dumps(selector)).replace('PAYLOAD',json.dumps(payload))
            adapter._observe_stable(session,tab);started=True
            if evaluate(script) is not True:raise ValueError('编辑器未确认排版更新')
            time.sleep(10 if platform=='toutiao' else 1)
            observed=evaluate('document.querySelector('+json.dumps(selector)+').innerHTML')
            observed=ToutiaoPublisher._canonical_body(observed) if platform=='toutiao' else observed
            mapping,checks=checked_images(snapshot,remote,observed,adapter,browser_locked=True)
            rendered=snapshot['html']
            for src,url in mapping.items():rendered=rendered.replace(html.escape(src,quote=True),html.escape(url,quote=True))
            matcher=composition.wechat_content_matches if platform=='wechat' else composition.toutiao_content_matches
            if not matcher(composition.semantic_html(rendered),composition.semantic_html(observed)) or not composition.emphasis_preserved(rendered,observed):
                raise ValueError('保存前段落、图片或加粗未保留')
            if platform=='wechat':
                obs=adapter._observe_stable(session,tab)
                adapter._bsk('click',adapter._ref(obs,'button','保存为草稿'),'--session',session,'--tab-id',tab)
            time.sleep(5 if platform=='wechat' else 10)
            result=dict(remote,status='saved',title=snapshot['title'],account_identity=identity,verified_hash=None,
                image_map=mapping,layout_image_checks=checks,
                evidence='在原草稿更新段落与加粗；未新建、未发表，随后独立回读')
            return result
    except Exception as exc:
        return {'status':'unknown' if started else 'needs_attention','draft_id':remote.get('draft_id'),
            'account_identity':remote.get('account_identity'),'message':'排版更新未通过核验：'+_safe_bsk_error(str(exc))}
