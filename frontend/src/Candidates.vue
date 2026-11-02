<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { creation } from './workflow'
import { hotlistLinks } from './sources'
import ScreeningProcessDrawer from './ScreeningProcessDrawer.vue'
import ScreenRunDialog from './ScreenRunDialog.vue'
import Icon from './Icon.vue'
const router=useRouter(), route=useRoute()
const columns=ref([]), lineIds=ref([]), selected=ref([]), pendingViews=ref({}), pages=ref({}), expanded=ref({})
const sourceFilters=ref({}), screenOpen=ref(false)
const loading=ref(true), error=ref(''), detail=ref(null), processLine=ref(null)
const processColumn=computed(()=>columns.value.find(s=>s.line.id===processLine.value)||null)
let timer, disposed=false, initialized=false
const visible=computed(()=>columns.value.filter(s=>lineIds.value.includes(s.line.id)))
const allIds=computed(()=>columns.value.filter(s=>s.line.enabled!==false).map(s=>s.line.id))
const allSelected=computed(()=>allIds.value.length>0&&allIds.value.every(id=>lineIds.value.includes(id)))
function toggleAll(checked){lineIds.value=checked?[...allIds.value]:[]}
const ready=state=>(state.review?.events||[]).filter(e=>e.comparison?.selected).sort((a,b)=>(a.ranking?.rank||a.comparison.rank||0)-(b.ranking?.rank||b.comparison.rank||0))
const key=(s,e)=>`${s.line.id}:${e.id}`
const item=(s,ref)=>s.items.find(i=>i.ref===ref)
const isPending=s=>pendingViews.value[s.line.id]||!ready(s).length
const showRows=s=>expanded.value[s.line.id]??(lineIds.value.length===1||ready(s).length>0)
const busy=s=>['queued','screening'].includes(s.status)
const progress=s=>({queued:'排队中',screening:'筛选中',partial:'部分完成',failed:'筛选失败',stopped:'已停止',completed:'筛选完成'}[s.status]||'待筛选')
function rows(s){return isPending(s)?s.items.filter(i=>(s.pending_refs||[]).includes(i.ref)).map(i=>({...s.review?.events.find(e=>e.refs.includes(i.ref)),id:i.ref,refs:[i.ref]})):ready(s)}
function sourceOptions(s){return [...new Map(s.items.filter(i=>i.source_id).map(i=>[i.source_id,{value:i.source_id,text:i.source_name||i.source_id}])).values()]}
function filteredRows(s){const sources=sourceFilters.value[s.line.id]||[];return rows(s).filter(row=>!sources.length||row.refs.some(ref=>sources.includes(item(s,ref)?.source_id)))}
function filterSources(s,filters){if('source' in filters){sourceFilters.value[s.line.id]=[...filters.source];pages.value[s.line.id]=1}}
const chosen=computed(()=>columns.value.filter(s=>s.line.enabled!==false&&s.selection_current).map(s=>({state:s,events:ready(s).filter(e=>selected.value.includes(key(s,e)))})).filter(g=>g.events.length))
const count=computed(()=>chosen.value.reduce((n,g)=>n+g.events.length,0))
function persist(){
 for(const s of columns.value){creation.selections[s.line.id]={batchId:s.batch?.id,format:s.line.format,targets:[],events:ready(s).filter(e=>selected.value.includes(key(s,e))).map(e=>({id:e.id,version:e.version,batchId:e.batch_id||s.batch?.id,title:item(s,e.refs[0])?.title}))}}
}
async function refresh(){
 try{const data=await api('/api/topics');columns.value=data.columns
 if(!initialized){lineIds.value=route.query.column?[route.query.column]:columns.value.filter(s=>s.line.enabled!==false).map(s=>s.line.id);selected.value=columns.value.flatMap(s=>ready(s).filter(e=>creation.selections[s.line.id]?.events.some(x=>x.id===e.id&&x.version===e.version)).map(e=>key(s,e)));initialized=true}
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
watch(()=>route.query.column,id=>{if(initialized)lineIds.value=id?[id]:columns.value.filter(s=>s.line.enabled!==false).map(s=>s.line.id)})
onMounted(poll);onBeforeUnmount(()=>{disposed=true;clearTimeout(timer);persist()})
</script>
<template>
 <section class="topics-page">
  <header class="page-heading">
   <div class="topics-heading"><h1>选题</h1><el-select v-model="lineIds" multiple collapse-tags :max-collapse-tags="2" collapse-tags-tooltip aria-label="筛选栏目" class="column-select"><template #header><el-checkbox :model-value="allSelected" :indeterminate="!!lineIds.length&&!allSelected" @change="toggleAll">全部栏目</el-checkbox></template><el-option v-for="s in columns" :key="s.line.id" :label="s.line.name" :value="s.line.id"/></el-select></div>
   <div class="actions topics-actions"><el-button :disabled="!lineIds.length" @click="screenOpen=true">更新选题</el-button><el-button type="primary" :disabled="!count" @click="create">加入制作队列{{count?`（${count}）`:''}}</el-button></div>
  </header>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <div v-if="loading" class="loading-state">正在加载…</div>
  <div v-if="!loading&&!visible.length" class="empty-state">请选择要查看的栏目</div>
  <section v-for="s in visible" :key="s.line.id" class="topic-column surface">
   <div class="section-heading"><div class="actions"><h2>{{s.line.name}}</h2><span class="status-pill" :class="busy(s)?'progress':['partial','failed'].includes(s.status)?'warning':'neutral'">{{progress(s)}}</span><el-tooltip content="查看筛选过程"><button class="icon-button process-trigger" :aria-label="`查看${s.line.name}筛选过程`" @click="processLine=s.line.id"><Icon name="process"/></button></el-tooltip><span class="muted">{{s.line.format==='digest'?'集合稿':'单篇'}}</span></div></div>
   <div class="shortlist-bar"><div class="filter-tabs"><button :class="{active:!isPending(s)}" :disabled="!ready(s).length" @click="pendingViews[s.line.id]=false;pages[s.line.id]=1;expanded[s.line.id]=true">可做稿 {{ready(s).length}}</button><button :class="{active:isPending(s)}" @click="pendingViews[s.line.id]=true;pages[s.line.id]=1;expanded[s.line.id]=true">待处理 {{s.pending_refs?.length||0}}</button></div><span class="muted">有效素材 {{s.pool?.eligible||0}}</span></div>
   <el-table v-if="showRows(s)" :data="filteredRows(s).slice(((pages[s.line.id]||1)-1)*20,(pages[s.line.id]||1)*20)" row-key="id" :empty-text="sourceFilters[s.line.id]?.length?'所选来源暂无内容':busy(s)?'正在筛选…':'暂无内容'" @filter-change="filters=>filterSources(s,filters)" @row-click="e=>inspect(s,e)">
    <el-table-column width="48"><template #default="{row}"><el-checkbox v-if="!isPending(s)" :model-value="selected.includes(key(s,row))" @change="checked=>selected=checked?[...selected,key(s,row)]:selected.filter(k=>k!==key(s,row))" :disabled="!s.selection_current||s.line.enabled===false" :aria-label="`选择 ${item(s,row.refs[0])?.title}`" @click.stop/></template></el-table-column>
    <el-table-column label="标题" min-width="300"><template #default="{row}"><button class="candidate-title" @click.stop="inspect(s,row)">{{item(s,row.refs[0])?.title}}</button></template></el-table-column>
    <el-table-column label="来源" width="160" column-key="source" :filters="sourceOptions(s)" :filtered-value="sourceFilters[s.line.id]" :filter-multiple="true"><template #filter-icon><button class="table-filter-button" :class="{active:sourceFilters[s.line.id]?.length}" :aria-label="`${s.line.name}来源筛选`"><Icon name="filter" :size="14"/></button></template><template #default="{row}">{{[...new Set(row.refs.map(r=>item(s,r)?.source_name))].filter(Boolean).join('、')}}</template></el-table-column>
    <el-table-column label="判断" min-width="240"><template #default="{row}">{{sourceIssue(row.error)||row.comparison?.reason||row.assessment?.reason||row.plan?.reason||'待筛选'}}</template></el-table-column>
   </el-table>
   <el-pagination v-if="showRows(s)&&filteredRows(s).length>20" v-model:current-page="pages[s.line.id]" :page-size="20" :total="filteredRows(s).length" layout="prev,pager,next"/>
  </section>
  <ScreenRunDialog v-model="screenOpen" :initial-ids="lineIds" @started="refresh"/>
  <ScreeningProcessDrawer :column="processColumn" @close="processLine=null" @changed="refresh"/>
  <el-drawer :model-value="!!detail" title="来源与依据" size="560px" @close="detail=null"><template v-if="detail"><h2>{{item(detail.state,detail.event.refs[0])?.title}}</h2><p>{{detail.event.comparison?.reason||detail.event.assessment?.reason||detail.event.plan?.reason||'尚未筛选'}}</p><p v-for="message in [...new Set(Object.values(detail.event.source_errors||{}))]" :key="message" class="action-error">{{sourceIssue(message)}}</p><div v-for="m in detail.event.materials||[]" :key="m.material_id" class="source-entry"><a v-if="m.status==='read'" :href="m.final_url||m.url" target="_blank" rel="noopener noreferrer">{{m.title||m.publisher}} ↗</a></div><div v-for="link in hotlistLinks(detail.event.refs.map(r=>item(detail.state,r)).filter(Boolean))" :key="link.url" class="source-entry"><a :href="link.url" target="_blank" rel="noopener noreferrer">{{link.label}} ↗</a></div></template></el-drawer>
 </section>
</template>
<style scoped>
.table-filter-button{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:0;padding:2px;background:none;color:inherit}.table-filter-button.active,.table-filter-button:hover{color:#207469}
.topics-heading{display:flex;align-items:center;gap:16px;min-width:0}.topics-heading h1{white-space:nowrap}.column-select{width:300px;max-width:100%}.page-heading{flex-wrap:wrap;gap:16px}.topics-actions{margin-left:auto;gap:12px}.topic-column{padding:20px;margin-bottom:24px}.topic-column .section-heading{margin-bottom:12px}.topic-column .el-pagination{justify-content:flex-end;margin-top:12px}.process-trigger{color:#207469;width:28px;height:28px;margin-left:-4px}.topics-actions>span{display:inline-flex}@media(max-width:850px){.page-heading{align-items:flex-start}.topics-heading{max-width:100%}.column-select{width:280px}.topics-actions{width:100%;justify-content:flex-end}.topic-column{padding:12px}}
</style>
