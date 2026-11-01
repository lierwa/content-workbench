"""Persistent source identities and per-column decisions; run files hold evidence."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import screening_policy as policy


def canonical(url):
    parts = urlsplit(url or '')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('spm', 'fbclid', 'gclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ''))


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone(timedelta(hours=8)))
    except (ValueError, TypeError, AttributeError):
        return None


def fresh(item, line, at=None):
    at = at or datetime.now(timezone.utc)
    stamp = instant(item.get('published_at')) or instant(item.get('first_seen_at'))
    return bool(stamp and at - timedelta(hours=line['valid_hours']) <= stamp <= at)


def input_basis(items, line):
    return policy.digest({'members': sorted((x['ref'], x['version']) for x in items),
                          'line': policy.line_basis(line), 'policy': policy.policy_hash()})


class Pool:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS pool_items (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, identity TEXT NOT NULL,
                    version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(source_id, identity));
                CREATE TABLE IF NOT EXISTS line_records (
                    line_id TEXT NOT NULL, format TEXT NOT NULL, item_id TEXT NOT NULL,
                    version INTEGER NOT NULL, rule_hash TEXT NOT NULL, basis_hash TEXT NOT NULL,
                    input_hash TEXT NOT NULL, run_id TEXT NOT NULL, result_ref TEXT NOT NULL,
                    stage TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(line_id, format, item_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, items, observed_at):
        counts = {'new': 0, 'changed': 0, 'unchanged': 0}
        changed = []
        with self.connect() as db:
            for item in items:
                value = dict(item)
                identity = ('key:' + str(value['source_key'])) if value.get('source_key') else 'url:' + canonical(value['entry_url'])
                ref = value['source_id'] + ':' + policy.digest(identity)[:24]
                old = db.execute('SELECT * FROM pool_items WHERE id=?', (ref,)).fetchone()
                fingerprint = policy.digest({k: canonical(value.get(k)) if k in ('entry_url', 'url') else value.get(k)
                                             for k in ('title', 'summary', 'entry_url', 'url', 'published_at', 'event_time')})
                different = bool(old and old['fingerprint'] != fingerprint)
                version = old['version'] + int(different) if old else 1
                value.update(ref=ref, version=version, fingerprint=fingerprint,
                             first_seen_at=old['first_seen_at'] if old else observed_at,
                             last_seen_at=observed_at, collected_at=observed_at)
                db.execute('INSERT INTO pool_items VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                           'version=excluded.version,fingerprint=excluded.fingerprint,last_seen_at=excluded.last_seen_at,payload=excluded.payload',
                           (ref, value['source_id'], identity, version, fingerprint, value['first_seen_at'], observed_at,
                            json.dumps(value, ensure_ascii=False)))
                counts['new' if not old else 'changed' if different else 'unchanged'] += 1
                if different:
                    changed.append(ref)
        return dict(counts, changed_refs=changed)

    def snapshot(self, line):
        with self.connect() as db:
            values = [json.loads(row['payload']) for row in db.execute('SELECT payload FROM pool_items ORDER BY first_seen_at,id')]
            records = {r['item_id']: dict(r) for r in db.execute(
                'SELECT * FROM line_records WHERE line_id=? AND format=?', (line['id'], line['format']))}
        eligible = [v for v in values if v['source_id'] in line['sources'] and fresh(v, line)]
        return eligible, records, {'total': len(values), 'eligible': len(eligible),
            'expired': sum(v['source_id'] in line['sources'] and not fresh(v, line) for v in values)}

    def current(self, items):
        with self.connect() as db:
            return all((row := db.execute('SELECT version FROM pool_items WHERE id=?', (i['ref'],)).fetchone())
                       and row['version'] == i['version'] for i in items)

    def material_records(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM line_records ORDER BY processed_at DESC')]

    def remember(self, line, members, event, run_id, at):
        rounds = event.get('screening', {}).get('rounds', [])
        last = rounds[-1] if rounds else {}
        decision = last.get('decision', 'insufficient')
        if event.get('stage') == 'not_selected':
            decision = 'deferred'
        if event.get('stage') == 'deferred':
            decision = 'advance' if (event.get('plan') or {}).get('selected') else 'insufficient'
        if decision == 'deferred' and event.get('stage') != 'not_selected':
            decision = 'insufficient'
        incoming = input_basis(members, line)
        basis = policy.digest({'input': incoming, 'stage': event.get('stage'),
            'materials': [(m.get('material_id'), m.get('content_sha256')) for m in event.get('materials', [])]})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not all((row := db.execute('SELECT version FROM pool_items WHERE id=?', (i['ref'],)).fetchone())
                       and row['version'] == i['version'] for i in members):
                return False
            for i in members:
                db.execute('INSERT OR REPLACE INTO line_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (line['id'], line['format'], i['ref'], i['version'], policy.line_hash(line), basis, incoming,
                     run_id, event['id'], event.get('stage', 'queued'), decision, last.get('reason', ''), at))
        return True
