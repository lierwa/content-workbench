"""One owned run using the existing collection, screening and production queues."""
import copy
import json
import time
import uuid

from . import ai_settings, column_policy, screen, screening_policy as policy, screening_scope

ACTIVE={'collecting','screening','producing','collecting_more'}


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
        if any(not l.get('enabled',True) or not l.get('auto_draft') for l in lines):
            raise ValueError('所选栏目须启用自动选稿制作')
        if ((ws.schedule.get('production_run') or {}).get('status') in ACTIVE or any(ws._active(i) for i in ids)
                or ws.production.running):raise ValueError('已有相关任务运行中')
        minutes=body.get('timeout_minutes',60)
        if type(minutes) is not int or not 1<=minutes<=1440:raise ValueError('执行时限须为1至1440分钟')
        settings=ws.collection_settings.read()
        collection=ws.start_collection_plan({'expected_revision':settings['revision']})
        run={'id':uuid.uuid4().hex,'status':'collecting','started_at':screen.timestamp(),
             'deadline':time.time()+minutes*60,'line_ids':ids,'lines':copy.deepcopy(lines),
             'config_hash':policy.digest(lines),'model_hash':policy.digest(ai_settings.current_snapshot(ws.data)),
             'collection_hash':policy.digest(settings),'collection_id':collection['run_id'],
             'collections':[collection['run_id']],'batches':{},'job_ids':[],'error':None}
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
               and j['status'] not in ('ready_review','failed','cancelled')]
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
    if not run or run['status'] not in ACTIVE:return
    try:
        if time.time()>=run['deadline']:raise ValueError('本轮执行时限已到，尚未达到全部出稿目标')
        if policy.digest([ws.line(i) for i in run['line_ids']])!=run['config_hash']:
            raise ValueError('本轮栏目配置发生变化，不能继续作为同一轮自动出稿')
        if policy.digest(ai_settings.current_snapshot(ws.data))!=run['model_hash']:
            raise ValueError('本轮模型设置发生变化')
        if policy.digest(ws.collection_settings.read())!=run['collection_hash']:
            raise ValueError('本轮采集设置发生变化')
        if run['status'] in ('collecting','collecting_more'):
            if ws.collection_thread and ws.collection_thread.is_alive():return
            if ws.collection.get('id')!=run['collection_id']:raise ValueError('本轮采集被其他执行替换')
            if ws.collection['status']!='completed':raise ValueError('采集未完整成功，请查看逐源错误')
            if run['status']=='collecting':
                for line_id in run['line_ids']:
                    row=ws.start_screen(line_id=line_id,runtime_checked=True,topic_record_id=run['id'])
                    run['batches'][line_id]=row['batch_id']
            else:
                resumed=[]
                for line_id in run.pop('short_lines'):
                    batch=run['batches'][line_id]
                    if not extend_batch(ws,batch):continue
                    record=ws.draft_selection.rounds[batch]
                    record['status']='replenishing';ws.draft_selection.persist()
                    ws.start_screen(batch_id=batch,line_id=line_id,runtime_checked=True,
                                    replenish=record.get('excluded_ids',[]))
                    resumed.append(line_id)
                if not resumed:raise ValueError('候选已耗尽，按原渠道补采也没有新候选；本轮未完成')
            run['status']='screening';save(ws,run)
            return
        short=[];finished=[];jobs=[];busy=False
        for line_id,batch_id in run['batches'].items():
            batch=json.loads((ws._path(batch_id)/'batch.json').read_text())
            if ws._active(line_id) or batch['status'] in ('queued','screening'):
                busy=True;continue
            if batch['status']!='completed':
                if batch.get('auto_resume') and batch.get('automatic_retries',0)<2:continue
                raise ValueError(batch.get('error') or '栏目筛选未完成')
            record=ws.draft_selection.rounds.get(batch_id)
            if not record:continue
            if record['status'] in ('judging','ready','replenishing'):
                busy=True;continue
            if record['status']=='failed':
                if record.get('auto_retry_at'):continue
                raise ValueError(record.get('error') or '选稿失败')
            if record['status'] in ('insufficient','empty'):
                short.append(line_id);continue
            if record['status']=='cancelled':raise ValueError('本轮选稿已取消')
            owned=[ws.production.jobs[j] for j in record.get('job_ids',[]) if j in ws.production.jobs]
            jobs.extend(j['id'] for j in owned)
            from . import editorial
            if owned and all(j['status']=='ready_review' and editorial.approved(j) for j in owned):
                finished.append(line_id)
            elif any(j['status'] in ('failed','cancelled','outcome_unknown') or j['status']=='needs_attention' and not j.get('digest_replenishment') for j in owned):
                raise ValueError('制作未通过：'+'；'.join(j.get('error') or j['status'] for j in owned if j['status']!='ready_review'))
        run['job_ids']=list(dict.fromkeys(jobs));run['completed_lines']=finished
        if len(finished)==len(run['line_ids']):
            run.update(status='completed',finished_at=screen.timestamp())
        elif short and not busy and not ws.production.running:
            run['short_lines']=short
            settings=ws.collection_settings.read()
            row=ws.start_collection_plan({'expected_revision':settings['revision']})
            run.update(status='collecting_more',collection_id=row['run_id'])
            run['collections'].append(row['run_id'])
        else:run['status']='producing' if jobs else 'screening'
        save(ws,run)
    except Exception as exc:
        run.update(status='failed',error=str(exc),finished_at=screen.timestamp(),stop_pending=True)
        save(ws,run)
