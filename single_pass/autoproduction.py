"""One owned run using the existing collection, screening and production queues."""
import copy
import json
import time
import uuid

from . import ai_settings, column_policy, screen, screening_policy as policy, screening_scope

ACTIVE={'collecting','screening','producing','collecting_more','syncing'}


def snapshot(ws):
    return copy.deepcopy(ws.schedule.get('production_run'))


def save(ws, run):
    run['updated_at']=screen.timestamp()
    ws.schedule['production_run']=run
    screen.write_json(ws.data/'schedule.json',ws.schedule)


def start(ws, body):
    screen.check_runtime()
    with ws.lock:
        config=ws.config();ws.column_config.expected(config,body.get('expected_revision'))
        ids=body.get('line_ids')
        if not isinstance(ids,list) or not ids or len(set(ids))!=len(ids):raise ValueError('请选择要自动出稿的栏目')
        lines=[ws.line(i) for i in ids]
        delivery = body.get('delivery', 'local')
        if delivery not in ('local','draft'):
            raise ValueError('交付方式须为本地稿或平台草稿')
        if delivery == 'draft' and any(not line.get('targets') for line in lines):
            raise ValueError('同步草稿前须为每个栏目指定目标账号')
        if any(not l.get('enabled',True) or not l.get('auto_draft') for l in lines):
            raise ValueError('所选栏目须启用自动选稿制作')
        if ((ws.schedule.get('production_run') or {}).get('status') in ACTIVE or any(ws._active(i) for i in ids)
                or ws.production.running):raise ValueError('已有相关任务运行中')
        minutes=body.get('timeout_minutes',60)
        if type(minutes) is not int or not 1<=minutes<=1440:raise ValueError('执行时限须为1至1440分钟')
        settings=ws.collection_settings.read()
        session_id = None
        if delivery == 'draft':
            existing = ws.platform._persistent_browser_session
            session = ws.platform.start_persistent_session()
            if not existing:
                session_id = session['session_id']
        try:
            collection=ws.start_collection_plan({'expected_revision':settings['revision']})
        except Exception:
            if session_id: ws.platform.end_persistent_session()
            raise
        run={'id':uuid.uuid4().hex,'status':'collecting','started_at':screen.timestamp(),
             'deadline':time.time()+minutes*60,'line_ids':ids,'lines':copy.deepcopy(lines),
             'config_hash':policy.digest(lines),'model_hash':policy.digest(ai_settings.current_snapshot(ws.data)),
             'collection_hash':policy.digest(settings),'collection_id':collection['run_id'],
             'collections':[collection['run_id']],'batches':{},'job_ids':[],'error':None,
             'delivery':delivery, 'owned_session_id':session_id}
        save(ws,run)
        return snapshot(ws)


def extend_batch(ws, batch_id):
    root=ws._path(batch_id);batch=json.loads((root/'batch.json').read_text())
    plan=screening_scope.resolve(ws,{'line_id':batch['line']['id']})
    previous=json.loads((root/'items.json').read_text());known={i['ref']:i for i in previous}
    additions=[i for i in plan['items'] if i['ref'] not in known or i['version']!=known[i['ref']]['version']]
    if not additions:return False
    known.update({i['ref']:i for i in additions})
    screen.write_json(root/'items.json',list(known.values()))
    screen.write_json(root/'input-records.json',plan['records'])
    batch['input_cutoff_at']=screen.timestamp()
    batch.setdefault('supplemental_inputs',[]).append({'at':screen.timestamp(),'refs':[i['ref'] for i in additions]})
    screen.write_json(root/'batch.json',batch)
    state_path=root/'review/state.json'
    state=json.loads(state_path.read_text());state.pop('refill_exhausted',None)
    screen.write_json(state_path,state)
    for path in (root/'result.json',root/'review/grouping-map.json'):
        path.unlink(missing_ok=True)
    return True


def stop_owned(ws, run):
    """Stop this failed run's local work without touching other runs or remote outcomes."""
    batches=set(run['batches'].values())
    for line_id,batch_id in run['batches'].items():
        path=ws._path(batch_id)/'batch.json'
        batch=json.loads(path.read_text())
        batch.update(auto_run_stopped=True,auto_resume=False)
        screen.write_json(path,batch)
        if ws._active(line_id):ws.stop(batch_id)
        ws.schedule['draft:'+line_id]=batch_id
        record=ws.draft_selection.rounds.get(batch_id)
        if record and record['status'] in ('judging','ready','replenishing'):
            token=ws.draft_selection.active.get(batch_id)
            if token:token.set()
            record.update(status='cancelled',revision=record['revision']+1)
            ws.draft_selection.persist()
    if not ws.production.lock.acquire(blocking=False):return False
    try:
        owned=[j for j in ws.production.jobs.values() if j.get('batch_id') in batches
               and j['status'] not in ('ready_review','remote_verified','failed','cancelled')]
        if owned:
            run['stop_results']=ws.production.action([j['id'] for j in owned],'cancel',
                                                     {j['id']:j['revision'] for j in owned})
    finally:ws.production.lock.release()
    return True


def tick(ws):
    run=ws.schedule.get('production_run')
    if run and run.get('stop_pending'):
        run['stop_pending']=not stop_owned(ws,run)
        save(ws,run)
    if not run or run['status'] not in ACTIVE:
        if (run and run.get('owned_session_id') and not run.get('stop_pending')
                and not ws.production.running and not (ws.collection_thread and ws.collection_thread.is_alive())
                and not any(ws._active(i) for i in run['line_ids'])):
            lease = ws.platform._persistent_browser_session or {}
            if lease.get('session_id') == run['owned_session_id']:
                ws.platform.end_persistent_session()
            run['owned_session_id'] = None
            save(ws,run)
        return
    try:
        if time.time()>=run['deadline']:raise ValueError('本轮执行时限已到，尚未达到全部出稿目标')
        if policy.digest([ws.line(i) for i in run['line_ids']])!=run['config_hash']:
            raise ValueError('本轮栏目配置发生变化，不能继续作为同一轮自动出稿')
        if policy.digest(ai_settings.current_snapshot(ws.data))!=run['model_hash']:
            raise ValueError('本轮模型设置发生变化')
        if policy.digest(ws.collection_settings.read())!=run['collection_hash']:
            raise ValueError('本轮采集设置发生变化')
        if run['status'] == 'syncing':
            verify_delivery(ws,run)
            return
        if run['status'] in ('collecting','collecting_more'):
            if ws.collection_thread and ws.collection_thread.is_alive():return
            if ws.collection.get('id')!=run['collection_id']:raise ValueError('本轮采集被其他执行替换')
            if ws.collection['status'] not in ('completed', 'partial'):
                raise ValueError('采集未取得可用结果，请查看逐源错误')
            if ws.collection['status'] == 'partial':
                run.setdefault('collection_errors', {})[run['collection_id']] = '、'.join(
                    s['source_id'] for s in ws.collection['sources'] if s['status']=='failed') + '采集失败'
            if run['status'] == 'collecting':
                for line_id in run['line_ids']:
                    try:
                        row = ws.start_screen(line_id=line_id,runtime_checked=True,topic_record_id=run['id'])
                        run['batches'][line_id] = row['batch_id']
                    except (ValueError, RuntimeError) as exc:
                        run.setdefault('line_errors', {})[line_id] = str(exc)
            else:
                for line_id in run.pop('short_lines'):
                    batch = run['batches'][line_id]
                    try:
                        if not extend_batch(ws,batch):
                            raise ValueError('补采失败或没有新候选，栏目未达出稿目标')
                        record = ws.draft_selection.rounds[batch]
                        record.update(status='replenishing', revision=record['revision']+1)
                        ws.draft_selection.persist()
                        ws.start_screen(batch_id=batch,line_id=line_id,runtime_checked=True,
                                        replenish=record.get('excluded_ids',[]))
                    except (ValueError, RuntimeError) as exc:
                        run.setdefault('line_errors', {})[line_id] = str(exc)
            run['status']='screening';save(ws,run)
            return

        errors = run.setdefault('line_errors', {})
        short, ready, jobs, finished = [], [], [], []
        waiting = False
        from . import editorial
        for line_id, batch_id in run['batches'].items():
            record = ws.draft_selection.rounds.get(batch_id)
            partial_queue = (record or {}).get('status')=='failed' and record.get('retry_from')=='ready' and record.get('job_ids')
            if line_id in errors and not partial_queue:
                continue
            batch = json.loads((ws._path(batch_id)/'batch.json').read_text())
            if ws._active(line_id) or batch['status'] in ('queued','screening'):
                waiting = True
                continue
            if batch['status'] != 'completed':
                if batch.get('auto_resume') and batch.get('automatic_retries',0)<2:
                    waiting = True
                else:
                    errors[line_id] = batch.get('error') or '栏目筛选未完成'
                continue
            if not record or record['status'] in ('judging','ready','replenishing') or record.get('auto_retry_at'):
                waiting = True
                continue
            if record['status'] in ('insufficient','empty'):
                short.append(line_id)
                continue
            if partial_queue:
                errors[line_id] = record.get('error') or '部分制作任务未入队'
            elif record['status'] != 'sent' or not record.get('job_ids'):
                errors[line_id] = record.get('error') or '选稿未交付制作任务'
                continue
            all_ready = True
            for job_id in record['job_ids']:
                jobs.append(job_id)
                job = ws.production.jobs[job_id]
                if job_id in run.get('sync_revisions', {}):
                    # This job already passed local review. Its remote result is
                    # tracked independently while other columns are still busy.
                    continue
                if job['status'] == 'ready_review' and editorial.approved(job):
                    ready.append(job_id)
                    continue
                all_ready = False
                if job['status'] in ('failed','cancelled','outcome_unknown','ready_review') or (
                        job['status']=='needs_attention' and not job.get('digest_replenishment')):
                    run.setdefault('job_errors', {})[job_id] = job.get('error') or '稿件未通过'
                else:
                    waiting = True
            if all_ready and not partial_queue:
                finished.append(line_id)

        run['job_ids'] = list(dict.fromkeys(jobs))
        run['completed_lines'] = finished
        if ready and run.get('delivery') == 'draft':
            revisions = run.setdefault('sync_revisions', {})
            revisions.update({key:ws.production.jobs[key]['revision'] for key in ready if key not in revisions})
            run['sync_job_ids'] = list(revisions)
        if waiting:
            run['status'] = 'producing' if jobs else 'screening'
        elif short:
            run['short_lines'] = short
            settings = ws.collection_settings.read()
            row = ws.start_collection_plan({'expected_revision':settings['revision']})
            run.update(status='collecting_more',collection_id=row['run_id'])
            run['collections'].append(row['run_id'])
        elif run.get('sync_job_ids'):
            run['status'] = 'syncing'
        else:
            finish(run)
        # Persist dispatch intent and the approved revisions before any write.
        save(ws,run)
        if run.get('sync_job_ids'):
            verify_delivery(ws,run,final=run['status']=='syncing')
    except Exception as exc:
        run.update(stop_pending=run['status']!='syncing',status='failed',error=str(exc),finished_at=screen.timestamp())
        save(ws,run)


def finish(run):
    errors = [*run.get('collection_errors', {}).values(), *run.get('line_errors', {}).values(),
              *run.get('job_errors', {}).values()]
    run.update(status='failed' if errors else 'completed', error='；'.join(dict.fromkeys(errors)) or None,
               finished_at=screen.timestamp())


def verify_delivery(ws,run,*,final=True):
    if not ws.production.lock.acquire(blocking=False):
        return
    try:
        jobs = [ws.production.jobs[j] for j in run.get('sync_job_ids', run['job_ids'])]
        revisions = run.get('sync_revisions', {})
        unsent = [j['id'] for j in jobs if j['id'] in revisions
                  and j.get('revision') == revisions[j['id']] and j.get('status') == 'ready_review'
                  and not any(j.get(field) for field in ('pending_operation','next_check_at','remote','error',
                              'operation_id','external_operation','operation_uncertain','reconcile_before_create'))
                  and j['id'] not in ws.production.running and j['id'] not in run.get('job_errors', {})]
        if unsent:
            result = ws.production.action(unsent, 'sync', revisions)
            for item in result['results']:
                if not item['ok']:
                    run.setdefault('job_errors', {})[item['job_id']] = item.get('error') or '同步未受理'
            save(ws,run)
        if any(j.get('pending_operation') or j['id'] in ws.production.running or j.get('next_check_at') for j in jobs):
            return
        for j in jobs:
            remote = j.get('remote') or {}
            if j['status'] != 'remote_verified' or not remote.get('verified_hash') or not remote.get('readback'):
                run.setdefault('job_errors', {}).setdefault(j['id'], j.get('error') or '草稿未经平台回读验证，不重复提交')
        if final:
            finish(run)
        save(ws,run)
    finally:
        ws.production.lock.release()
