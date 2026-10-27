import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from workbench import collect, pipeline, storage
from workbench.server import Handler

def fixture_urls():
    toutiao=json.dumps({"status":"success","data":[{"Title":"同一话题","Url":"https://example.com/a","ClusterIdStr":"101","Image":{"url":"https://example.com/a.jpg"},"HotValue":"100"}]})
    baidu='<!--s-data:'+json.dumps({"data":{"cards":[{"component":"hotList","content":[{"word":"同一话题","url":"https://www.baidu.com/s?wd=x","img":"https://example.com/b.jpg","hotScore":"99"}]}]}})+'-->'
    bili=json.dumps({"code":0,"data":{"list":[{"title":"视频","bvid":"BV1xx","pic":"https://example.com/p.jpg","pubdate":1792815097,"stat":{"view":15,"like":2}}]}})
    def card(node,title,url):
        return f'<div class="cc-cd" id="node-{node}"><a href="{url}" itemid="12"><span class="s h">1</span><span class="t">{title}</span><span class="e">100万</span></a><div class="i-h ">14分钟前</div></div>'
    top=card(1,"微博话题","https://s.weibo.com/weibo?q=x")+card(6,"知乎话题","https://www.zhihu.com/question/1")
    return {collect.URLS['toutiao']:toutiao,collect.URLS['baidu']:baidu,collect.URLS['bilibili']:bili,collect.URLS['tophub']:top}

class CollectTests(unittest.TestCase):
    def test_isolated_failure_dedup_and_selection_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'x.sqlite3'); fixtures=fixture_urls(); calls=[]
            def fetch(url):
                calls.append(url)
                if url==collect.URLS['baidu']: raise OSError('sample failure')
                return fixtures[url]
            result=pipeline.run(db,fetch)
            self.assertEqual([r['status'] for r in result['source_results']],['success','failed','success','success','success'])
            self.assertEqual(calls.count(collect.URLS['tophub']),1)
            self.assertEqual(set(calls),set(collect.URLS.values()))  # no article or cover requests
            con=storage.connect(db); first=storage.list_candidates(con)['items']; cid=next(x['id'] for x in first if x['source_id']=='toutiao')
            con.execute("UPDATE candidates SET selection='selected' WHERE id=?",(cid,));con.commit();con.close()
            pipeline.run(db,fetch);con=storage.connect(db)
            self.assertEqual(storage.get_candidate(con,cid)['selection'],'selected')
            self.assertEqual(len(storage.detail_view(con,cid)['appearances']),2)
            self.assertEqual(storage.get_candidate(con,cid)['event_time'],None)
            con.close()

    def test_parsers_and_historical_filter(self):
        f=fixture_urls(); a=collect.parse_toutiao(f[collect.URLS['toutiao']])[0];b=collect.parse_baidu(f[collect.URLS['baidu']])[0];v=collect.parse_bilibili(f[collect.URLS['bilibili']])[0]
        w=collect.parse_tophub(f[collect.URLS['tophub']],'weibo')[0]
        self.assertEqual(a['cover_url'],'https://example.com/a.jpg');self.assertEqual(b['metrics']['hotScore'],'99')
        self.assertIsNotNone(v['published_at']);self.assertIsNone(v['event_time']);self.assertEqual(w['platform_snapshot_time'],'14分钟前')
        with tempfile.TemporaryDirectory() as tmp:
            con=storage.connect(Path(tmp)/'x.db');batch,_=storage.begin_batch(con)
            a['source_type']='historical';storage.save_rows(con,batch,[a,b])
            self.assertEqual(storage.list_candidates(con,source_type='historical')['total'],1)
            self.assertEqual(len(storage.list_candidates(con)['items'][0]['possible_duplicates']),1)
            con.close()

class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=str(Path(self.tmp.name)/'api.db');con=storage.connect(self.db);batch,_=storage.begin_batch(con)
        x=collect.parse_toutiao(fixture_urls()[collect.URLS['toutiao']])[0];storage.save_rows(con,batch,[x]);self.cid=storage.candidate_id(x);con.close()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.db=self.db
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,method='GET',data=None,headers=None):
        h={'Content-Type':'application/json'};h.update(headers or {})
        req=urllib.request.Request(self.base+path, data=json.dumps(data).encode() if data is not None else None,headers=h,method=method)
        try:
            with urllib.request.urlopen(req,timeout=5) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def test_selection_details_and_confirmation_lifecycle(self):
        base='/api/candidates/'+self.cid
        self.assertEqual(self.request(base+'/details','POST',{'manual_text':'事实'})[0],409)
        self.assertEqual(self.request(base+'/draft','PUT',{'title':'标题','body':'正文'})[0],409)
        self.assertEqual(self.request(base+'/confirm','POST',{'revision':1})[0],409)
        self.assertEqual(self.request('/api/candidates/selection','POST',{'ids':[self.cid],'selection':'selected'})[0],200)
        status,data=self.request(base+'/details','POST',{'manual_text':'人手记录','source_url':'https://example.com/news'})
        self.assertEqual(status,200);self.assertEqual(data['detail']['provenance'],'manual');self.assertEqual(data['detail']['source_url'],'https://example.com/news')
        self.assertEqual(self.request(base+'/details','POST',{'source_url':'http://127.0.0.1/'})[1]['detail']['status'],'blocked')
        self.assertEqual(self.request(base+'/draft','PUT',{'title':'标题','body':'正文'})[1]['draft']['revision'],1)
        self.assertEqual(self.request(base+'/confirm','POST',{'revision':1})[1]['draft']['confirmed_revision'],1)
        self.assertEqual(self.request(base+'/details','POST',{'manual_text':'补充证据'})[0],200)
        self.assertIsNone(self.request(base)[1]['draft']['confirmed_revision'])
        self.request(base+'/confirm','POST',{'revision':1})
        self.request('/api/candidates/selection','POST',{'ids':[self.cid],'selection':'deferred'})
        self.request('/api/candidates/selection','POST',{'ids':[self.cid],'selection':'selected'})
        self.assertIsNone(self.request(base)[1]['draft']['confirmed_revision'])
        self.assertEqual(self.request(base+'/draft','PUT',{'title':'标题','body':'新正文'})[1]['draft']['revision'],2)
        self.assertEqual(self.request(base+'/confirm','POST',{'revision':1})[0],409)
        self.assertEqual(self.request('/api/candidates/selection','POST',{'ids':[self.cid],'selection':'selected'},{'Origin':'https://example.org'})[0],403)

if __name__=='__main__': unittest.main()
