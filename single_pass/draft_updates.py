"""Durable existing-draft workflow. This module never creates a platform draft."""
import copy
import json
import re
from pathlib import Path

from . import composition, screen
from .platform_format_update import unchanged


def _path(adapter, account_id, operation_id):
    if not re.fullmatch(r'[0-9a-f]{32}', str(operation_id or '')):
        raise ValueError('更新流程缺少操作 ID')
    folder = adapter._path(account_id) / 'draft-updates'
    folder.mkdir(exist_ok=True)
    return folder / (operation_id + '.json')


def _binding(snapshot, remote):
    draft_id = str(remote.get('draft_id') or '')
    if not re.fullmatch(r'\d+', draft_id) or not remote.get('readback'):
        raise ValueError('更新必须绑定已有草稿 ID 和上次完整回读')
    if snapshot.get('existing_draft_id') and str(snapshot['existing_draft_id']) != draft_id:
        raise ValueError('更新目标与任务原草稿 ID 不符')
    identity = (remote.get('account_identity') or {}).get('key')
    if not identity:
        raise ValueError('更新必须绑定原平台身份')
    return {'account_id': snapshot['account_id'], 'platform': snapshot['platform'],
            'draft_id': draft_id, 'identity': identity, 'revision': snapshot['revision'],
            'content_hash': snapshot['content_hash'], 'operation_id': snapshot['operation_id']}


def _record(path, journal, phase, detail=None):
    journal['phase'] = phase
    journal.setdefault('events', []).append({'phase': phase, 'at': screen.timestamp(), 'detail': detail})
    screen.write_json(path, journal)


def run(adapter, account_id, snapshot, remote, writer):
    """Check the old version, write the same ID, then require independent read-back."""
    path = _path(adapter, account_id, snapshot.get('operation_id'))
    binding = _binding(snapshot, remote)
    if account_id != binding['account_id']:
        raise ValueError('更新入口账号与冻结任务不符')
    journal = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'kind': 'update_existing_draft', 'binding': binding, 'original': copy.deepcopy(remote), 'events': []}
    if journal['binding'] != binding:
        raise ValueError('更新流程绑定的版本、账号或草稿 ID 改变')
    if journal.get('phase') in ('writing', 'saving', 'recovering', 'outcome_unknown'):
        return {'status': 'unknown', 'draft_id': binding['draft_id'],
                'message': '原更新已有未确认写入，须走恢复流程，不重放写入'}
    if journal.get('phase') in ('saved', 'verified'):
        return dict(journal['result'], readback=None)
    _record(path, journal, 'checking_original')
    actual = adapter.inspect_draft(account_id, remote)
    if actual.get('status') != 'ok':
        _record(path, journal, 'check_failed', actual.get('message'))
        return actual
    if (str(actual.get('draft_id')) != binding['draft_id']
            or (actual.get('account_identity') or {}).get('key') != binding['identity']
            or not unchanged(actual, remote['readback'])):
        _record(path, journal, 'original_changed')
        return {'status': 'needs_attention', 'message': '原草稿已被修改，未覆盖'}
    checked = dict(remote, readback=actual, fingerprint=composition.remote_fingerprint(actual))
    journal['checked_original'] = actual
    if snapshot['content_hash'] in (remote.get('verified_hash'),remote.get('last_verified_hash')):
        result=dict(remote,status='saved',readback=actual,no_op=True,
                    evidence='当前版本与已核验平台草稿一致，仅重新核对，未重复写入')
        journal['result']=result
        _record(path,journal,'saved','unchanged_version')
        return result
    _record(path, journal, 'writing')
    try:
        result = writer(checked, actual)
    except Exception as exc:
        _record(path, journal, 'outcome_unknown', str(exc)[:500])
        raise
    if result.get('status') == 'saved' and str(result.get('draft_id')) != binding['draft_id']:
        result = dict(result, status='unknown', message='更新返回了不同草稿 ID，停止后续操作')
    journal['result'] = result
    phase = 'saved' if result.get('status') == 'saved' else 'outcome_unknown' if result.get('status') in ('unknown','outcome_unknown') else 'write_not_started'
    _record(path, journal, phase, result.get('message'))
    return result


def resume(adapter, account_id, snapshot, context, writer):
    """Resume the bound operation; a lost editor can never become a new draft."""
    path = _path(adapter, account_id, snapshot.get('operation_id'))
    if not path.exists():
        return writer(snapshot, context)  # Existing platform recovery evidence.
    journal = json.loads(path.read_text(encoding='utf-8'))
    if journal['binding'] != _binding(snapshot, journal['original']):
        raise ValueError('恢复的版本、账号或草稿 ID 与原更新不符')
    if journal.get('phase') in ('saved', 'verified'):
        return dict(journal['result'], readback=None)
    bound = dict(snapshot, existing_draft_id=journal['binding']['draft_id'])
    if journal.get('save_attempt') or (journal.get('result') or {}).get('phase') == 'save_draft':
        # Saving an existing draft cannot be proved by its already-existing ID.
        # Reopen it and validate the whole requested version before any new write.
        actual=adapter.inspect_draft(account_id,journal['original'])
        try:
            result=confirmed_result(adapter,bound,journal['binding'],actual)
        except (ValueError,OSError,KeyError):
            if actual.get('status')!='ok' or not unchanged(actual,journal['original']['readback']):
                return {'status':'unknown','message':'保存结果尚未核对一致，保留原更新，未重放保存'}
            checked=dict(journal['original'],readback=actual,fingerprint=composition.remote_fingerprint(actual))
            _record(path,journal,'writing')
            result=adapter._write_draft(account_id,bound,checked,actual)
        journal['result']=result
        _record(path,journal,'saved' if result.get('status')=='saved' else 'outcome_unknown',result.get('message'))
        return result
    _record(path, journal, 'recovering')
    try:
        result = writer(bound, context)
        if result.get('status') == 'needs_attention' and result.get('phase') == 'resume_image_editor':
            # A previous readback may have reloaded an unsaved WeChat editor.
            # Restart replacement only after the original saved draft is proven
            # unchanged. Keep the same ID and operation, never create a substitute.
            actual = adapter.inspect_draft(account_id,journal['original'])
            if (actual.get('status') == 'ok' and str(actual.get('draft_id')) == bound['existing_draft_id']
                    and unchanged(actual,journal['original']['readback'])):
                _record(path,journal,'original_restored')
                checked=dict(journal['original'],readback=actual,fingerprint=composition.remote_fingerprint(actual))
                _record(path,journal,'writing')
                result=adapter._write_draft(account_id,bound,checked,actual)
    except Exception as exc:
        _record(path, journal, 'outcome_unknown', str(exc)[:500])
        raise
    if result.get('status') == 'saved' and str(result.get('draft_id')) != bound['existing_draft_id']:
        result = dict(result, status='unknown', message='恢复返回了不同草稿 ID，停止后续操作')
    journal['result'] = result
    _record(path, journal, 'saved' if result.get('status') == 'saved' else 'outcome_unknown', result.get('message'))
    return result


def save_attempt(adapter, account_id, snapshot, metadata):
    path=_path(adapter,account_id,snapshot.get('operation_id'))
    if not path.exists():return
    journal=json.loads(path.read_text(encoding='utf-8'))
    if journal['binding']!=_binding(snapshot,journal['original']):raise ValueError('保存时更新绑定改变')
    journal['save_attempt']=metadata
    _record(path,journal,'saving')


def confirmed_result(adapter, snapshot, binding, actual):
    from .platform_format_update import checked_images
    from .asset_verification import verify_photo
    if (actual.get('status')!='ok' or str(actual.get('draft_id'))!=binding['draft_id']
            or (actual.get('account_identity') or {}).get('key')!=binding['identity']
            or composition.normalize_text(actual.get('title'))!=composition.normalize_text(snapshot['title'])
            or composition.normalize_text(actual.get('summary'))!=composition.normalize_text(snapshot.get('summary'))):
        raise ValueError('保存结果的账号、标题或摘要不同')
    mapping,checks=checked_images(snapshot,{'draft_id':binding['draft_id']},actual['html'],adapter)
    expected=composition.semantic_html(snapshot['html'])
    expected['images']=[dict(x,src=mapping[x['src']]) for x in expected['images']]
    if (not composition.wechat_content_matches(expected,composition.semantic_html(actual['html']))
            or not composition.emphasis_preserved(snapshot['html'],actual['html'])):
        raise ValueError('保存结果的正文、图片或排版不同')
    cover=actual.get('cover_url')
    if bool(cover)!=bool(snapshot.get('cover_path')):raise ValueError('保存结果缺少封面')
    cover_check=verify_photo(cover,Path(snapshot['cover_path']).read_bytes(),'wechat') if cover else None
    return dict(status='saved',draft_id=binding['draft_id'],account_identity=actual['account_identity'],
                title=actual['title'],cover_url=cover,cover_sha256=snapshot.get('cover_sha256'),image_map=mapping,
                image_checks=checks,cover_check=cover_check,readback=actual,evidence='保存中断后重新打开原 ID，全文与图片独立核对一致')


def needs_editor_recovery(adapter, account_id, operation_id):
    if not re.fullmatch(r'[0-9a-f]{32}',str(operation_id or '')):
        return False
    path=adapter._path(account_id)/'draft-updates'/(operation_id+'.json')
    if not path.exists():return False
    journal=json.loads(path.read_text(encoding='utf-8'))
    return (journal['binding']['platform']=='wechat'
            and journal.get('phase') in ('writing','saving','recovering','outcome_unknown'))


def mark_verified(adapter, account_id, operation_id, actual):
    path = _path(adapter, account_id, operation_id)
    if not path.exists():
        return  # A newly created draft has its own existing production journal.
    journal = json.loads(path.read_text(encoding='utf-8'))
    binding = journal['binding']
    if (str(actual.get('draft_id')) != binding['draft_id']
            or (actual.get('account_identity') or {}).get('key') != binding['identity']):
        raise ValueError('更新回读的草稿 ID 或账号与流程绑定不符')
    journal['verified_readback'] = actual
    _record(path, journal, 'verified')
