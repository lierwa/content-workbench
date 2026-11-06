<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { creation } from './workflow'
import { draftRound, judgment, stageName, verdict, topicsByStage, screeningTabs } from './topicJudgment'
import { hotlistLinks } from './sources'
import SessionTimeline from './SessionTimeline.vue'
import ScreeningProcessDrawer from './ScreeningProcessDrawer.vue'
import DraftSelectionBar from './DraftSelectionBar.vue'
import Icon from './Icon.vue'
const router=useRouter(), route=useRoute()
const columns=ref([]), selected=ref([]), views=ref({}), pages=ref({})
const sourceFilters=ref({}), timeline=ref(null)
const decisionFilters=ref({})
const loading=ref(true), error=ref(''), detail=ref(null), record=ref(null)
const processBatch=ref(null)
const processColumn=computed(()=>columns.value.find(s=>s.batch?.id===processBatch.value))
function calls(s,kind){timeline.value={batchId:s.batch.id,kind}}
let timer, disposed=false, initialized=false
const ready=state=>(state.review?.events||[]).filter(e=>e.comparison?.selected).sort((a,b)=>(a.ranking?.rank||a.comparison.rank||0)-(b.ranking?.rank||b.comparison.rank||0))
const key=(s,e)=>`${s.line.id}:${e.id}`
const item=(s,ref)=>s.items.find(i=>i.ref===ref)
const byLine=computed(()=>Object.fromEntries(columns.value.map(s=>[s.line.id,topicsByStage(s)])))
const stages=s=>byLine.value[s.line.id]
function currentStage(s) {
 return views.value[s.line.id] || screeningTabs.findLast(tab=>stages(s)[tab.id].length)?.id || 'initial'
}
function changeStage(s,tabName) {
 views.value[s.line.id]=tabName.slice(s.line.id.length+1)
 pages.value[s.line.id]=1
}
const busy=s=>['queued','screening'].includes(s.status)
const progress=s=>({ended:'已结束',queued:'排队中',screening:'筛选中',partial:'部分完成',failed:'筛选失败',stopped:'已停止',completed:'筛选完成'}[s.status]||'待筛选')
function sourceOptions(s){return [...new Map(s.items.filter(i=>i.source_id).map(i=>[i.source_id,{value:i.source_id,text:i.source_name||i.source_id}])).values()]}
function rowsBySource(s,stage) {
 const rows=stages(s)[stage], sources=sourceFilters.value[s.line.id] || []
 return sources.length ? rows.filter(row=>row.e.refs.some(ref=>sources.includes(item(s,ref)?.source_id))) : rows
}
function decisionFilter(s,stage) {
 return decisionFilters.value[s.batch.id]?.[stage] || ''
}
function setDecisionFilter(s,stage,label) {
 (decisionFilters.value[s.batch.id] ??= {})[stage]=label
 pages.value[s.line.id]=1
}
function decisionOptions(s,stage) {
 const counts=new Map()
 for (const {decision} of rowsBySource(s,stage)) {
  counts.set(decision.label,(counts.get(decision.label) || 0)+1)
 }
 const selected=decisionFilter(s,stage)
 if (selected && !counts.has(selected)) counts.set(selected,0)
 return counts
}
function filteredRows(s,stage=currentStage(s)) {
 const rows=rowsBySource(s,stage), label=decisionFilter(s,stage)
 return label ? rows.filter(row=>row.decision.label===label) : rows
}
function filterSources(s,filters){if('source' in filters){sourceFilters.value[s.line.id]=[...filters.source];pages.value[s.line.id]=1}}
const configRevision=ref(0), savingLine=ref('')
let sequence=0
function locked(s,e) {
 if(e.production?.jobs?.length)return e.production.occupied
 const round=draftRound(s,e)
 if(!round)return false
 return round.status==='judging' || (round.ids.includes(e.id) && verdict(s,e) && ['ready','sent','failed'].includes(round.status))
}
async function changeDraft(s,change) {
 if(savingLine.value)return
 savingLine.value=s.line.id;error.value=''
 try { await api('/api/topics/automation',{line_id:s.line.id,expected_revision:configRevision.value,...change}) }
 catch(e){error.value=e.message}
 finally {await refresh();savingLine.value=''}
}
function productionLabel(s,e){if(e.production?.label)return e.production.label;const r=draftRound(s,e);return r?.status==='sent'&&r.ids.includes(e.id)?'制作记录待核对':''}
function canPick(s,e){return e.selectable!==false&&s.selection_current&&s.line.enabled!==false&&!locked(s,e)}
function pick(s,e,value) {
 if(!canPick(s,e))return
 selected.value=value?[...new Set([...selected.value,key(s,e)])]:selected.value.filter(k=>k!==key(s,e))
}
const chosen=computed(()=>columns.value.filter(s=>s.line.enabled!==false&&s.selection_current).map(s=>({state:s,events:ready(s).filter(e=>canPick(s,e)&&selected.value.includes(key(s,e)))})).filter(g=>g.events.length))
const count=computed(()=>chosen.value.reduce((n,g)=>n+g.events.length,0))
function persist(){
 for(const s of columns.value){creation.selections[s.line.id]={batchId:s.batch?.id,format:s.line.format,targets:[],events:ready(s).filter(e=>canPick(s,e)&&selected.value.includes(key(s,e))).map(e=>({id:e.id,version:e.version,batchId:e.batch_id||s.batch?.id,title:item(s,e.refs[0])?.title}))}}
}
async function refresh(){
 const request=++sequence
 try{const data=await api('/api/topic-records/'+encodeURIComponent(route.params.recordId));if(disposed||request!==sequence)return
 columns.value=data.columns;record.value=data.record;configRevision.value=data.revision
 if(!initialized){selected.value=columns.value.flatMap(s=>ready(s).filter(e=>creation.selections[s.line.id]?.events.some(x=>x.id===e.id&&x.version===e.version)).map(e=>key(s,e)));initialized=true}
 else selected.value=selected.value.filter(k=>columns.value.some(s=>s.selection_current&&ready(s).some(e=>key(s,e)===k&&creation.selections[s.line.id]?.events.some(x=>x.id===e.id&&x.version===e.version))))
 for(const s of columns.value){
  const sources=sourceFilters.value[s.line.id]||[],valid=sources.filter(id=>sourceOptions(s).some(option=>option.value===id))
  if(!sourceFilters.value[s.line.id]||valid.length!==sources.length)sourceFilters.value[s.line.id]=valid
  pages.value[s.line.id]=Math.min(pages.value[s.line.id]||1,Math.max(1,Math.ceil(filteredRows(s).length/20)))
 }
 persist()
 }catch(e){error.value=e.message}finally{loading.value=false}
}
async function poll(){await refresh();if(!disposed)timer=setTimeout(poll,3000)}
function create(){
 persist();creation.groups=[]
 for(const g of chosen.value){
  const events=creation.selections[g.state.line.id].events
  for(const batchId of [...new Set(events.map(e=>e.batchId))])creation.groups.push({lineId:g.state.line.id,lineName:g.state.line.name,batchId,format:g.state.line.format,events:events.filter(e=>e.batchId===batchId).map(e=>({...e}))})
 }
 creation.targets=[];router.push('/drafts/new')
}
function inspect(s,e,steps){
 const final = e.comparison?.selected!==undefined || verdict(s,e) ? judgment(s,e) : null
 detail.value={state:s,event:e,steps,final}
}
function sourceIssue(text){return /extension protocol|code=unsupported|浏览器扩展协议/.test(text||'')?'浏览器扩展版本不兼容，更新后可重试。':text}
watch(selected,persist,{deep:true})
watch(()=>route.params.recordId,()=>{timeline.value=null;processBatch.value=null;detail.value=null;views.value={};decisionFilters.value={};pages.value={};initialized=false;selected.value=[];columns.value=[];loading.value=true;refresh()})
onMounted(poll);onBeforeUnmount(()=>{disposed=true;clearTimeout(timer);persist()})
</script>
<template>
 <section class="topics-page">
  <header class="page-heading">
   <div class="topics-heading"><RouterLink to="/topics" class="back-link"><Icon name="back"/>选题记录</RouterLink><span v-if="record?.created_at" class="record-time">{{new Date(record.created_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}}</span></div>
  </header>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <div v-if="loading" class="loading-state">正在加载…</div>
  <div v-if="!loading&&!columns.length" class="empty-state">暂无栏目记录</div>
  <section v-for="s in columns" :key="s.line.id" class="topic-column surface">
   <div class="section-heading">
    <div class="column-heading"><h2>{{s.line.name}}</h2><span class="status-pill" :class="busy(s)?'progress':['partial','failed'].includes(s.status)?'warning':'neutral'">{{progress(s)}}</span></div>
    <div class="actions">
     <el-button class="process-button" size="small" :aria-label="`查看${s.line.name}筛选过程`" @click="processBatch=s.batch.id">筛选过程</el-button>
    </div>
   </div>
   <DraftSelectionBar :state="s" :readonly="s.historical" :saving="!!savingLine" @change="change=>changeDraft(s,change)" />
   <el-tabs class="screen-tabs" :model-value="s.line.id+'-'+currentStage(s)" @update:model-value="name=>changeStage(s,name)">
    <el-tab-pane v-for="tab in screeningTabs" :key="tab.id" :name="s.line.id+'-'+tab.id" :label="tab.label+' '+stages(s)[tab.id].length">
     <template v-if="tab.id===currentStage(s)">
      <el-table :data="filteredRows(s,tab.id).slice(((pages[s.line.id]||1)-1)*20,(pages[s.line.id]||1)*20)" :row-key="r=>r.e.id" :empty-text="decisionFilter(s,tab.id)?'没有符合筛选条件的选题':sourceFilters[s.line.id]?.length?'所选来源暂无内容':busy(s)?'正在筛选…':'暂无匹配选题'" @filter-change="filters=>filterSources(s,filters)" @row-click="row=>inspect(s,row.e,row.steps)">
       <el-table-column label="选题" min-width="155" column-key="source" :filters="sourceOptions(s)" :filtered-value="sourceFilters[s.line.id]" :filter-multiple="true"><template #filter-icon><button class="table-filter-button" :class="{active:sourceFilters[s.line.id]?.length}" :aria-label="s.line.name+'来源筛选'"><Icon name="filter" :size="14"/></button></template><template #default="{row:{e,steps}}">
        <button class="candidate-title" @click.stop="inspect(s,e,steps)" :title="item(s,e.refs[0])?.title||e.title">{{item(s,e.refs[0])?.title||e.title}}</button><div class="topic-meta"><span class="source-caption">{{[...new Set(e.refs.map(r=>item(s,r)?.source_name))].filter(Boolean).join('、')}}</span>
        <span v-if="tab.id==='verification' && productionLabel(s,e) && (e.production?.occupied || !canPick(s,e))" class="row-state">{{productionLabel(s,e)}}</span><el-checkbox v-else-if="tab.id==='verification' && e.comparison?.selected && canPick(s,e)" :model-value="selected.includes(key(s,e))" @change="value=>pick(s,e,value)" :aria-label="'选择 '+item(s,e.refs[0])?.title" @click.stop>选入制作</el-checkbox></div></template></el-table-column>
       <el-table-column label="本轮判断" min-width="128">
        <template #header>
         <el-dropdown trigger="click" placement="bottom-start" @command="label=>setDecisionFilter(s,tab.id,label)">
          <button type="button" class="decision-filter" :class="{active:decisionFilter(s,tab.id)}" :aria-label="s.line.name+tab.label+'判断筛选'">
           {{decisionFilter(s,tab.id) || '本轮判断'}}<Icon name="filter" :size="14"/>
          </button>
          <template #dropdown><el-dropdown-menu>
           <el-dropdown-item command="">全部判断</el-dropdown-item>
           <el-dropdown-item v-for="[label,count] in decisionOptions(s,tab.id)" :key="label" :command="label" :class="{'current-decision':decisionFilter(s,tab.id)===label}">{{label}}（{{count}}）</el-dropdown-item>
          </el-dropdown-menu></template>
         </el-dropdown>
        </template>
        <template #default="{row}">
        <div class="table-decision"><span class="decision-tag" :class="row.decision.tone">{{row.decision.label}}</span><span class="short-reason">{{row.shortReason}}</span></div>
       </template></el-table-column>
      </el-table>
      <el-pagination v-if="filteredRows(s,tab.id).length>20" v-model:current-page="pages[s.line.id]" :page-size="20" :total="filteredRows(s,tab.id).length" layout="prev,pager,next"/>
     </template>
    </el-tab-pane>
   </el-tabs>
  </section>
  <div v-if="count" class="selection-actions"><el-button type="primary" @click="create">制作所选（{{count}}）</el-button></div>
  <ScreeningProcessDrawer :column="processColumn" @close="processBatch=null" @changed="refresh" @trace="kind=>calls(processColumn,kind)" />
  <SessionTimeline :model-value="!!timeline" :batch-id="timeline?.batchId" :kind="timeline?.kind" @update:model-value="value=>{if(!value)timeline=null}"/>
  <el-drawer :model-value="!!detail" title="选题依据" size="560px" @close="detail=null"><template v-if="detail">
   <h2>{{item(detail.state,detail.event.refs[0])?.title}}</h2>
   <div v-for="step in detail.steps" :key="step.stage" class="decision-step"><b>{{stageName(step)}} · {{step.label}}</b><span v-if="step.rank">（第 {{step.rank}} 位）</span><p>{{step.note}}</p></div>
   <div v-if="detail.final" class="decision-step"><b>终选 · {{detail.final.label}}</b><p>{{detail.final.note}}</p><p v-if="detail.final.angle && detail.final.angle!==detail.final.note">稿件看点：{{detail.final.angle}}</p>
    <blockquote v-if="verdict(detail.state,detail.event)?.quote && verdict(detail.state,detail.event)?.quote!==detail.final.note">{{verdict(detail.state,detail.event).quote}}</blockquote>
   </div>
   <p v-if="verdict(detail.state,detail.event)?.error" class="action-error">{{verdict(detail.state,detail.event).error}}</p>
   <p v-if="detail.event.error" class="action-error">{{sourceIssue(detail.event.error)}}</p>
   <p v-for="message in [...new Set(Object.values(detail.event.source_errors||{}))]" :key="message" class="action-error">{{sourceIssue(message)}}</p>
   <div v-for="m in detail.event.materials||[]" :key="m.material_id" class="source-entry"><a v-if="m.status==='read'" :href="m.final_url||m.url" target="_blank" rel="noopener noreferrer">{{m.title||m.publisher}} ↗</a></div><div v-for="link in hotlistLinks(detail.event.refs.map(r=>item(detail.state,r)).filter(Boolean))" :key="link.url" class="source-entry"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{link.label}} ↗</a></div></template></el-drawer>
 </section>
</template>
<style scoped>
.screen-tabs :deep(.el-tabs__header){margin-bottom:10px}
.decision-filter{display:inline-flex;align-items:center;gap:4px;padding:2px 0;border:0;background:none;color:var(--el-text-color-secondary);font:inherit;font-weight:600}
.decision-filter.active,.decision-filter:hover,.current-decision{color:#207469}
.table-decision{display:grid;align-items:center;justify-items:start;gap:3px;min-width:0}
.decision-tag{padding:0 6px;border-radius:4px;background:#edf1ef;color:#44524b;font-size:12px;line-height:20px;white-space:nowrap}
.decision-tag.pick{background:#dcefe7;color:#205f48}
.decision-tag.hold{background:#fcf0d8;color:#795319}
.short-reason,.candidate-title,.source-caption{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.short-reason{font-size:13px;color:#56665e}
.topic-meta{display:flex;align-items:center;gap:8px;min-width:0;font-size:12px;color:#687a70}
.topic-meta .el-checkbox{height:20px}
.topic-meta .row-state{flex-shrink:0}
.topic-column{container-type:inline-size}
@container (min-width: 720px){.table-decision{grid-template-columns:max-content minmax(0,1fr);gap:10px}.short-reason{width:100%}}
.decision-step{padding:10px 0;border-top:1px solid #e1e7e4}.decision-step p{margin:.3em 0}

.topics-heading,.column-heading{display:flex;align-items:center;gap:8px;min-width:0}.record-time{font-size:13px;color:#859188;margin-left:8px}.topic-column{padding:24px;margin-bottom:16px}.topic-column .section-heading{flex-wrap:wrap;margin-bottom:16px;gap:8px}.topic-column .el-pagination{justify-content:flex-end;margin-top:16px}.row-state{font-size:12px;color:#7c8983}.selection-actions{display:flex;justify-content:flex-end;position:sticky;bottom:0;padding:12px 0;background:#f5f7f7;z-index:4}.table-filter-button{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:0;padding:2px;background:none;color:inherit}.table-filter-button.active,.table-filter-button:hover{color:#207469}
@media(max-width:580px){.topic-column{padding:16px}.topic-column .section-heading>.actions{gap:6px}}
</style>
