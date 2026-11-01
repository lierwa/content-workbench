"""Continue an owned editor after cover selection, without creating another draft."""
import base64
import gzip
import hashlib
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import composition


def can_resume_images(prior):
    phase, step = prior.get('phase'), prior.get('step')
    return (phase == 'fill_body'
            or (phase == 'fill_summary' and step == 'reveal_summary')
            or (phase == 'upload_cover' and step in ('choose_cover_library','file_chooser_upload','select_uploaded_cover'))
            or (phase == 'upload_cover' and step == 'inspect_cover_dialog'
                and not re.search(r'L1 (?:modal|mask)',prior.get('observation') or ''))
            or (phase == 'upload_cover' and step in ('confirm_cover_selection','inspect_cover_dialog')
                and 'heading "选择图片"' in (prior.get('observation') or ''))
            or (phase == 'upload_body_images' and (step in ('read_uploaded_body_image','clear_temporary_body')
                or (step == '' and prior.get('image_map')))))


def context_for(adapter, job, snapshot):
    paths = job.get('failure_evidence_paths') or []
    journal_path=adapter._path(job['target']['account_id'])/'draft-updates'/(str(job.get('operation_id'))+'.json')
    if journal_path.exists():
        journal=json.loads(journal_path.read_text(encoding='utf-8'))
        if (journal.get('binding',{}).get('content_hash')==snapshot['content_hash']
                and (journal.get('save_attempt') or journal.get('phase') in ('saved','verified')
                    or (journal.get('result') or {}).get('phase')=='save_draft')):
            return dict(kind='wechat_saved_update',operation_id=job['operation_id'],revision=job['revision'],
                content_hash=snapshot['content_hash'],account_identity_key=journal['binding']['identity'])
        if (journal.get('binding',{}).get('content_hash')==snapshot['content_hash']
                and (journal.get('result') or {}).get('evidence_paths')):
            paths=journal['result']['evidence_paths']
    if not paths:
        failure = next((str(h.get('detail') or '') for h in reversed(job.get('history', []))
            if h.get('action') == 'sync_blocked' and '图片诊断=' in str(h.get('detail') or '')), job.get('error') or '')
        match = re.search(r'图片诊断=(.+\.json)', failure)
        paths = [match.group(1)] if match else []
    if len(paths) != 1:
        raise ValueError('没有唯一的原编辑页诊断')
    path = Path(paths[0]).resolve()
    account = adapter._read(job['target']['account_id'])
    if path.parent != (adapter._path(account['id']) / 'diagnostics').resolve():
        raise ValueError('诊断不属于该账号')
    raw = path.read_bytes(); prior = json.loads(raw)
    if can_resume_images(prior):
        if (prior.get('operation_id') != job['operation_id'] or prior.get('revision') != job['revision']
                or prior.get('content_hash') != snapshot['content_hash']):
            raise ValueError('原图片操作或稿件版本不匹配')
        return dict(kind='wechat_image_resume', session_id=prior['session_id'], tab_id=str(prior['tab_id']),
            agent_window_id=prior['agent_window_id'], diagnostic_path=str(path),
            diagnostic_sha256=hashlib.sha256(raw).hexdigest(), operation_id=job['operation_id'],
            revision=job['revision'], content_hash=snapshot['content_hash'],
            account_identity_key=account['identity']['key'])
    if (not snapshot.get('cover_path') and not snapshot.get('body_images')
            and prior.get('phase') in ('fill_summary','save_draft')):
        from urllib.parse import urlsplit, parse_qs
        if prior.get('operation_id') and prior['operation_id'] != job['operation_id']:
            raise ValueError('原编辑页属于另一同步操作')
        tabs = json.loads(adapter._bsk('tab','list','--scope','agent','--session',prior['session_id'],'--json'))['tabs']
        account_tabs = [t for t in tabs if str(t['tab_id']) != str(prior['tab_id'])
                        and urlsplit(t.get('url','')).hostname == 'mp.weixin.qq.com'
                        and parse_qs(urlsplit(t['url']).query).get('action') != ['edit']]
        if not account_tabs: raise ValueError('原任务缺少账号核验页面')
        return dict(kind='wechat_editor_resume', session_id=prior['session_id'], tab_id=str(prior['tab_id']),
            account_tab_id=str(account_tabs[0]['tab_id']), agent_window_id=prior['agent_window_id'],
            diagnostic_path=str(path), diagnostic_sha256=hashlib.sha256(raw).hexdigest(),
            operation_id=job['operation_id'], revision=job['revision'], content_hash=snapshot['content_hash'],
            account_identity_key=account['identity']['key'])
    if prior.get('phase') != 'upload_cover' or prior.get('step') not in ('confirm_cover_selection','inspect_cover_dialog'):
        raise ValueError('当前仅支持从已上传封面的裁剪阶段续作')
    if prior.get('cover_asset', {}).get('sha256') != snapshot['cover_sha256']:
        raise ValueError('诊断封面与稿件不一致')
    return dict(kind='wechat_cover_resume', session_id=prior['session_id'], tab_id=str(prior['tab_id']),
        agent_window_id=prior['agent_window_id'], diagnostic_path=str(path),
        diagnostic_sha256=hashlib.sha256(raw).hexdigest(), operation_id=job['operation_id'],
        revision=job['revision'], content_hash=snapshot['content_hash'],
        account_identity_key=account['identity']['key'])


def resume(adapter, account, snapshot, context):
    from .platforms import _wechat_cdn_verify_cover, _safe_bsk_error, _write_json
    session, tab = context['session_id'], context['tab_id']
    journal_path = adapter.root.parent / 'production' / 'evidence' / ('wechat-' + context['operation_id'] + '-resume.json')
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {}
    try:
        raw = Path(context['diagnostic_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != context['diagnostic_sha256']:
            raise ValueError('原诊断已变化')
        prior = json.loads(raw)
        if context['content_hash'] != snapshot['content_hash'] or context['revision'] != snapshot['revision']:
            raise ValueError('续作稿件已变化')
        cover = Path(snapshot['cover_path']).read_bytes()
        if hashlib.sha256(cover).hexdigest() != snapshot['cover_sha256']:
            raise ValueError('图片文件已变化')
        tabs_now = json.loads(adapter._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
        original_editor = next((x for x in tabs_now if str(x['tab_id'])==tab
            and urlsplit(x.get('url','')).hostname=='mp.weixin.qq.com'
            and urlsplit(x.get('url','')).path=='/cgi-bin/appmsg'), None)
        if original_editor is None:
            if journal.get('save_started'):
                raise ValueError('保存已触发但原页不存在，必须查找现有草稿')
            # This diagnostic records a failure before the save button. Still
            # reconcile the live draft list before opening a replacement editor.
            result = adapter.save_draft(account['id'],dict(snapshot,reconcile_before_create=True),None)
            result['recovery'] = '原页已离开；确认原操作未到保存阶段并检查草稿列表后续作'
            return result
        with adapter._lock(account['id']), adapter._browser_lock:
            state = json.loads(adapter._bsk('status', '--json'))
            if not any(x['session_id'] == session and x['agent_window_id'] == context['agent_window_id'] for x in state.get('sessions', [])):
                raise ValueError('原任务窗口不存在，不能新建代替')
            tabs = json.loads(adapter._bsk('tab','list','--scope','agent','--session',session,'--json'))['tabs']
            current = next((x for x in tabs if str(x['tab_id']) == tab), None)
            other = next((x for x in tabs if str(x['tab_id']) != tab and urlsplit(x.get('url','')).hostname == 'mp.weixin.qq.com'), None)
            if not current or urlsplit(current['url']).path != '/cgi-bin/appmsg' or not other:
                raise ValueError('原编辑页或账号页不存在')
            identity = adapter._browser_identity(account, session, str(other['tab_id']))
            if identity['key'] != context['account_identity_key']:
                raise ValueError('公众号身份不匹配')
            def evaluate(script):
                result = json.loads(adapter._bsk('evaluate','--json','--session',session,'--tab-id',tab,script))
                if not result.get('ok'): raise RuntimeError('编辑页读取失败')
                return result.get('value')
            def fields():
                return evaluate("""(() => {const b=document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])');
                  return {title:document.querySelector('.ProseMirror[data-placeholder="请在这里输入标题"]')?.innerText,
                    html:b?.innerHTML,summary:document.querySelector('#js_description')?.value,
                    id:new URL(location.href).searchParams.get('appmsgid')}})()""")
            value = fields(); expected = composition.semantic_html(snapshot['html']); actual = composition.semantic_html(value['html'])
            if (composition.normalize_text(value['title']) != composition.normalize_text(snapshot['title'])
                    or actual['paragraphs'] != expected['paragraphs'] or actual['unsafe']
                    or composition.normalize_text(value['summary']) != composition.normalize_text(snapshot['summary'])):
                raise ValueError('现有编辑页文字已变化，不能覆盖')
            if journal.get('save_started'):
                if not value.get('id'): raise ValueError('保存回执仍未知，未重复点击')
            else:
                obs = adapter._observe_stable(session, tab)
                if '编辑封面' in obs:
                    for _ in range(20):
                        refs = re.findall(r'(@e\d+)\s+button\s+"确认"(?:\s|$)',obs)
                        if len(refs)==1: break
                        time.sleep(.5); obs=adapter._observe_stable(session,tab)
                    if len(refs)!=1: raise ValueError('裁剪确认按钮尚未就绪')
                    adapter._bsk('click', refs[0], '--session',session,'--tab-id',tab)
                # Read only visible cover URLs, never page tokens or script state.
                covers = []
                for _ in range(20):
                    covers = evaluate("""[...document.querySelectorAll('#js_cover_area .js_cover_preview_new')]
                      .filter(x=>getComputedStyle(x).display!=='none').map(x=>x.style.backgroundImage)
                      .map(x=>x.slice(4,-1).replace(/^['"]|['"]$/g,'')).filter(x=>x.startsWith('https://'))""")
                    if len(set(covers or [])) == 1: break
                    time.sleep(.5)
                if len(set(covers or [])) != 1: raise ValueError('封面未唯一显示')
                parsed = urlsplit(covers[0]); cover_url = urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
                original = cover_url.rsplit('/',1)[0] + '/0'
                from .asset_verification import verify_photo
                comparison = verify_photo(original, cover, 'wechat')
                image_map = {}
                for src, asset in snapshot['body_images'].items():
                    if asset['sha256'] != snapshot['cover_sha256']:
                        raise ValueError('此续作不能猜测其他正文图片的上传结果')
                    existing_url = prior.get('image_map',{}).get(Path(src).name)
                    if existing_url:
                        verify_photo(existing_url,Path(asset['path']).read_bytes(),'wechat')
                    image_map[src] = existing_url or original
                rendered = snapshot['html']
                for src, url in image_map.items(): rendered=rendered.replace(html.escape(src,quote=True),html.escape(url,quote=True))
                desired = composition.semantic_html(rendered)
                if not composition.wechat_content_matches(desired,actual):
                    # Accept only the originally evidenced missing-image case.
                    if actual['images'] or composition.semantic_html(prior.get('body_html',''))['images']:
                        raise ValueError('正文图片状态与原诊断不同，不能重复回填')
                    payload=base64.b64encode(gzip.compress(json.dumps({'html':rendered,'plain':'\n\n'.join(desired['paragraphs'])},ensure_ascii=False).encode())).decode()
                    script="""(async()=>{const b=document.querySelector('.ProseMirror[contenteditable=true]:not([data-placeholder])');
                      const stream=new Blob([Uint8Array.from(atob(PAYLOAD),c=>c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip'));
                      const d=JSON.parse(await new Response(stream).text()); b.focus();document.execCommand('selectAll');
                      const t=new DataTransfer();t.setData('text/html',d.html);t.setData('text/plain',d.plain);
                      const e=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:t});b.dispatchEvent(e);return e.defaultPrevented})()""".replace('PAYLOAD',json.dumps(payload))
                    if evaluate(script) is not True: raise ValueError('正文补图未确认')
                value=fields()
                if not composition.wechat_content_matches(desired,composition.semantic_html(value['html'])):
                    raise ValueError('保存前正文和图片未核对一致')
                journal.update(image_map=image_map,cover_url=cover_url,cover_comparison=comparison,save_started=True)
                _write_json(journal_path,journal)
                obs=adapter._observe_stable(session,tab)
                adapter._bsk('click',adapter._ref(obs,'button','保存为草稿'),'--session',session,'--tab-id',tab)
            for _ in range(30):
                value=fields()
                if re.fullmatch(r'\d+',str(value.get('id') or '')):break
                time.sleep(.5)
            if not value.get('id'):raise ValueError('尚未取得草稿保存回执')
            return dict(status='saved',draft_id=value['id'],title=snapshot['title'],account_identity=identity,
                cover_url=journal['cover_url'],cover_sha256=snapshot['cover_sha256'],image_map=journal['image_map'],
                cover_comparison=journal.get('cover_comparison'),
                evidence_paths=[str(journal_path)],evidence='同一编辑页完成封面和正文图片后保存，待独立回读')
    except Exception as exc:
        return dict(status='unknown',message='公众号原位续作待核验：'+_safe_bsk_error(str(exc)),evidence_paths=[str(journal_path)] if journal_path.exists() else [])
