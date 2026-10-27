"""SQLite state for candidates and user decisions."""
import hashlib
import json
import sqlite3
from pathlib import Path
from .collect import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, source_results TEXT);
CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_name TEXT NOT NULL,
 source_type TEXT NOT NULL, source_url TEXT NOT NULL, canonical_url TEXT NOT NULL, source_key TEXT,
 title TEXT NOT NULL, event_time TEXT, published_at TEXT, cover_url TEXT, collected_at TEXT NOT NULL,
 metrics TEXT NOT NULL, platform_snapshot_time TEXT, selection TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS appearances (id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL, batch_id INTEGER NOT NULL,
 source_id TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL, collected_at TEXT NOT NULL,
 metrics TEXT NOT NULL, platform_snapshot_time TEXT, FOREIGN KEY(candidate_id) REFERENCES candidates(id));
CREATE TABLE IF NOT EXISTS details (id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL, status TEXT NOT NULL,
 provenance TEXT NOT NULL, source_url TEXT, collected_at TEXT NOT NULL, text TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS drafts (candidate_id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
 revision INTEGER NOT NULL, confirmed_revision INTEGER, updated_at TEXT NOT NULL);
"""


def connect(db):
    p = Path(db); p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def candidate_id(item):
    key = item.get("source_key") or item.get("canonical_url")
    if not key:
        raise ValueError("缺少来源标识或链接")
    return item["source_id"] + ":" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def begin_batch(conn):
    start = now()
    cur = conn.execute("INSERT INTO batches(started_at) VALUES (?)", (start,))
    conn.commit()
    return cur.lastrowid, start


def save_rows(conn, batch, rows):
    stamp = now()
    for x in rows:
        cid = candidate_id(x)
        existing = conn.execute("SELECT id FROM candidates WHERE source_id=? AND ((source_key IS NOT NULL AND source_key=?) OR (canonical_url<>'' AND canonical_url=?)) LIMIT 1",
                                (x["source_id"], x.get("source_key"), x["canonical_url"])).fetchone()
        if existing: cid = existing[0]
        vals = (cid, x["source_id"], x["source_name"], x["source_type"], x["source_url"], x["canonical_url"], x.get("source_key"),
                x["title"], x.get("event_time"), x.get("published_at"), x.get("cover_url"), stamp,
                json.dumps(x.get("metrics") or {}, ensure_ascii=False), x.get("platform_snapshot_time"))
        conn.execute("""INSERT INTO candidates(id,source_id,source_name,source_type,source_url,canonical_url,source_key,title,event_time,published_at,cover_url,collected_at,metrics,platform_snapshot_time)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,source_url=excluded.source_url,
        cover_url=excluded.cover_url,published_at=excluded.published_at,collected_at=excluded.collected_at,metrics=excluded.metrics,
        platform_snapshot_time=excluded.platform_snapshot_time""", vals)
        conn.execute("INSERT INTO appearances(candidate_id,batch_id,source_id,source_name,source_url,collected_at,metrics,platform_snapshot_time) VALUES (?,?,?,?,?,?,?,?)",
                     (cid,batch,x["source_id"],x["source_name"],x["source_url"],stamp,vals[12],x.get("platform_snapshot_time")))
    conn.commit()


def end_batch(conn, batch, results):
    end = now()
    conn.execute("UPDATE batches SET finished_at=?, source_results=? WHERE id=?", (end,json.dumps(results,ensure_ascii=False),batch))
    conn.commit()
    return end


def as_candidate(conn, row):
    obj = dict(row)
    obj.pop("canonical_url", None); obj.pop("source_key", None)
    obj["metrics"] = json.loads(obj["metrics"])
    # Similar titles are hints only, never an automatic merge.
    obj["possible_duplicates"] = [r[0] for r in conn.execute("SELECT id FROM candidates WHERE title=? AND id<>? LIMIT 20", (obj["title"],obj["id"]))]
    return obj


def list_candidates(conn, selection="", source="", q="", limit=50, offset=0, source_type=""):
    where, args = [], []
    for condition, value in (("selection=?",selection),("source_id=?",source),("source_type=?",source_type),("title LIKE ?", "%"+q+"%" if q else "")):
        if value: where.append(condition); args.append(value)
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = conn.execute("SELECT count(*) FROM candidates"+clause,args).fetchone()[0]
    rows = conn.execute("SELECT * FROM candidates"+clause+" ORDER BY collected_at DESC,id LIMIT ? OFFSET ?", args+[limit,offset]).fetchall()
    return {"items":[as_candidate(conn,r) for r in rows],"total":total}


def get_candidate(conn, cid):
    row = conn.execute("SELECT * FROM candidates WHERE id=?",(cid,)).fetchone()
    return as_candidate(conn,row) if row else None


def detail_view(conn, cid):
    c = get_candidate(conn,cid)
    if not c: return None
    appearances = [dict(x) for x in conn.execute("SELECT source_id,source_name,source_url,collected_at,metrics,platform_snapshot_time,batch_id FROM appearances WHERE candidate_id=? ORDER BY id DESC",(cid,))]
    for a in appearances: a["metrics"] = json.loads(a["metrics"])
    details = [dict(x) for x in conn.execute("SELECT id,status,provenance,source_url,collected_at,text,reason FROM details WHERE candidate_id=? ORDER BY id DESC",(cid,))]
    draft = conn.execute("SELECT title,body,revision,confirmed_revision,updated_at FROM drafts WHERE candidate_id=?",(cid,)).fetchone()
    return {"candidate":c,"appearances":appearances,"details":details,"draft":dict(draft) if draft else None}


def save_detail(conn, cid, status, provenance, source_url=None, text=None, reason=None):
    stamp=now(); cur=conn.execute("INSERT INTO details(candidate_id,status,provenance,source_url,collected_at,text,reason) VALUES (?,?,?,?,?,?,?)",(cid,status,provenance,source_url,stamp,text,reason))
    conn.execute("UPDATE drafts SET confirmed_revision=NULL WHERE candidate_id=?", (cid,))
    conn.commit()
    return {"id":cur.lastrowid,"status":status,"provenance":provenance,"source_url":source_url,"collected_at":stamp,"text":text,"reason":reason}


def save_draft(conn, cid, title, body):
    row=conn.execute("SELECT revision FROM drafts WHERE candidate_id=?",(cid,)).fetchone()
    revision=(row[0]+1) if row else 1; stamp=now()
    conn.execute("INSERT INTO drafts VALUES (?,?,?,?,NULL,?) ON CONFLICT(candidate_id) DO UPDATE SET title=excluded.title,body=excluded.body,revision=excluded.revision,confirmed_revision=NULL,updated_at=excluded.updated_at",(cid,title,body,revision,stamp))
    conn.commit()
    return {"title":title,"body":body,"revision":revision,"confirmed_revision":None,"updated_at":stamp}
