"""Loopback-only HTTP API and static interface."""
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from . import details, pipeline, storage

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "workbench" / "static"
LOCK = threading.Lock()
SELECTIONS = {"pending", "selected", "deferred", "excluded"}

def lock_selected(conn, cid):
    conn.execute("BEGIN IMMEDIATE")
    row=conn.execute("SELECT selection FROM candidates WHERE id=?",(cid,)).fetchone()
    if not row or row[0] != "selected": raise APIError(409,"not_selected","请先选择该候选")


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def json_body(self):
        if self.headers.get_content_type() != "application/json": raise APIError(415,"content_type","需要 application/json")
        origin=self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.server.server_port}": raise APIError(403,"origin","只允许本地同源请求")
        try: length=int(self.headers.get("Content-Length", "0"))
        except ValueError: raise APIError(400,"body","Content-Length 无效")
        if length < 0 or length > 1_200_000: raise APIError(413,"body_size","请求过大")
        try: obj=json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError): raise APIError(400,"body","JSON 无效")
        if not isinstance(obj,dict): raise APIError(400,"body","JSON 须为对象")
        return obj

    def guard(self):
        host=self.headers.get("Host","")
        if host not in (f"127.0.0.1:{self.server.server_port}",): raise APIError(403,"host","只允许回环地址访问")

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")

    def dispatch(self, method):
        try:
            self.guard()
            p=urlsplit(self.path); path=p.path; params=parse_qs(p.query)
            if method != "GET": body=self.json_body()
            if path == "/api/health" and method == "GET": return self.respond(200,{"ok":True})
            if path == "/api/config" and method == "GET":
                config=json.loads((ROOT/"config"/"workbench.json").read_text(encoding="utf-8"))
                return self.respond(200,config)
            if path == "/api/collect" and method == "POST":
                if not LOCK.acquire(blocking=False): raise APIError(409,"collect_running","采集正在运行")
                try: result=pipeline.run(self.server.db)
                finally: LOCK.release()
                return self.respond(200,result)
            if not path.startswith("/api/") and method == "GET": return self.static(path)
            conn=storage.connect(self.server.db)
            try:
                if path == "/api/batches" and method == "GET":
                    rows=conn.execute("SELECT id,started_at,finished_at,source_results FROM batches ORDER BY id DESC LIMIT 30").fetchall()
                    items=[{"id":r["id"],"started_at":r["started_at"],"finished_at":r["finished_at"],"source_results":json.loads(r["source_results"]) if r["source_results"] else []} for r in rows]
                    return self.respond(200,{"items":items})
                if path == "/api/candidates" and method == "GET":
                    selection=params.get("selection",[""])[0]; source=params.get("source",[""])[0]; q=params.get("q",[""])[0]; source_type=params.get("source_type",[""])[0]
                    try: limit=max(1,min(200,int(params.get("limit",["50"])[0]))); offset=max(0,int(params.get("offset",["0"])[0]))
                    except ValueError: raise APIError(400,"query","分页参数无效")
                    if selection and selection not in SELECTIONS: raise APIError(400,"selection","无效的选择状态")
                    if source_type and source_type not in ("current_hotlist","historical"): raise APIError(400,"source_type","无效的来源类型")
                    return self.respond(200,storage.list_candidates(conn,selection,source,q,limit,offset,source_type))
                if path == "/api/candidates/selection" and method == "POST":
                    ids=body.get("ids"); selection=body.get("selection")
                    if not isinstance(ids,list) or not ids or len(ids)>1000 or any(not isinstance(x,str) for x in ids) or selection not in SELECTIONS: raise APIError(400,"selection","需要候选ID数组及有效状态")
                    conn.execute("BEGIN IMMEDIATE")
                    ids=list(dict.fromkeys(ids)); marks=",".join("?" for _ in ids)
                    count=conn.execute(f"SELECT count(*) FROM candidates WHERE id IN ({marks})",ids).fetchone()[0]
                    if count != len(ids): raise APIError(404,"not_found","候选不存在，未执行修改")
                    conn.execute(f"UPDATE candidates SET selection=? WHERE id IN ({marks})",[selection]+ids)
                    if selection != "selected": conn.execute(f"UPDATE drafts SET confirmed_revision=NULL WHERE candidate_id IN ({marks})",ids)
                    conn.commit()
                    return self.respond(200,{"updated":len(ids)})
                parts=path.strip("/").split("/")
                if len(parts) in (3,4) and parts[:2] == ["api","candidates"]:
                    cid=unquote(parts[2]); c=storage.get_candidate(conn,cid)
                    if not c: raise APIError(404,"not_found","候选不存在")
                    if len(parts)==3 and method == "GET": return self.respond(200,storage.detail_view(conn,cid))
                    if c["selection"] != "selected": raise APIError(409,"not_selected","请先选择该候选")
                    if len(parts)==4 and parts[3]=="details" and method=="POST":
                        manual=body.get("manual_text"); url=body.get("source_url")
                        if manual is not None:
                            if not isinstance(manual,str) or not manual.strip(): raise APIError(400,"details","手动正文不能为空")
                            if url and (not isinstance(url,str) or urlsplit(url).scheme not in ("http","https") or not urlsplit(url).hostname): raise APIError(400,"details","来源链接无效")
                            lock_selected(conn,cid)
                            d=storage.save_detail(conn,cid,"obtained","manual",source_url=url or None,text=manual.strip())
                        else:
                            url=url or c["source_url"]
                            if not isinstance(url,str): raise APIError(400,"details","链接无效")
                            status,text,reason=("needs_source",None,"榜单话题、搜索或视频入口不是事实报道，请提供具体报道或手动文本") if details.is_topic(url) else details.fetch_detail(url)
                            lock_selected(conn,cid)
                            d=storage.save_detail(conn,cid,status,"web",source_url=url,text=text,reason=reason)
                        return self.respond(200,{"detail":d})
                    if len(parts)==4 and parts[3]=="draft" and method=="PUT":
                        title=body.get("title"); content=body.get("body")
                        if not isinstance(title,str) or not isinstance(content,str) or not title.strip() or not content.strip(): raise APIError(400,"draft","标题与正文均不能为空")
                        lock_selected(conn,cid)
                        return self.respond(200,{"draft":storage.save_draft(conn,cid,title.strip(),content.strip())})
                    if len(parts)==4 and parts[3]=="confirm" and method=="POST":
                        revision=body.get("revision")
                        if type(revision) is not int: raise APIError(400,"revision","需要整数 revision")
                        lock_selected(conn,cid)
                        cur=conn.execute("UPDATE drafts SET confirmed_revision=revision WHERE candidate_id=? AND revision=?",(cid,revision))
                        if cur.rowcount != 1: raise APIError(409,"revision_conflict","草稿不存在或版本已变化")
                        conn.commit();return self.respond(200,{"draft":storage.detail_view(conn,cid)["draft"]})
                raise APIError(404,"not_found","接口不存在")
            finally: conn.close()
        except APIError as exc: self.respond(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except Exception as exc: self.respond(500,{"error":{"code":"internal","message":f"{type(exc).__name__}: {exc}"}})

    def static(self,path):
        target=(STATIC / unquote(path.lstrip("/") or "index.html")).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file(): raise APIError(404,"not_found","文件不存在")
        body=target.read_bytes();self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length",str(len(body)));self.send_header("X-Content-Type-Options","nosniff");self.end_headers();self.wfile.write(body)


def serve(host,port,db):
    if host != "127.0.0.1": raise ValueError("只允许绑定 127.0.0.1")
    server=ThreadingHTTPServer((host,port),Handler);server.db=db
    print(f"http://{host}:{server.server_port}  db={db}",flush=True)
    try: server.serve_forever()
    finally: server.server_close()
