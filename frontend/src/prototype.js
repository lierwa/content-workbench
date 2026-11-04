// Same views as production; all mutations stay in this page's memory.
import initial from './prototype-data.json'
import draftRules from '../../single_pass/draft_selection_rules.txt?raw'
import { lineSchedule } from './screenSchedule'
const copy=value=>JSON.parse(JSON.stringify(value))
const data=copy(initial)
const scene=new URLSearchParams(location.search).get('scene')
if(scene==='empty'){data.columns.lines=[];data.production={jobs:[],works:[],accounts:[],busy:false};data.states={};data.pool={items:[]}}
let serial=0
let collectionSettings={revision:0,scope:'all',line_ids:[],source_ids:[],per_source_limit:null,interval_minutes:null,schedule_mode:'manual',daily_times:['09:00']}
function version(body){if(body?.expected_revision!==data.columns.revision)throw new Error('栏目配置已变化，请刷新后再保存')}
function validate(line){if(!line.name?.trim())throw new Error('请填写栏目名称');if(!line.sources?.length)throw new Error('请选择渠道');if(!line.rules?.trim())throw new Error('请填写栏目定位与范围');for(const [stage,label] of [['initial','初筛'],['selection','二筛'],['verification','三筛']])if(!line.screening_rules?.[stage]?.trim())throw new Error(`请填写${label}规则`);if(!Number.isInteger(line.max_candidates)||line.max_candidates<1||line.max_candidates>10)throw new Error('单轮取材上限须为 1 至 10 条');if(data.columns.lines.some(l=>l.id!==line.id&&l.name===line.name))throw new Error('已存在同名栏目')}
export async function request(path,body,method='POST'){
  if(scene==='error')throw new Error('加载失败，请重试')
  const url=new URL(path,'http://prototype.local'), route=url.pathname
  if(body===undefined&&route==='/api/columns')return copy({...data.columns,lines:data.columns.lines.map(l=>({...l,has_history:!!data.states[l.id],running:false}))})
  if(body===undefined&&route==='/api/topics')return {columns:await Promise.all(data.columns.lines.map(l=>request('/api/state?line_id='+l.id))),concurrency:2,revision:data.columns.revision}
  if(body && route==='/api/topics/draft-prompt' && body.action==='open'){
    const line=data.columns.lines.find(l=>l.id===body.line_id)
    if(!line)throw new Error('栏目不存在')
    return {rules:line.draft_rules||draftRules.trim(),limit:line.draft_limit??2,default_rules:draftRules.trim(),revision:data.columns.revision,titles:[],sample:null}
  }
  if(body && route==='/api/topics/automation' && body.action==='settings'){
    version(body)
    const line=data.columns.lines.find(l=>l.id===body.line_id)
    if(!line)throw new Error('栏目不存在')
    Object.assign(line,body.settings); data.columns.revision++
    return {ok:true}
  }
  if(body===undefined&&route==='/api/collection')return copy({...data.columns,settings:collectionSettings,run:{status:'idle',sources:[]}})
  if(body===undefined&&route==='/api/collection/history')return {items:[],total:0,page:1,page_size:20}
  if(body&&route==='/api/collection/settings'){if(body.expected_revision!==collectionSettings.revision)throw new Error('采集设置已变化');collectionSettings={...body.settings,revision:collectionSettings.revision+1};return request('/api/collection')}
  if(body&&['/api/screen/batch','/api/production/batch'].includes(route))throw new Error('交互原型不执行筛选或制作，请进入实际工作台')
  if(body===undefined && route==='/api/screen/options') return {
    revision:data.columns.revision, batches:[], columns:data.columns.lines.map(line=>({
      line_id:line.id, name:line.name, hours:line.valid_hours, enabled:line.enabled!==false, running:false, schedule:lineSchedule(line),
      schedule_scope:line.screen_scope || {unit:'hours',count:line.valid_hours}
    }))
  }
  if(body && route==='/api/screen/schedule'){
    version(body)
    for(const line of data.columns.lines.filter(l=>body.line_ids.includes(l.id))){
      if(body.scopes?.[line.id]) line.screen_scope=copy(body.scopes[line.id])
      line.interval_minutes = body.schedule.mode==='interval' ? body.schedule.minutes : null
      if(['weekly','daily'].includes(body.schedule.mode)) line.screen_calendar=copy(body.schedule)
      else delete line.screen_calendar
    }
    data.columns.revision++
    return {ok:true}
  }
  if(body && route==='/api/screen/preview'){
    const columns=body.columns.map(spec=>{
      const state=data.states[spec.line_id]
      const pending=new Set(state?.pending_refs || [])
      const items=(state?.items || []).filter(item=>{
        const stamp=Date.parse(item.published_at || item.first_seen_at)
        return pending.has(item.ref) && stamp>=Date.parse(spec.start) && stamp<=Date.parse(spec.end)
      })
      return {line_id:spec.line_id, items}
    })
    return {columns}
  }
  if(body && route==='/api/screen/start')throw new Error('交互原型不执行筛选，请进入实际工作台')
  if(body===undefined&&route==='/api/production')return copy(data.production)
  if(body===undefined&&route==='/api/pool'){
    const statuses={pending:'待筛选',running:'筛选中',selected:'可做稿',dropped:'未采用',not_selected:'本轮未选',insufficient:'待补证',stale:'待复筛',expired:'已过期',unassigned:'未分配栏目'}
    const all=data.pool?.items||[...new Map(Object.values(data.states).flatMap(s=>s.items||[]).map(i=>[i.ref,i])).values()]
    const rows=all.map(i=>{
      const source=data.columns.sources.find(s=>s.id===(i.source_id||i.source))
      const decisions=data.columns.lines.filter(l=>l.sources.includes(i.source_id||i.source)).map(l=>{
        const saved=i.decisions?.find(d=>d.line_id===l.id)
        if(saved)return saved
        const e=data.states[l.id]?.review?.events?.find(e=>e.refs?.includes(i.ref))
        const status=e?.comparison?.selected?'selected':e?.stage==='drop'?'dropped':'pending'
        return {line_id:l.id,name:l.name,status,label:statuses[status],reason:e?.comparison?.reason||e?.plan?.reason||''}
      })
      return {...i,source_id:i.source_id||i.source,source_name:i.source_name||source?.name,category:source?.category||'其他',decisions}
    })
    const f=Object.fromEntries(url.searchParams),page=Number(f.page||1),size=Number(f.page_size||30)
    if(f.batch_id)throw new Error('原型没有采集批次，请清除批次筛选后查看')
    const filtered=rows.filter(i=>(!f.q||(i.title+' '+(i.summary||'')).toLowerCase().includes(f.q.toLowerCase()))&&(!f.source_id||i.source_id===f.source_id)&&(!f.category||i.category===f.category)&&(!f.line_id||i.decisions.some(d=>d.line_id===f.line_id))&&(!f.status||(f.status==='unassigned'?!i.decisions.length:i.decisions.some(d=>(!f.line_id||d.line_id===f.line_id)&&d.status===f.status)))&&(!f.hours||Date.now()-new Date(i.first_seen_at).getTime()<=Number(f.hours)*3600000))
    return copy({items:filtered.slice((page-1)*size,page*size),total:filtered.length,pool_total:rows.length,page,page_size:size,lines:data.columns.lines,sources:data.columns.sources,categories:[...new Set(data.columns.sources.map(s=>s.category||'其他'))],statuses,collecting:false})
  }
  if(body===undefined&&route==='/api/state'){
    const line=data.columns.lines.find(l=>l.id===url.searchParams.get('line_id'))||data.columns.lines[0]
    const empty={status:'idle',items:[],results:[],review:{events:[]},pool:{eligible:0},pending_refs:[],selection_current:false}
    const state=copy(data.states[line?.id]||empty)
    if(line&&!data.states[line.id]){state.items=Object.values(data.states).flatMap(s=>s.items).filter(i=>line.sources.includes(i.source||i.source_id));state.pending_refs=state.items.map(i=>i.ref);state.pool.eligible=state.items.length}
    return {...state,line,lines:copy(data.columns.lines),collection:{status:'idle',sources:[]},collection_sources:copy(data.columns.sources)}
  }
  if(route==='/api/columns'&&body){version(body);const line={...copy(body.column),id:'prototype_'+(++serial)};validate(line);data.columns.lines.push(line);data.columns.revision++;return copy(data.columns)}
  let match=route.match(/^\/api\/columns\/([^/]+)$/)
  if(match){version(body);const index=data.columns.lines.findIndex(l=>l.id===match[1]);if(index<0)throw new Error('栏目不存在');if(method==='DELETE'){if(data.states[match[1]])throw new Error('已有内容的栏目请停用');data.columns.lines.splice(index,1)}else{const line={...data.columns.lines[index],...body.column};validate(line);data.columns.lines[index]=line}data.columns.revision++;return copy(data.columns)}
  match=route.match(/^\/api\/sources\/([^/]+)$/)
  if(match){version(body);Object.assign(data.columns.sources.find(s=>s.id===match[1]),body.source);data.columns.revision++;return copy(data.columns)}
  match=route.match(/^\/api\/production\/jobs\/([^/]+)(?:\/(sources|entries|body-image|cover))?$/)
  if(match){const job=data.production.jobs.find(j=>j.id===match[1]);if(!job)throw new Error('稿件不存在');if(match[2]==='sources')return {sources:copy(data.sources[job.id]||[])};if(match[2]==='body-image'||match[2]==='cover')throw new Error('原型不上传文件');if(body.expected_revision!==job.revision)throw new Error('稿件已变化，请刷新后再保存');if(match[2]==='entries')throw new Error('原型不调用 AI 重新组稿');Object.assign(job.draft,body.changes);if(body.changes?.cover_url===null)Object.assign(job.draft,{cover_sha256:null,cover_kind:null,cover_source:null});job.revision++;job.editorial_approved=false;job.stage='ready_review';return copy(job)}
  if(route==='/api/production/accounts'&&body){data.production.accounts.push({...body,id:'prototype_account_'+(++serial),status:'needs_login',capabilities:{connect:false,publish:false}});return {ok:true}}
  if(['/api/screen','/api/collect','/api/production/jobs','/api/production/actions'].includes(route)||route.endsWith('/connect'))throw new Error('原型不执行采集、AI 或平台操作')
  throw new Error('原型中没有此操作')
}
