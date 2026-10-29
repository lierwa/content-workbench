"""Durable per-article production, review and cautious platform operations."""
import copy
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import composition, screen


LOCAL_STAGES = {'queued', 'researching', 'writing', 'editing', 'checking', 'formatting'}
EXTERNAL_STAGES = {'syncing', 'publishing'}


class ProductionService:
    def __init__(self, data_root, candidate_provider, platform_adapter=None):
        self.root = Path(data_root).resolve() / 'production'
        self.jobs_dir = self.root / 'jobs'
        self.media_dir = self.root / 'media'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.candidate_provider = candidate_provider
        self.platform = platform_adapter
        self.lock = threading.RLock()
        self.account_locks = {}
        self.cancels = {}
        self.running = set()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='production')
        self.platform_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='platform')
        self.platform_queues = {}
        self.active_accounts = set()
        self.job_account_keys = {}
        self.scheduler_stop = threading.Event()
        self.jobs = {}
        for path in self.jobs_dir.glob('*.json'):
            job = json.loads(path.read_text(encoding='utf-8'))
            self.jobs[job['id']] = job
        for job in list(self.jobs.values()):
            if job.get('stage') in EXTERNAL_STAGES:
                job.update(stage='needs_attention', status='outcome_unknown', operation_uncertain=True, error='上次平台操作结果未知；请核验，不能直接重试提交')
                job['pending_operation'] = None
                self._schedule_check(job)
                self._record(job, 'interrupted_external', job['error'])
            elif job.get('stage') in LOCAL_STAGES and job.get('status') in ('queued','running'):
                self._enqueue(job['id'], 'reformat' if job.get('local_operation') == 'reformat' else 'compose')
            elif job.get('pending_operation') in ('sync','publish','verify'):
                self._enqueue(job['id'], job['pending_operation'])
            elif job.get('status') in ('submitted','reviewing','outcome_unknown') and not job.get('next_check_at'):
                self._schedule_check(job); self._record(job, 'verification_rescheduled')
        self.scheduler = threading.Thread(target=self._schedule_loop, name='platform-verification', daemon=True)
        self.scheduler.start()

    def _save(self, job):
        job['updated_at'] = screen.timestamp()
        screen.write_json(self.jobs_dir / (job['id'] + '.json'), job)

    def _record(self, job, action, detail=None):
        if job.get('pending_operation') == 'verify' and action in ('verified','remote_verified','verify_blocked','readback_blocked','readback_mismatch','draft_unresolved','verify_failed','failed'):
            job['pending_operation'] = None
        job['last_action'] = action
        job.setdefault('history', []).append({'at':screen.timestamp(), 'action':action, 'stage':job['stage'], 'status':job['status'], 'detail':detail})
        self._save(job)

    def _adapter(self, name, *args):
        fn = getattr(self.platform, name, None) if self.platform else None
        if fn is None:
            raise RuntimeError('平台能力尚未接入：' + name)
        return fn(*args)

    def snapshot(self):
        with self.lock:
            jobs = copy.deepcopy(list(self.jobs.values()))
        # Full material bodies are persisted for provenance, not sent on every poll.
        for job in jobs:
            if job.get('draft', {}).get('title') and job.get('draft', {}).get('html'):
                job['content_hash'] = composition.content_hash(job)
            else: job['content_hash'] = None
            draft = job.get('draft') or {}
            if not draft.get('body_html') and draft.get('html'):
                draft['body_html'] = composition.sanitize_body(draft['html'])
                draft['content_format'] = 'richtext'
            for source in job.get('packet', {}).get('materials', []):
                source.pop('content', None)
        try: accounts = self._adapter('list_accounts')
        except Exception: accounts = []
        try: capabilities = self._adapter('capabilities')
        except Exception: capabilities = {}
        return {'jobs':jobs, 'accounts':accounts, 'capabilities':capabilities, 'busy':bool(self.running)}

    def create_account(self, platform, display_name, mode='browser'):
        return self._adapter('create_account', platform, display_name, mode)

    def connect_account(self, account_id):
        return self._adapter('connect_account', account_id)

    def preview(self, platform, theme, body_html, presentation_mode=None):
        if not isinstance(body_html, str) or len(body_html) > 2_000_000: raise ValueError('正文内容无效或过大')
        if theme not in ('simple','default'): raise ValueError('不支持该排版主题')
        if presentation_mode not in (None,'toutiao_native') or (presentation_mode == 'toutiao_native' and platform != 'toutiao'): raise ValueError('不支持该平台排版模式')
        cleaned = composition.sanitize_body(body_html)
        return {'html':composition.render_body(cleaned, platform, theme, presentation_mode)}

    def create_jobs(self, batch_id, event_ids, targets):
        if not isinstance(event_ids, list) or not isinstance(targets, list):
            raise ValueError('需要选题和目标账号列表')
        source = self.candidate_provider(batch_id)
        if (source.get('batch') or {}).get('id') != batch_id:
            raise ValueError('批次已变化，请刷新')
        events = {e['id']:e for e in (source.get('review') or {}).get('events', []) if (e.get('comparison') or {}).get('selected') is True}
        items = {i['ref']:i for i in source.get('items', [])}
        accounts = self._adapter('list_accounts')
        if not isinstance(accounts, list): raise ValueError('无法读取平台账号')
        results = []
        for event_id in dict.fromkeys(event_ids):
            for target in targets:
                try:
                    if event_id not in events: raise ValueError('选题不是当前最终候选')
                    if not isinstance(target, dict) or target.get('platform') not in ('wechat', 'toutiao') or not isinstance(target.get('account_id'), str) or not target['account_id'].strip():
                        raise ValueError('目标平台或账号无效')
                    target = {'platform':target['platform'], 'account_id':target['account_id']}
                    account = next((a for a in accounts if a.get('id') == target['account_id'] and a.get('platform') == target['platform']), None)
                    if not account: raise ValueError('目标账号不存在或平台不匹配')
                    event = events[event_id]
                    ref_items = [copy.deepcopy(items[r]) for r in event['refs'] if r in items]
                    if not ref_items: raise ValueError('找不到选题原标题')
                    sources = copy.deepcopy(event.get('materials') or [])
                    if not any(m.get('status') == 'read' and m.get('content') for m in sources):
                        raise ValueError('选题缺少已读正文')
                    with self.lock:
                        existing = next((j for j in self.jobs.values() if j['batch_id'] == batch_id and j['event_id'] == event_id and j['target'] == target and j['status'] != 'cancelled'), None)
                        if existing: raise ValueError('该选题和账号已有任务')
                        job_id = uuid.uuid4().hex
                        now = screen.timestamp()
                        account_card = Path(__file__).resolve().parent.parent / 'accounts' / '首个账号.md'
                        guidance = account_card.read_text(encoding='utf-8') if account_card.exists() else ''
                        guidance = guidance.partition('## 方向与形式')[2].partition('## 工作流程与边界')[0]
                        packet = {'refs':event['refs'], 'items':ref_items, 'materials':sources, 'assessment':copy.deepcopy(event.get('assessment')), 'comparison':copy.deepcopy(event.get('comparison')), 'ranking':copy.deepcopy(event.get('ranking')),
                            'account':{k:account.get(k) for k in ('id','platform','display_name')}, 'account_guidance':account.get('guidance') or guidance}
                        job = {'id':job_id, 'batch_id':batch_id, 'event_id':event_id, 'source_title':ref_items[0].get('title') or '', 'target':target,
                            'packet':packet, 'stage':'queued', 'status':'queued', 'revision':1, 'created_at':now, 'updated_at':now,
                            'draft':{'title':'','summary':'','markdown':'','body_html':'','content_format':'richtext','html':'','theme':'simple',
                                'work_statements':['取材网络','引用AI'] if target['platform']=='toutiao' else None,
                                'first_publish':False if target['platform']=='toutiao' else None, 'original':False if target['platform']=='toutiao' else None,
                                'cover_url':None,'cover_sha256':None,'sources':[], 'title_review':None,'editorial_notes':[]},
                            'remote':None, 'approval':None, 'error':None, 'last_action':'created', 'history':[]}
                        self.jobs[job_id] = job
                        self._record(job, 'created')
                        self._enqueue(job_id, 'compose')
                    results.append({'event_id':event_id,'target':target,'job_id':job_id,'ok':True})
                except Exception as exc:
                    results.append({'event_id':event_id,'target':target,'ok':False,'error':str(exc)})
        return {'results':results}

    def _enqueue(self, job_id, operation):
        with self.lock:
            if job_id in self.running: raise ValueError('任务正在执行')
            self.running.add(job_id)
            cancel = self.cancels.setdefault(job_id, threading.Event())
            cancel.clear()
            if operation in ('compose', 'reformat'):
                self.executor.submit(self._worker, job_id, operation, cancel)
            else:
                job = self.jobs[job_id]; job['pending_operation'] = operation; self._record(job, 'queued_' + operation)
                key = self._account_key(job)
                if key is None:
                    self.running.discard(job_id)
                    job['requires_user_action'] = True; job['error'] = '无法确认目标账号身份，等待重新连接'
                    self._record(job, 'account_identity_required', job['error'])
                    return
                self.job_account_keys[job_id] = key
                self.platform_queues.setdefault(key, []).append((job_id,operation,cancel))
                self._dispatch_account(key)

    def _account_key(self, job):
        target = job['target']
        try: account = next((a for a in self._adapter('list_accounts') if a.get('id') == target['account_id'] and a.get('platform') == target['platform']), None)
        except Exception: account = None
        identity = ((account or {}).get('identity') or {}).get('key')
        return (target['platform'], identity) if identity else None

    def _dispatch_account(self, key):
        queue = self.platform_queues.get(key) or []
        if key not in self.active_accounts and queue:
            self.active_accounts.add(key)
            job_id, operation, cancel = queue.pop(0)
            self.platform_executor.submit(self._worker, job_id, operation, cancel)

    def _schedule_check(self, job):
        attempt = int(job.get('check_attempts') or 0)
        delay = min(900, 30 * (2 ** min(attempt, 5)))
        job['next_check_at'] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec='seconds')
        job['check_attempts'] = attempt + 1

    def _schedule_loop(self):
        while not self.scheduler_stop.wait(5):
            with self.lock:
                now = datetime.now(timezone.utc)
                for job in self.jobs.values():
                    pending = job.get('pending_operation')
                    if pending in ('sync','publish','verify') and job['id'] not in self.running:
                        if self._account_key(job) is not None:
                            job['requires_user_action'] = False; job['error'] = None
                            self._enqueue(job['id'], pending)
                        continue
                    if job.get('status') not in ('submitted','reviewing','outcome_unknown') or not job.get('next_check_at') or job['id'] in self.running:
                        continue
                    try: due = datetime.fromisoformat(job['next_check_at']) <= now
                    except (ValueError,TypeError): due = False
                    if due:
                        self._schedule_check(job); self._record(job, 'scheduled_verify')
                        self._enqueue(job['id'], 'verify')

    def _worker(self, job_id, operation, cancel):
        try:
            if operation == 'compose': self._compose(job_id, cancel)
            elif operation == 'reformat': self._reformat(job_id, cancel)
            else: self._external(job_id, operation)
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                if job['status'] == 'cancelled': return
                previous = job['stage']
                if operation == 'verify' and job['status'] == 'published':
                    job['error'] = '已发布记录保留；本次补充核验出错：' + str(exc)[:800]
                    job['next_check_at'] = None; self._record(job, 'verify_failed', job['error']); return
                if operation == 'verify' and job['status'] in ('submitted','reviewing') and not job.get('operation_uncertain'):
                    job['error'] = str(exc)[:1000]; self._schedule_check(job); self._record(job, 'verify_failed', job['error']); return
                uncertain = previous in EXTERNAL_STAGES or job.get('operation_uncertain', False)
                if uncertain:
                    job['operation_uncertain'] = True; self._schedule_check(job)
                job.update(stage='needs_attention' if uncertain else previous,
                    status='outcome_unknown' if uncertain else 'failed', error=str(exc)[:1000], failed_stage=previous)
                self._record(job, 'failed', job['error'])
        finally:
            with self.lock:
                self.running.discard(job_id)
                if operation not in ('compose', 'reformat'):
                    job = self.jobs[job_id]
                    if job.get('pending_operation') == operation:
                        job['pending_operation'] = None; self._save(job)
                    key = self.job_account_keys.pop(job_id)
                    self.active_accounts.discard(key); self._dispatch_account(key)

    def _phase(self, job, stage):
        if job['status'] == 'cancelled': raise RuntimeError('已取消')
        job.update(stage=stage, status='running', error=None)
        self._record(job, stage)

    def _compose(self, job_id, cancel):
        with self.lock:
            job = self.jobs[job_id]
            stage = job['stage']
            if stage == 'queued': stage = 'researching'
            if stage == 'checking': stage = 'editing'
        call_root = self.jobs_dir / job_id
        if stage == 'researching':
            with self.lock: self._phase(job, 'researching'); packet = copy.deepcopy(job['packet']); packet['source_title'] = job['source_title']
            result = composition.research(call_root, packet, cancel)
            with self.lock:
                if cancel.is_set(): return
                job['packet']['materials'] = result['materials']; job['packet']['research_unknowns'] = result['unknowns']
                self._phase(job, 'writing')
            stage = 'writing'
        if stage == 'writing':
            with self.lock: self._phase(job, 'writing'); view = copy.deepcopy(job)
            value = composition.write(call_root, view, cancel)
            with self.lock:
                if cancel.is_set(): return
                job['writing'] = value; self._phase(job, 'editing')
            stage = 'editing'
        if stage == 'editing':
            with self.lock: self._phase(job, 'editing'); view = copy.deepcopy(job)
            value = composition.edit(call_root, view, cancel)
            with self.lock:
                if cancel.is_set(): return
                job['edited'] = value; self._phase(job, 'formatting')
            stage = 'formatting'
        if stage == 'formatting':
            with self.lock: self._phase(job, 'formatting'); edited = copy.deepcopy(job['edited'])
            previous = copy.deepcopy(job.get('draft') or {})
            keep_cover = bool(previous.get('cover_url')) and (previous.get('cover_kind') == 'uploaded' or previous.get('title') == edited['title'])
            if keep_cover:
                cover_url = previous['cover_url']; cover_sha = previous.get('cover_sha256'); cover_kind = previous.get('cover_kind')
            else:
                cover = self.media_dir / (job_id + '-' + uuid.uuid4().hex + '.png')
                cover_sha = composition.make_cover(cover, edited['title']); cover_url = '/api/production/media/' + cover.name; cover_kind = 'generated'
            with self.lock:
                if cancel.is_set(): return
                reason = edited.get('title_reason') or job['writing']['title_reason']
                if job['target']['platform'] == 'toutiao': reason = '头条文章标题须为2至30字。' + reason
                title_review = {'original':job['source_title'], 'selected':edited['title'], 'alternative':job['writing']['alternative_title'], 'reason':reason, 'angle':job['writing']['angle']}
                sources = [{k:m.get(k) for k in ('url','final_url','title','publisher','published_at','content_sha256')} for m in job['packet']['materials'] if m.get('status') == 'read']
                markdown = composition.strip_repeated_title(edited['markdown'],edited['title'])
                theme = (job.get('draft') or {}).get('theme') or 'simple'
                body = composition.import_markdown(markdown)
                mode = previous.get('presentation_mode') or ('toutiao_native' if job['target']['platform']=='toutiao' else None)
                job['draft'] = {'title':edited['title'], 'summary':edited['summary'], 'markdown':markdown, 'body_html':body, 'content_format':'richtext', 'html':composition.render_body(body,job['target']['platform'],theme,mode), 'theme':theme,'presentation_mode':mode,
                    'work_statements':previous.get('work_statements'), 'first_publish':previous.get('first_publish'), 'original':previous.get('original'),
                    'cover_url':cover_url, 'cover_sha256':cover_sha, 'cover_kind':cover_kind, 'sources':sources, 'title_review':title_review, 'editorial_notes':edited['editorial_notes'], 'fact_checks':edited['fact_checks'], 'unresolved':edited['unresolved'],
                    'fact_check_scope':'系统确认所列断言及原文摘录分别存在，关联和正文覆盖由模型判断，仍需审稿核对，不能证明所有事实已核实。'}
                job.update(stage='ready_review', status='ready_review', error=None)
                self._record(job, 'ready_review')

    def _reformat(self, job_id, cancel):
        with self.lock:
            job = self.jobs[job_id]; self._phase(job, 'formatting')
            draft = copy.deepcopy(job['draft']); platform = job['target']['platform']
        body = composition.sanitize_body(draft.get('body_html') or draft.get('html') or '')
        if not composition.semantic_html(body)['paragraphs']: raise ValueError('稿件没有正文')
        theme = draft.get('theme') or 'simple'
        rendered = composition.render_body(body, platform, theme, draft.get('presentation_mode'))
        with self.lock:
            if cancel.is_set(): return
            draft.update(body_html=body, content_format='richtext', html=rendered, theme=theme)
            job['draft'] = draft; job['local_operation'] = None
            job.update(stage='ready_review', status='ready_review', error=None)
            self._record(job, 'ready_review')

    def update_job(self, job_id, changes, expected_revision):
        if not isinstance(changes, dict) or set(changes) - {'title','summary','body_html','cover_url','cover_sha256','theme','work_statements','first_publish','original','target_account_id','presentation_mode'}:
            raise ValueError('不支持修改这些字段')
        with self.lock:
            job = self._get(job_id, expected_revision)
            editable = job['stage'] in ('ready_review','remote_verified','needs_attention') or (job['stage'] == 'failed' and job.get('external_operation') == 'publish' and (job.get('remote') or {}).get('status') == 'failed')
            if job_id in self.running or job.get('pending_operation') or not editable or job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','unknown','outcome_unknown'):
                raise ValueError('当前状态不可修改')
            draft = copy.deepcopy(job['draft']); changed = False; draft_changed = False
            old_target = copy.deepcopy(job['target']); new_target = copy.deepcopy(old_target)
            for key, value in changes.items():
                if key == 'target_account_id':
                    if not isinstance(value, str) or not value.strip(): raise ValueError('请选择有效的目标账号')
                    accounts = self._adapter('list_accounts')
                    account = next((a for a in accounts if a.get('id') == value and a.get('platform') == old_target['platform']), None)
                    if not account: raise ValueError('目标账号不存在或与当前平台不匹配')
                    if value != old_target['account_id']:
                        if job.get('reconcile_before_create'): raise ValueError('该稿仍需在原账号核验可能延迟出现的草稿，不能换账号')
                        if any(h.get('status') in ('submitted','reviewing','published') or h.get('stage') in ('submitted','reviewing','published') for h in job.get('history', [])):
                            raise ValueError('已有平台提交记录，请复制为新稿后选择账号')
                        if any(other['id'] != job_id and other['batch_id'] == job['batch_id'] and other['event_id'] == job['event_id'] and other.get('target') == {'platform':old_target['platform'], 'account_id':value} and other.get('status') != 'cancelled' for other in self.jobs.values()):
                            raise ValueError('该选题在目标账号已有任务，请查看现有任务')
                        new_target['account_id'] = value; changed = True
                    continue
                if key == 'work_statements':
                    if not isinstance(value, list) or any(not isinstance(x, str) or x not in ('取材网络','引用AI') for x in value) or len(value) != len(set(value)): raise ValueError('作品声明无效')
                    value = [x for x in ('取材网络','引用AI') if x in value]
                elif key in ('first_publish','original'):
                    if value is not False: raise ValueError('首发和原创目前只能明确关闭')
                elif not isinstance(value, str): raise ValueError('稿件字段必须为文本')
                if key in ('title','body_html') and not value.strip(): raise ValueError('标题及正文不能为空')
                if key == 'title' and job['target']['platform'] == 'toutiao' and not 2 <= composition.title_units(value.strip()) <= 30: raise ValueError('头条文章标题须为2至30字')
                if key == 'theme' and value not in ('simple','default'): raise ValueError('不支持该排版主题')
                if key == 'presentation_mode' and (value != 'toutiao_native' or old_target['platform'] != 'toutiao'): raise ValueError('不支持该平台排版模式')
                if key in ('cover_url','cover_sha256'): raise ValueError('封面须通过受限上传接口替换')
                if draft.get(key) != value: draft[key] = value; changed = draft_changed = True
            if changed:
                if draft_changed:
                    original_body = draft.get('body_html') or draft.get('html') or ''
                    original_images = [x['src'] for x in composition.semantic_html(original_body)['images']]
                    draft['body_html'] = composition.sanitize_body(original_body)
                    if original_images != [x['src'] for x in composition.semantic_html(draft['body_html'])['images']]: raise ValueError('正文图片地址不受支持，请重新选择图片')
                    if not composition.semantic_html(draft['body_html'])['paragraphs']: raise ValueError('稿件没有正文')
                    image_hashes = {}
                    for item in composition.semantic_html(draft['body_html'])['images']:
                        src = item['src']
                        if src.startswith('/api/production/media/'):
                            name = src.removeprefix('/api/production/media/')
                            if not name.startswith(job_id + '-') or not self.media_path(name).is_file(): raise ValueError('正文图片不属于该稿件或不存在')
                            image_hashes[src] = hashlib.sha256(self.media_path(name).read_bytes()).hexdigest()
                    draft['body_image_hashes'] = image_hashes
                    draft['content_format'] = 'richtext'
                    draft['theme'] = draft.get('theme') or 'simple'
                    draft['html'] = composition.render_body(draft['body_html'],job['target']['platform'],draft['theme'],draft.get('presentation_mode'))
                    if draft.get('cover_kind') == 'generated' and draft['title'] != job['draft']['title']:
                        cover = self.media_dir / (job_id + '-' + uuid.uuid4().hex + '.png')
                        draft['cover_sha256'] = composition.make_cover(cover, draft['title'])
                        draft['cover_url'] = '/api/production/media/' + cover.name
                version_path = None
                if new_target != old_target or draft.get('presentation_mode') != job['draft'].get('presentation_mode'):
                    versions = self.root / 'versions' / job_id; versions.mkdir(parents=True,exist_ok=True)
                    version_path = versions / (str(job['revision']) + '-' + uuid.uuid4().hex + '.json')
                    screen.write_json(version_path, job)
                job['draft'] = draft
                job['revision'] += 1; job['approval'] = None
                if new_target != old_target:
                    job['target'] = new_target; job['remote'] = None
                    job['operation_id'] = None; job['external_operation'] = None; job['next_check_at'] = None
                    job['requires_user_action'] = False; job['check_attempts'] = 0
                elif job['remote']: job['remote']['verified_hash'] = None
                job.update(stage='ready_review',status='ready_review',error=None)
                self._record(job, 'target_changed' if new_target != old_target else 'edited',
                    {'old_target':old_target,'new_target':new_target,'previous_version':str(version_path)} if version_path else None)
            return copy.deepcopy(job)

    def update_cover(self, job_id, content, expected_revision):
        """Store an explicitly supplied, decoded image as a local PNG."""
        from io import BytesIO
        from PIL import Image, ImageOps, UnidentifiedImageError
        if not isinstance(content, bytes) or not content or len(content) > 10_000_000:
            raise ValueError('图片大小须在 10 MB 以内')
        try:
            image_file = Image.open(BytesIO(content))
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError('无法读取图片，请选择有效的 PNG 或 JPEG') from exc
        with image_file as image:
            if image.format not in ('PNG','JPEG') or image.width * image.height > 24_000_000:
                raise ValueError('仅支持不超过 2400 万像素的 PNG 或 JPEG')
            image = ImageOps.exif_transpose(image).convert('RGB')
            with self.lock:
                job = self._get(job_id, expected_revision)
                editable = job['stage'] in ('ready_review','remote_verified','needs_attention') or (job['stage'] == 'failed' and job.get('external_operation') == 'publish' and (job.get('remote') or {}).get('status') == 'failed')
                if job_id in self.running or job.get('pending_operation') or not editable or job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','outcome_unknown'):
                    raise ValueError('当前状态不可修改封面')
                path = self.media_dir / (job_id + '-' + uuid.uuid4().hex + '.png')
                image.save(path, format='PNG')
                draft = job['draft']; draft['cover_url'] = '/api/production/media/' + path.name
                draft['cover_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                draft['cover_kind'] = 'uploaded'
                job['revision'] += 1; job['approval'] = None
                if job['remote']: job['remote']['verified_hash'] = None
                job.update(stage='ready_review',status='ready_review',error=None)
                self._record(job, 'cover_changed')
                return copy.deepcopy(job)

    def upload_body_image(self, job_id, content, expected_revision):
        """Store an explicitly supplied image; inserting it into the draft is a separate edit."""
        from io import BytesIO
        from PIL import Image, ImageOps, UnidentifiedImageError
        if not isinstance(content, bytes) or not content or len(content) > 10_000_000:
            raise ValueError('图片大小须在 10 MB 以内')
        try: image_file = Image.open(BytesIO(content))
        except (UnidentifiedImageError, OSError) as exc: raise ValueError('无法读取图片，请选择有效的 PNG 或 JPEG') from exc
        with image_file as image:
            if image.format not in ('PNG','JPEG') or image.width * image.height > 24_000_000:
                raise ValueError('仅支持不超过 2400 万像素的 PNG 或 JPEG')
            image = ImageOps.exif_transpose(image).convert('RGB')
            with self.lock:
                job = self._get(job_id, expected_revision)
                if job_id in self.running or job.get('pending_operation') or job['stage'] not in ('ready_review','remote_verified','needs_attention') or job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','outcome_unknown'):
                    raise ValueError('当前状态不可上传正文图片')
                path = self.media_dir / (job_id + '-' + uuid.uuid4().hex + '.png')
                image.save(path, format='PNG')
                return {'url':'/api/production/media/' + path.name}

    def media_path(self, name):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.png'):
            raise ValueError('图片路径无效')
        path = self.media_dir / name
        if not path.is_file(): raise ValueError('图片不存在')
        return path

    def _get(self, job_id, expected_revision):
        job = self.jobs.get(job_id)
        if not job: raise ValueError('任务不存在')
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or job['revision'] != expected_revision: raise ValueError('稿件版本已变化，请刷新')
        return job

    def recover_pre_save(self, job_id, operation_id, expected_revision):
        """Resolve one evidenced pre-save sync without inferring media-upload outcome."""
        if not isinstance(operation_id, str) or len(operation_id) != 32 or any(c not in '0123456789abcdef' for c in operation_id):
            raise ValueError('操作标识无效')
        evidence_path = self.root / 'evidence' / (operation_id + '.json')
        if not evidence_path.is_file(): raise ValueError('缺少对应操作的核验证据')
        raw = evidence_path.read_bytes()
        if len(raw) > 32768: raise ValueError('证据内容过大')
        try: evidence = json.loads(raw)
        except (ValueError,UnicodeDecodeError) as exc: raise ValueError('证据格式无效') from exc
        with self.lock:
            job = self._get(job_id, expected_revision)
            if job_id in self.running or job.get('pending_operation'): raise ValueError('任务仍在执行或等待核验')
            if (job['target']['platform'] != 'wechat' or job.get('external_operation') != 'sync'
                or job.get('operation_id') != operation_id or not job.get('operation_uncertain')
                or job.get('status') != 'outcome_unknown' or job.get('approval')
                or (job.get('remote') or {}).get('draft_id')):
                raise ValueError('任务不是该次待核验的公众号草稿操作')
            if any(h.get('status') in ('submitted','reviewing','published') or h.get('stage') in ('submitted','reviewing','published') for h in job.get('history',[])):
                raise ValueError('任务有平台提交记录，不能按草稿前失败恢复')
            history = job.get('history') or []
            last_sync = next((i for i in range(len(history)-1,-1,-1) if history[i].get('action')=='syncing'), None)
            if last_sync is None: raise ValueError('缺少同步阶段记录')
            failed = next((h for h in history[last_sync+1:] if h.get('action')=='sync_blocked' and h.get('status')=='outcome_unknown' and '阶段=upload_cover' in str(h.get('detail') or '')), None)
            if not failed: raise ValueError('没有匹配的封面上传阶段失败记录')
            account = next((a for a in self._adapter('list_accounts') if a.get('id') == job['target']['account_id'] and a.get('platform') == 'wechat'), None)
            identity = ((account or {}).get('identity') or {}).get('key')
            if (not identity or evidence.get('evidence_type') != 'operator_observation_not_raw_capture'
                or evidence.get('operation_id') != operation_id or evidence.get('account_id') != job['target']['account_id']
                or evidence.get('account_identity_key') != identity or evidence.get('observed_identity_matched') is not True
                or evidence.get('target_title') != job['draft']['title'] or evidence.get('target_title_visible') is not False
                or evidence.get('draft_box_displayed') != '暂无素材' or evidence.get('read_only') is not True
                or evidence.get('prior_failure_phase') != 'upload_cover' or evidence.get('session_tabs_closed') is not True):
                raise ValueError('操作证据或账号身份不匹配')
            try:
                if datetime.fromisoformat(evidence['recorded_at_utc']) < datetime.fromisoformat(failed['at']):
                    raise ValueError('核验证据早于同步失败')
            except (KeyError,TypeError) as exc: raise ValueError('核验证据缺少时间') from exc
            backups = self.root / 'recovery-evidence'; backups.mkdir(parents=True,exist_ok=True)
            backup = backups / (job_id + '-' + operation_id + '-before.json')
            if backup.exists(): raise ValueError('该操作已有恢复记录')
            screen.write_json(backup, copy.deepcopy(job))
            job.update(stage='ready_review',status='failed',operation_uncertain=False,pending_operation=None,
                next_check_at=None,remote=None,failed_stage='syncing',error='草稿保存步骤未触发；可继续同步。封面素材是否上传未知。')
            self._record(job, 'sync_pre_save_recovered', {'operation_id':operation_id,'phase':'upload_cover',
                'evidence_type':evidence['evidence_type'],'evidence_sha256':hashlib.sha256(raw).hexdigest(),
                'evidence_path':str(evidence_path),'backup':str(backup),'media_upload':'unknown',
                'explanation':'保存步骤尚未触发；同账号草稿箱只读显示为空。该观察为人工记录，不是原始页面捕获。'})
            return copy.deepcopy(job)

    def recover_toutiao_absent(self, job_id, operation_id, expected_revision):
        """Resolve this evidenced draft-only unknown; next sync must reconcile first."""
        if operation_id != '48f17e40efd346e8b74cd9006145ba3d':
            raise ValueError('该操作不在已授权的恢复范围内')
        evidence_path = self.root / 'evidence' / (operation_id + '.json')
        if not evidence_path.is_file(): raise ValueError('缺少对应操作的核验证据')
        raw=evidence_path.read_bytes()
        if len(raw)>32768: raise ValueError('证据内容过大')
        try: evidence=json.loads(raw)
        except (ValueError,UnicodeDecodeError) as exc: raise ValueError('证据格式无效') from exc
        with self.lock:
            job=self._get(job_id,expected_revision)
            if job_id in self.running or job.get('pending_operation'): raise ValueError('任务仍在执行或等待核验')
            if (job['target']['platform']!='toutiao' or job.get('external_operation')!='sync' or job.get('operation_id')!=operation_id
                or job.get('status')!='outcome_unknown' or not job.get('operation_uncertain') or job.get('approval') or (job.get('remote') or {}).get('draft_id')):
                raise ValueError('任务不是该次待核验的头条草稿操作')
            history=job.get('history') or []
            if any(h.get('status') in ('submitted','reviewing','published') or h.get('stage') in ('submitted','reviewing','published') for h in history):
                raise ValueError('已有平台提交记录，不能执行草稿恢复')
            last_sync=next((i for i in range(len(history)-1,-1,-1) if history[i].get('action')=='syncing'),None)
            if last_sync is None: raise ValueError('缺少同步阶段记录')
            failed=next((h for h in history[last_sync+1:] if h.get('action')=='sync_blocked' and h.get('status')=='outcome_unknown'
                and '编辑器未完整保留富文本或排版' in str(h.get('detail') or '')),None)
            if not failed: raise ValueError('同步失败阶段不匹配')
            account=next((a for a in self._adapter('list_accounts') if a.get('id')==job['target']['account_id'] and a.get('platform')=='toutiao'),None)
            identity=((account or {}).get('identity') or {}).get('key')
            pages=evidence.get('pages')
            if (not identity or evidence.get('evidence_type')!='operator_observation_not_raw_capture'
                or evidence.get('operation_id')!=operation_id or evidence.get('account_id')!=job['target']['account_id']
                or (evidence.get('account_identity') or {}).get('key')!=identity or evidence.get('identity_match') is not True
                or evidence.get('target_title')!=job['draft']['title'] or evidence.get('response_code')!=0
                or evidence.get('matches')!=[] or evidence.get('exhausted') is not True or evidence.get('read_only') is not True
                or evidence.get('session_cleanup')!='clean' or not isinstance(pages,list) or len(pages)!=1
                or pages[0].get('page')!=1 or pages[0].get('count')!=0 or (pages[0].get('metadata') or {}).get('count')!=0):
                raise ValueError('头条草稿观察证据或账号不匹配')
            try:
                failed_at=datetime.fromisoformat(failed['at']); checked=datetime.fromisoformat(evidence['checked_at'])
                if datetime.fromisoformat(evidence['failure_at'])!=failed_at or (checked-failed_at).total_seconds()<300:
                    raise ValueError('头条草稿观察时间不足或与失败记录不符')
            except (KeyError,TypeError) as exc: raise ValueError('头条草稿观察缺少时间') from exc
            backups=self.root/'recovery-evidence'; backups.mkdir(parents=True,exist_ok=True)
            backup=backups/(job_id+'-'+operation_id+'-before.json')
            if backup.exists(): raise ValueError('该操作已有恢复记录')
            screen.write_json(backup,copy.deepcopy(job))
            job.update(stage='ready_review',status='failed',operation_uncertain=False,pending_operation=None,
                next_check_at=None,remote=None,failed_stage='syncing',reconcile_before_create=True,reconcile_operation_id=operation_id,
                error='核验时未见草稿；再次同步前须按账号、标题和正文核对，不能排除延迟写入。')
            self._record(job,'toutiao_absent_recovered',{'operation_id':operation_id,'evidence_type':evidence['evidence_type'],
                'evidence_sha256':hashlib.sha256(raw).hexdigest(),'evidence_path':str(evidence_path),'backup':str(backup),
                'explanation':'同账号草稿列表核验时为空；仅为人工记录，不证明将来不会出现延迟写入。下一次同步必须先核对已有草稿。'})
            return copy.deepcopy(job)

    def action(self, job_ids, action, expected_revisions, expected_approvals=None):
        if not isinstance(job_ids, list) or not isinstance(expected_revisions, dict): raise ValueError('需要任务和版本列表')
        if action not in ('sync','confirm_publish','verify','retry','cancel','redo_writing','redo_editing','redo_formatting'): raise ValueError('不支持该操作')
        results = []
        for job_id in dict.fromkeys(job_ids):
            try:
                with self.lock:
                    job = self._get(job_id, expected_revisions.get(job_id))
                    if action == 'cancel':
                        if job.get('pending_operation') or job['stage'] in EXTERNAL_STAGES or job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','outcome_unknown'):
                            raise ValueError('平台操作进行中或已提交，不能取消')
                        self.cancels.setdefault(job_id, threading.Event()).set(); job['approval'] = None
                        job.update(stage='cancelled', status='cancelled', error=None); self._record(job, 'cancelled')
                    else:
                        if job_id in self.running: raise ValueError('任务正在执行')
                        if job.get('pending_operation'): raise ValueError('平台操作排队中，请等待账号连接或核验')
                        if action == 'sync':
                            if job['stage'] not in ('ready_review','remote_verified','needs_attention'): raise ValueError('稿件尚未准备好同步')
                            if job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','unknown','outcome_unknown'): raise ValueError('已有提交或未知结果，请先核验')
                            if job['stage'] == 'needs_attention' and (job.get('remote') or {}).get('status') == 'saved': raise ValueError('已有平台草稿，请先核验草稿')
                            account = next((a for a in self._adapter('list_accounts') if a.get('id') == job['target']['account_id']), None)
                            if not account or account.get('status') != 'connected' or not (account.get('identity') or {}).get('key'): raise ValueError('请先连接并核验目标账号')
                            job['approval'] = None
                            if job.get('remote'): job['remote']['verified_hash'] = None
                            self._record(job, 'sync_requested')
                        elif action == 'confirm_publish':
                            if job.get('operation_uncertain') or job['stage'] != 'remote_verified' or not job.get('remote') or job['remote'].get('verified_hash') != composition.content_hash(job): raise ValueError('请先同步并核对当前版本的平台草稿')
                            remote = job['remote']
                            if not remote.get('fingerprint') or not remote.get('verified_identity') or not remote.get('draft_id'): raise ValueError('平台草稿和实际账号尚未完整核验')
                            approved = {'revision':job['revision'], 'content_hash':composition.content_hash(job), 'account_id':job['target']['account_id'],
                                'account_identity':remote['verified_identity'], 'draft_id':remote['draft_id'], 'remote_fingerprint':remote['fingerprint']}
                            if not isinstance(expected_approvals,dict) or expected_approvals.get(job_id) != approved: raise ValueError('确认时展示的稿件、账号或平台草稿已变化，请重新查看')
                            job['approval'] = dict(approved, approved_at=screen.timestamp())
                            job['pending_operation'] = 'publish'
                            self._record(job, 'confirmed')
                        elif action == 'verify':
                            if not job.get('remote') and job['status'] != 'outcome_unknown' and not job.get('operation_uncertain'): raise ValueError('没有待核验的平台记录')
                        elif action == 'retry':
                            if job['status'] != 'failed' or job.get('failed_stage') not in LOCAL_STAGES: raise ValueError('只能重试失败的本地阶段；平台未知结果须先核验')
                            job['stage'] = job['failed_stage']; job['status'] = 'queued'; job['error'] = None; self._record(job, 'retry')
                        elif action.startswith('redo_'):
                            editable = job['stage'] in ('ready_review','remote_verified','needs_attention') or (job['stage'] == 'failed' and job.get('external_operation') == 'publish' and (job.get('remote') or {}).get('status') == 'failed')
                            if not editable or job.get('operation_uncertain') or job['status'] == 'outcome_unknown' or (job.get('remote') or {}).get('status') in ('submitted','reviewing','published','unknown','outcome_unknown'):
                                raise ValueError('当前状态不能重做稿件')
                            part = action[5:]
                            if part == 'editing' and not job.get('writing'): raise ValueError('没有初稿')
                            if part == 'formatting' and not ((job.get('draft') or {}).get('body_html') or (job.get('draft') or {}).get('html')): raise ValueError('没有可排版的正文')
                            if part in ('writing','editing'):
                                body = (job.get('draft') or {}).get('body_html') or composition.sanitize_body(job['draft'].get('html') or '')
                                composition.require_rewritable_body(body)
                                job['draft']['body_html'] = body; job['draft']['content_format'] = 'richtext'
                            job['revision'] += 1; job['approval'] = None
                            if job['remote']: job['remote']['verified_hash'] = None
                            job['local_operation'] = 'reformat' if part == 'formatting' else 'compose'
                            job.update(stage=part,status='queued',error=None); self._record(job, action)
                        operation = {'retry':'reformat' if job.get('local_operation') == 'reformat' else 'compose','confirm_publish':'publish','redo_writing':'compose','redo_editing':'compose','redo_formatting':'reformat'}.get(action, action)
                        self._enqueue(job_id, operation)
                results.append({'job_id':job_id,'ok':True})
            except Exception as exc: results.append({'job_id':job_id,'ok':False,'error':str(exc)})
        return {'results':results}

    def _remote_matches(self, job, actual):
        draft = job['draft']; remote = job['remote']
        identity = (actual.get('account_identity') or {}).get('key')
        saved_identity = (remote.get('account_identity') or {}).get('key')
        account = next((a for a in self._adapter('list_accounts') if a.get('id') == job['target']['account_id']), None)
        bound_identity = ((account or {}).get('identity') or {}).get('key')
        if not identity or identity != saved_identity or identity != bound_identity: return False
        if actual.get('draft_id') != remote.get('draft_id') or not remote.get('draft_id'): return False
        if not remote.get('cover_media_id') or actual.get('cover_media_id') != remote['cover_media_id'] or remote.get('cover_sha256') != draft['cover_sha256']: return False
        if composition.normalize_text(actual.get('title')) != composition.normalize_text(draft['title']): return False
        capabilities = (account or {}).get('capabilities') or {}
        supports_summary = capabilities.get('supports_summary')
        if supports_summary is not False and ('summary' not in actual or composition.normalize_text(actual.get('summary')) != composition.normalize_text(draft['summary'])): return False
        if job['target']['platform'] == 'toutiao':
            if not isinstance(draft.get('work_statements'), list) or actual.get('work_statements') != draft['work_statements']: return False
            if draft.get('first_publish') is not False or actual.get('first_publish') is not False or actual.get('publish_micro') is not False: return False
            if draft.get('original') is not False: return False
            if 'original' not in actual: return False
            if actual.get('original') is not False and not (capabilities.get('supports_original') is False and actual.get('original') is None): return False
        local = composition.semantic_html(draft['html']); observed = composition.semantic_html(actual.get('html'))
        image_map = remote.get('image_map') or {}
        mapped = [dict(image, src=image_map.get(image['src'], image['src'])) for image in local['images']]
        return not local['unsafe'] and not observed['unsafe'] and local['paragraphs'] == observed['paragraphs'] and local['links'] == observed['links'] and mapped == observed['images'] and local['table_events'] == observed['table_events'] and composition.presentation_preserved(draft['html'], actual.get('html'))

    def _snapshot(self, job):
        draft = job['draft']
        if job['target']['platform'] == 'toutiao' and not 2 <= composition.title_units(draft['title'].strip()) <= 30:
            raise ValueError('头条文章标题须为2至30字，请先修改')
        cover = self.media_path((draft.get('cover_url') or '').removeprefix('/api/production/media/'))
        if hashlib.sha256(cover.read_bytes()).hexdigest() != draft['cover_sha256']:
            raise ValueError('封面内容已变化，请重新设置')
        image_hashes = draft.get('body_image_hashes') or {}
        body_images = {}
        for image in composition.semantic_html(draft.get('body_html') or draft.get('html') or '')['images']:
            if image['src'] not in image_hashes:
                raise ValueError('正文图片尚未绑定本地文件，请上传有权使用的图片后再同步')
        for src, digest in image_hashes.items():
            if not src.startswith('/api/production/media/'):
                raise ValueError('正文图片路径无效')
            name = src.removeprefix('/api/production/media/')
            image_path = self.media_path(name)
            if (not name.startswith(job['id'] + '-') or image_path.is_symlink() or image_path.resolve().parent != self.media_dir.resolve()
                or hashlib.sha256(image_path.read_bytes()).hexdigest() != digest):
                raise ValueError('正文图片内容已变化，请重新保存稿件')
            body_images[src] = {'path':str(image_path.resolve()), 'sha256':digest}
        if job['target']['platform'] == 'toutiao' and (not isinstance(draft.get('work_statements'), list) or draft.get('first_publish') is not False or draft.get('original') is not False):
            raise ValueError('请确认头条作品声明，首发和原创目前只支持关闭')
        return {'revision':job['revision'], 'title':draft['title'], 'summary':draft['summary'], 'body_html':draft.get('body_html'), 'content_format':'richtext', 'html':draft['html'], 'presentation_mode':draft.get('presentation_mode'),
            'work_statements':copy.deepcopy(draft.get('work_statements')), 'first_publish':draft.get('first_publish'), 'original':draft.get('original'), 'body_images':body_images,
            'reconcile_before_create':bool(job.get('reconcile_before_create')), 'reconcile_operation_id':job.get('reconcile_operation_id'),
            'cover_path':str(cover.resolve()), 'cover_sha256':draft['cover_sha256'], 'content_hash':composition.content_hash(job),
            'platform':job['target']['platform'], 'account_id':job['target']['account_id']}

    def _blocked(self, job, result, action):
        status = result.get('status') or 'needs_attention'
        status = 'outcome_unknown' if status == 'unknown' else status
        pending_status = job.get('status')
        if status == 'outcome_unknown':
            job['operation_uncertain'] = True; self._schedule_check(job)
        elif job.get('operation_uncertain'):
            job['requires_user_action'] = status in ('needs_login','needs_attention'); status = 'outcome_unknown'; self._schedule_check(job)
        elif action.startswith(('verify_', 'draft_')) and pending_status in ('submitted','reviewing'):
            job['requires_user_action'] = status in ('needs_login','needs_attention'); status = pending_status; self._schedule_check(job)
        if result.get('draft_id'):
            if job.get('remote') is None: job['remote'] = {}
            for key in ('draft_id','url','evidence','account_identity','cover_media_id','cover_sha256','image_map'):
                if key in result: job['remote'][key] = result[key]
        if isinstance(result.get('evidence_paths'), list):
            job['failure_evidence_paths'] = [p for p in result['evidence_paths'] if isinstance(p, str)]
        job.update(stage='needs_attention',status=status,error=str(result.get('error') or result.get('message') or '平台未完成操作'))
        self._record(job, action, job['error'])

    def _external(self, job_id, operation):
        with self.lock:
            job = self.jobs[job_id]
            account_lock = self.account_locks.setdefault(self.job_account_keys[job_id], threading.Lock())
        with account_lock:
            if operation == 'sync':
                with self.lock:
                    snapshot = self._snapshot(job); existing = copy.deepcopy(job.get('remote'))
                    if job.get('reconcile_before_create'):
                        account = next((a for a in self._adapter('list_accounts') if a.get('id') == job['target']['account_id']), None)
                        if not account or (account.get('capabilities') or {}).get('reconcile_before_create') is not True:
                            raise ValueError('当前平台适配器尚未启用同步前旧草稿核对，不能新建')
                    job['operation_id'] = uuid.uuid4().hex; job['external_operation'] = 'sync'; job['pending_operation'] = None; self._phase(job, 'syncing')
                    snapshot['operation_id'] = job['operation_id']
                result = self._adapter('save_draft', job['target']['account_id'], snapshot, existing)
                with self.lock:
                    if result.get('status') != 'saved': self._blocked(job, result, 'sync_blocked'); return
                    if not result.get('draft_id'): raise RuntimeError('平台没有返回可核验的草稿标识')
                    job['remote'] = dict(result, verified_hash=None); self._record(job, 'draft_created')
                    remote = copy.deepcopy(job['remote'])
                actual = self._adapter('inspect_draft', job['target']['account_id'], remote)
                with self.lock:
                    if actual.get('status') != 'ok': self._blocked(job, actual, 'readback_blocked'); return
                    if not self._remote_matches(job, actual):
                        self._blocked(job, {'status':'needs_attention','error':'平台草稿回读与当前稿件或账号不一致'}, 'readback_mismatch'); return
                    job['remote'].update(verified_hash=composition.content_hash(job), fingerprint=composition.remote_fingerprint(actual),
                        verified_identity=actual['account_identity']['key'], readback=actual, status='draft_verified')
                    job['reconcile_before_create'] = False
                    job.update(stage='remote_verified',status='remote_verified',error=None); self._record(job, 'remote_verified')
            elif operation == 'publish':
                with self.lock: remote = copy.deepcopy(job['remote'])
                actual = self._adapter('inspect_draft', job['target']['account_id'], remote)
                with self.lock:
                    approval = job.get('approval') or {}
                    if actual.get('status') != 'ok' or not self._remote_matches(job, actual) or approval.get('revision') != job['revision'] or approval.get('content_hash') != composition.content_hash(job) or approval.get('draft_id') != actual.get('draft_id') or approval.get('account_identity') != (actual.get('account_identity') or {}).get('key') or approval.get('remote_fingerprint') != composition.remote_fingerprint(actual):
                        job['approval'] = None; job['remote']['verified_hash'] = None
                        job.update(stage='needs_attention',status='blocked',error='发布前回读不匹配，请重新同步并确认'); self._record(job, 'prepublish_mismatch'); return
                    snapshot = self._snapshot(job); approval = copy.deepcopy(job['approval']); job['operation_id'] = uuid.uuid4().hex; job['external_operation'] = 'publish'; job['pending_operation'] = None
                    self._phase(job, 'publishing'); operation_id = job['operation_id']
                result = self._adapter('publish', job['target']['account_id'], remote, snapshot, approval, operation_id)
                with self.lock:
                    status = result.get('status')
                    if status not in ('accepted','unknown','rejected'):
                        self._blocked(job, result, 'publish_blocked'); return
                    status = {'accepted':'submitted','unknown':'outcome_unknown','rejected':'failed'}[status]
                    job['operation_uncertain'] = status == 'outcome_unknown'
                    if status in ('submitted','outcome_unknown'): self._schedule_check(job)
                    job['remote'].update(result, status=status); job.update(stage=status,status=status,error=None)
                    self._record(job, 'publish_result', result.get('evidence'))
            elif operation == 'verify':
                with self.lock: remote = copy.deepcopy(job.get('remote')); operation_id = job.get('operation_id')
                if job.get('external_operation') != 'publish' and job['status'] != 'outcome_unknown' and remote and remote.get('draft_id') and remote.get('status') in ('saved','draft_verified'):
                    actual = self._adapter('inspect_draft', job['target']['account_id'], remote)
                    with self.lock:
                        if actual.get('status') != 'ok': self._blocked(job, actual, 'readback_blocked'); return
                        if not self._remote_matches(job, actual):
                            self._blocked(job, {'status':'needs_attention','error':'平台草稿回读与当前稿件或账号不一致'}, 'readback_mismatch'); return
                        job['remote'].update(verified_hash=composition.content_hash(job),fingerprint=composition.remote_fingerprint(actual),
                            verified_identity=actual['account_identity']['key'],readback=actual,status='draft_verified')
                        job['reconcile_before_create'] = False
                        job.update(stage='remote_verified',status='remote_verified',error=None); self._record(job,'remote_verified')
                    return
                result = self._adapter('verify', job['target']['account_id'], remote, operation_id)
                with self.lock:
                    if job['status'] == 'published' and result.get('status') != 'published':
                        job['error'] = '已发布记录保留；本次补充核验未获得新的已发布证据'
                        job['next_check_at'] = None; self._record(job, 'verify_failed', job['error']); return
                    if job['status'] in ('submitted','reviewing') and result.get('status') == 'unknown' and not job.get('operation_uncertain'):
                        job['error'] = str(result.get('message') or '本次核验未取得新结果，保留此前平台回执')
                        self._schedule_check(job); self._record(job, 'verify_failed', job['error']); return
                    if result.get('status') not in ('draft','submitted','reviewing','published','rejected','unknown'):
                        self._blocked(job, result, 'verify_blocked'); return
                    if result['status'] == 'draft':
                        for key in ('draft_id','account_identity','cover_media_id','cover_sha256','image_map','url','evidence'):
                            if key in result:
                                if job['remote'] is None: job['remote'] = {}
                                job['remote'][key] = result[key]
                        self._record(job, 'draft_found', result.get('evidence'))
                        proven = ((not job.get('operation_uncertain') or result.get('resolved_operation') is True)
                            and (job.get('external_operation') != 'publish' or (result.get('resolved_operation') is True and result.get('operation_not_submitted') is True)))
                        if not proven or not (job.get('remote') or {}).get('draft_id'):
                            self._blocked(job, {'status':'needs_attention','error':'发现草稿，但未证实未知操作未产生其他结果；不能重发'}, 'draft_unresolved'); return
                        remote = copy.deepcopy(job['remote'])
                        # Inspect below outside the global lock.
                        inspect_draft = True
                    else: inspect_draft = False
                    if inspect_draft: pass
                    else:
                        status = {'unknown':'outcome_unknown','rejected':'failed'}.get(result['status'], result['status'])
                        if status == 'outcome_unknown': job['operation_uncertain'] = True; self._schedule_check(job)
                        else: job['operation_uncertain'] = False
                        if status in ('submitted','reviewing'): self._schedule_check(job)
                        if status in ('published','failed'): job['next_check_at'] = None
                        if job['remote'] is None: job['remote'] = {}
                        job['remote'].update(result,status=status); job.update(stage=status,status=status,error=None)
                        self._record(job, 'verified', result.get('evidence'))
                if inspect_draft:
                    actual = self._adapter('inspect_draft', job['target']['account_id'], remote)
                    with self.lock:
                        if actual.get('status') != 'ok' or not self._remote_matches(job, actual):
                            self._blocked(job, {'status':'needs_attention','error':'发现的草稿未能独立回读匹配'}, 'readback_blocked'); return
                        job['operation_uncertain'] = False; job['approval'] = None; job['next_check_at'] = None
                        job['remote'].update(verified_hash=composition.content_hash(job),fingerprint=composition.remote_fingerprint(actual),
                            verified_identity=actual['account_identity']['key'],readback=actual,status='draft_verified')
                        job['reconcile_before_create'] = False
                        job.update(stage='remote_verified',status='remote_verified',error=None); self._record(job,'remote_verified')

    def shutdown(self):
        self.scheduler_stop.set()
        self.scheduler.join()
        self.executor.shutdown(wait=True)
        while True:
            with self.lock:
                if not self.active_accounts: break
            threading.Event().wait(0.2)
        self.platform_executor.shutdown(wait=True)
