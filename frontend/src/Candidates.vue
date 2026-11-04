<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { creation } from './workflow'
import { hotlistLinks } from './sources'
import SessionTimeline from './SessionTimeline.vue'
import DraftSelectionBar from './DraftSelectionBar.vue'
import Icon from './Icon.vue'
const router=useRouter(), route=useRoute()
const columns=ref([]), selected=ref([]), pendingViews=ref({}), pages=ref({}), expanded=ref({})
const sourceFilters=ref({}), timeline=ref(null)
const loading=ref(true), error=ref(''), detail=ref(null), record=ref(null)
function process(s, kind='screening'){timeline.value={batchId:s.batch.id,kind}}
let timer, disposed=false, initialized=false
const ready=state=>(state.review?.events||[]).filter(e=>e.comparison?.selected).sort((a,b)=>(a.ranking?.rank||a.comparison.rank||0)-(b.ranking?.rank||b.comparison.rank||0))
const key=(s,e)=>`${s.line.id}:${e.id}`
const item=(s,ref)=>s.items.find(i=>i.ref===ref)
const isPending=s=>pendingViews.value[s.line.id]||!ready(s).length
const showRows=s=>expanded.value[s.line.id]??(columns.value.length===1||ready(s).length>0)
const busy=s=>['queued','screening'].includes(s.status)
const progress=s=>({ended:'已结束',queued:'排队中',screening:'筛选中',partial:'部分完成',failed:'筛选失败',stopped:'已停止',completed:'筛选完成'}[s.status]||'待筛选')
function rows(s){return isPending(s)?s.items.filter(i=>(s.pending_refs||[]).includes(i.ref)).map(i=>({...s.review?.events.find(e=>e.refs.includes(i.ref)),id:i.ref,refs:[i.ref]})):ready(s)}
function sourceOptions(s){return [...new Map(s.items.filter(i=>i.source_id).map(i=>[i.source_id,{value:i.source_id,text:i.source_name||i.source_id}])).values()]}
function filteredRows(s){const sources=sourceFilters.value[s.line.id]||[];return rows(s).filter(row=>!sources.length||row.refs.some(ref=>sources.includes(item(s,ref)?.source_id)))}
function filterSources(s,filters){if('source' in filters){sourceFilters.value[s.line.id]=[...filters.source];pages.value[s.line.id]=1}}
const configRevision=ref(0), savingLine=ref('')
let sequence=0
function draftRound(s,e) {
 return s.draft_rounds?.find(r=>r.method==='ai' && r.batch_id===e.batch_id)
}
function verdict(s,e) {
 return draftRound(s,e)?.choices.find(c=>c.id===e.id && c.version===e.version)
}
function locked(s,e) {
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
function queued(s,e){const r=draftRound(s,e);return r?.status==='sent'&&r.ids.includes(e.id)}
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
function inspect(s,e){detail.value={state:s,event:e}}
function sourceIssue(text){return /extension protocol|code=unsupported|浏览器扩展协议/.test(text||'')?'浏览器扩展版本不兼容，更新后可重试。':text}
watch(selected,persist,{deep:true})
watch(()=>route.params.recordId,()=>{timeline.value=null;initialized=false;selected.value=[];columns.value=[];loading.value=true;refresh()})
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
   <div class="section-heading"><div class="column-heading"><h2>{{s.line.name}}</h2><span class="status-pill" :class="busy(s)?'progress':['partial','failed'].includes(s.status)?'warning':'neutral'">{{progress(s)}}</span><span class="muted">{{s.line.format==='digest'?'集合稿':'单篇'}}</span></div><div class="actions"><el-button class="process-button" size="small" :aria-label="`查看${s.line.name}筛选过程`" @click="process(s)">筛选过程</el-button><el-button v-if="s.has_selection_calls||s.draft_rounds?.length" class="process-button" size="small" :aria-label="`查看${s.line.name}选稿过程`" @click="process(s,'selection')">选稿过程</el-button></div></div>
   <DraftSelectionBar :state="s" :readonly="s.historical" :saving="!!savingLine" @change="change=>changeDraft(s,change)" />
   <div class="shortlist-bar"><div class="filter-tabs"><button :class="{active:!isPending(s)}" :disabled="!ready(s).length" @click="pendingViews[s.line.id]=false;pages[s.line.id]=1;expanded[s.line.id]=true">可做稿 {{ready(s).length}}</button><button :class="{active:isPending(s)}" @click="pendingViews[s.line.id]=true;pages[s.line.id]=1;expanded[s.line.id]=true">待处理 {{s.pending_refs?.length||0}}</button></div><span class="muted">有效素材 {{s.pool?.eligible||0}}</span></div>
   <el-table v-if="showRows(s)" :data="filteredRows(s).slice(((pages[s.line.id]||1)-1)*20,(pages[s.line.id]||1)*20)" row-key="id" :empty-text="sourceFilters[s.line.id]?.length?'所选来源暂无内容':busy(s)?'正在筛选…':'暂无内容'" @filter-change="filters=>filterSources(s,filters)" @row-click="e=>inspect(s,e)">
    <el-table-column v-if="!isPending(s)" label="选择" width="100"><template #default="{row}"><span v-if="queued(s,row)" class="row-state">已入队</span><el-checkbox v-else-if="canPick(s,row)" :model-value="selected.includes(key(s,row))" @change="value=>pick(s,row,value)" :aria-label="`选择 ${item(s,row.refs[0])?.title}`" @click.stop/><span v-else class="row-state">{{draftRound(s,row)?.status==='judging'?'选稿中':'—'}}</span></template></el-table-column>
    <el-table-column label="标题" min-width="300"><template #default="{row}"><button class="candidate-title" @click.stop="inspect(s,row)">{{item(s,row.refs[0])?.title}}</button></template></el-table-column>
    <el-table-column label="来源" width="160" column-key="source" :filters="sourceOptions(s)" :filtered-value="sourceFilters[s.line.id]" :filter-multiple="true"><template #filter-icon><button class="table-filter-button" :class="{active:sourceFilters[s.line.id]?.length}" :aria-label="`${s.line.name}来源筛选`"><Icon name="filter" :size="14"/></button></template><template #default="{row}">{{[...new Set(row.refs.map(r=>item(s,r)?.source_name))].filter(Boolean).join('、')}}</template></el-table-column>
    <el-table-column label="判断" min-width="240"><template #default="{row}"><template v-if="!isPending(s) && verdict(s,row)"><strong>{{verdict(s,row).use}}</strong> · {{verdict(s,row).reason}}</template><template v-else>{{sourceIssue(row.error)||row.comparison?.reason||row.assessment?.reason||row.plan?.reason||'待筛选'}}</template></template></el-table-column>
   </el-table>
   <el-pagination v-if="showRows(s)&&filteredRows(s).length>20" v-model:current-page="pages[s.line.id]" :page-size="20" :total="filteredRows(s).length" layout="prev,pager,next"/>
  </section>
  <div v-if="count" class="selection-actions"><el-button type="primary" @click="create">制作所选（{{count}}）</el-button></div>
  <SessionTimeline :model-value="!!timeline" :batch-id="timeline?.batchId" :kind="timeline?.kind" @update:model-value="value=>{if(!value)timeline=null}"/>
  <el-drawer :model-value="!!detail" title="来源与依据" size="560px" @close="detail=null"><template v-if="detail"><h2>{{item(detail.state,detail.event.refs[0])?.title}}</h2><p>{{detail.event.comparison?.reason||detail.event.assessment?.reason||detail.event.plan?.reason||'尚未筛选'}}</p><p v-for="message in [...new Set(Object.values(detail.event.source_errors||{}))]" :key="message" class="action-error">{{sourceIssue(message)}}</p><div v-for="m in detail.event.materials||[]" :key="m.material_id" class="source-entry"><a v-if="m.status==='read'" :href="m.final_url||m.url" target="_blank" rel="noopener noreferrer">{{m.title||m.publisher}} ↗</a></div><div v-for="link in hotlistLinks(detail.event.refs.map(r=>item(detail.state,r)).filter(Boolean))" :key="link.url" class="source-entry"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{link.label}} ↗</a></div></template></el-drawer>
 </section>
</template>
<style scoped>
.topics-heading,.column-heading{display:flex;align-items:center;gap:8px;min-width:0}.record-time{font-size:13px;color:#859188;margin-left:8px}.topic-column{padding:24px;margin-bottom:16px}.topic-column .section-heading{flex-wrap:wrap;margin-bottom:16px;gap:8px}.topic-column .el-pagination{justify-content:flex-end;margin-top:16px}.row-state{font-size:12px;color:#7c8983}.selection-actions{display:flex;justify-content:flex-end;position:sticky;bottom:0;padding:12px 0;background:#f5f7f7;z-index:4}.table-filter-button{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:0;padding:2px;background:none;color:inherit}.table-filter-button.active,.table-filter-button:hover{color:#207469}
@media(max-width:580px){.topic-column{padding:16px}.topic-column .section-heading>.actions{width:100%;justify-content:flex-end}}
</style>
