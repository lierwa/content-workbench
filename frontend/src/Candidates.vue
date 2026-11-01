<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api'
import { creation, rememberSelection, switchSelection } from './workflow'
import { hotlistLinks } from './sources'
const router = useRouter()
const route = useRoute()
const state = ref({ items: [], results: [], review: null })
const lineId = ref(route.query.column || creation.lineId || '')
const loading = ref(true)
const sending = ref(false)
const requestError = ref('')
const fetchError = ref('')
const selectedEvents = ref(creation.events.map(e => e.id))
const showPending = ref(false)
const detailKey = ref(null)
const detailRef = ref(null)
const detailOpen = ref(false)
const page = ref(1)
const pageSize = 20
let timer
let disposed = false
const review = computed(() => state.value.review)
const collectionBusy = computed(() => state.value.collection?.status === 'running')
const lineSources = computed(() => (state.value.collection_sources || []).filter(s => state.value.line?.sources.includes(s.id)))
const reviewBusy = computed(() => state.value.status === 'screening')
const busy = computed(() => sending.value || reviewBusy.value)
const currentPolicy = computed(() => state.value.selection_current === true)
const itemByRef = computed(() => new Map(state.value.items.map(i => [i.ref, i])))
const finalRows = computed(() => (review.value?.events || []).filter(e => e.comparison?.selected).sort((a,b) => (a.ranking?.rank || a.comparison.rank || 0) - (b.ranking?.rank || b.comparison.rank || 0)).map(e => ({ key:e.id, refs:e.refs, event:e })))
const rawRows = computed(() => state.value.items.filter(i => (state.value.pending_refs || []).includes(i.ref)).map(i => ({ key:i.ref, refs:[i.ref], event:(review.value?.events || []).find(e => e.refs.includes(i.ref)) })))
const viewingPending = computed(() => showPending.value || !finalRows.value.length)
const selectedRows = computed(() => viewingPending.value ? rawRows.value : finalRows.value)
const visibleRows = computed(() => selectedRows.value.slice((page.value-1)*pageSize, page.value*pageSize))
const detailRow = computed(() => [...finalRows.value, ...rawRows.value].find(r => r.key === detailKey.value))
const screenDisabled = computed(() => loading.value || busy.value || !state.value.pool?.eligible || state.value.line?.enabled === false)
const canResume = computed(() => currentPolicy.value && ['partial','stopped','failed'].includes(state.value.status))
const screenLabel = computed(() => canResume.value ? '继续筛选' : '筛选')
const message = computed(() => requestError.value || fetchError.value || state.value.error || '')
const progressText = computed(() => state.value.status === 'partial' ? '已保留完成部分，可继续筛选' : ({ grouping:'正在合并同一事件', initial:'正在筛选', planning:'正在选稿', reviewing:'正在读取和核实报道' })[review.value?.phase] || '正在筛选')
function openDetail(row, ref) { detailKey.value=row.key; detailRef.value=ref || row.refs[0]; detailOpen.value=true }
function activateRow(row) { openDetail(row) }
function detailReason(row) { return row.event?.assessment?.reason || row.event?.plan?.reason || '尚未筛选。' }
function briefReason(row) { const text=viewingPending.value ? row.event?.assessment?.reason || row.event?.plan?.reason : row.event?.comparison?.reason; return text ? text.split('。')[0]+'。' : '待筛选' }
function titleEntries(row) { return [...new Map(row.refs.map(r => itemByRef.value.get(r)).filter(Boolean).map(i => [i.title,i])).values()] }
function saveSelection() {
  if (loading.value || !state.value.batch || state.value.line?.id !== lineId.value) return
  creation.batchId=state.value.batch.id
  creation.lineId=lineId.value
  creation.format=state.value.line?.format || 'single'
  const existingOrder=creation.events.map(e => e.id)
  const rows=finalRows.value.filter(r => selectedEvents.value.includes(r.event.id))
  rows.sort((a,b) => (existingOrder.includes(a.key) ? existingOrder.indexOf(a.key) : 9999) - (existingOrder.includes(b.key) ? existingOrder.indexOf(b.key) : 9999))
  creation.events=rows.map(r => ({ id:r.event.id, version:r.event.version, title:itemByRef.value.get(r.refs[0])?.title }))
  rememberSelection()
}
function startCreation() {
  saveSelection()
  router.push('/drafts/new')
}
async function request(path, options) {
  return api(path, options?.body ? JSON.parse(options.body) : undefined, options?.method)
}
async function refresh() {
  const requestedLine=lineId.value
  try {
    const next=await request('/api/state?line_id='+encodeURIComponent(requestedLine))
    if (requestedLine !== lineId.value) return
    state.value={ ...next, items:next.items || [], results:next.results || [] }
    if (!creation.selections[requestedLine] && !creation.targets.length) creation.targets=(next.line?.targets || []).map(t=>t.account_id)
    fetchError.value=''
    selectedEvents.value=selectedEvents.value.filter(id => finalRows.value.some(r => r.event.id===id
      && r.event.version === creation.events.find(e => e.id === id)?.version))
  } catch(e) { fetchError.value=e.message }
  finally { if (requestedLine === lineId.value) loading.value=false }
}
async function run(path, body) {
  sending.value=true; requestError.value=''
  try { await request(path, { method:'POST', headers:{ 'Content-Type':'application/json' }, body:JSON.stringify(body) }); await refresh() }
  catch(e) { requestError.value=e.message }
  finally { sending.value=false }
}
async function poll() { await refresh(); if (!disposed) timer=window.setTimeout(poll,3000) }
watch(lineId, () => {
  loading.value=true
  switchSelection(lineId.value)
  selectedEvents.value=creation.events.map(e => e.id)
  showPending.value=false; requestError.value=''; detailOpen.value=false; refresh()
})
watch(() => state.value.batch?.id, () => { page.value=1; detailOpen.value=false })
watch(selectedEvents, saveSelection, { deep:true })
watch(viewingPending, () => { page.value=1 })
watch(()=>route.query.column,id=>{if(id&&id!==lineId.value)lineId.value=id})
onMounted(async()=>{try{const c=await api('/api/columns');if(!c.lines.some(l=>l.id===lineId.value)) lineId.value=c.lines.find(l=>l.enabled!==false)?.id||c.lines[0]?.id||'';if(lineId.value!==creation.lineId) switchSelection(lineId.value)}catch(e){fetchError.value=e.message}await poll()})
onBeforeUnmount(() => { saveSelection(); disposed=true; window.clearTimeout(timer) })
</script>

<template>
  <section class="topics-page">
    <header class="page-heading">
      <div class="actions"><h1>选题</h1><el-select v-model="lineId" aria-label="栏目" style="width: 150px"><el-option v-for="line in state.lines || []" :key="line.id" :label="line.name" :value="line.id" /></el-select></div>
      <div class="actions">
        <el-button :disabled="loading || sending || collectionBusy || !lineId || state.line?.enabled===false" @click="run('/api/collect', {line_id:lineId})">{{ collectionBusy ? '采集中' : '采集' }}</el-button>
        <el-button :disabled="screenDisabled" @click="run('/api/screen', { line_id: lineId, batch_id: canResume ? state.batch?.id : undefined })">{{ screenLabel }}</el-button>
        <el-button v-if="canResume && state.pending_changes" link :disabled="screenDisabled" @click="run('/api/screen', { line_id: lineId })">筛选新内容</el-button>
        <el-button type="primary" :disabled="!selectedEvents.length || !currentPolicy || state.line?.enabled===false" @click="startCreation()">做稿{{ selectedEvents.length ? `（${selectedEvents.length}）` : '' }}</el-button>
      </div>
    </header>
    <el-alert v-if="state.line?.enabled===false" title="栏目已停用" type="info" :closable="false" />
    <div v-if="busy || canResume || collectionBusy" class="progress-line">
      <span v-if="collectionBusy">正在采集新内容</span>
      <span v-if="reviewBusy || canResume">{{ progressText }}</span>
      <el-button v-if="reviewBusy || state.status === 'screening'" link :disabled="sending" @click="run('/api/stop', { batch_id: state.batch.id })">停止</el-button>
    </div>
    <el-alert v-if="message" class="error" :title="message" type="error" :closable="false" />
    <p v-for="source in (state.collection?.sources || []).filter(s => s.status === 'failed' && state.line?.sources?.includes(s.source_id))" :key="source.source_id" role="alert" class="action-error">{{ source.name }}采集失败：{{ source.error }}</p>
    <div v-if="!loading" class="shortlist-bar">
      <div class="filter-tabs"><button :class="{active:!viewingPending}" :disabled="!finalRows.length" @click="showPending=false">可做稿 <span>{{finalRows.length}}</span></button><button :class="{active:viewingPending}" @click="showPending=true">待处理 <span>{{rawRows.length}}</span></button></div><span v-if="selectedEvents.length" class="muted">已选 {{selectedEvents.length}}</span>
    </div>
    <section v-if="!loading && state.items.length" class="content">
      <el-table :data="visibleRows" class="shortlist-table" row-key="key" :empty-text="busy ? '正在筛选…' : '暂无选题'" @row-click="activateRow">
        <el-table-column width="48"><template #default="{ row }"><input v-if="!viewingPending && row.event?.comparison?.selected" type="checkbox" class="candidate-select" v-model="selectedEvents" :value="row.event.id" :aria-label="`选择 ${itemByRef.get(row.refs[0])?.title}`" @click.stop /></template></el-table-column>
        <el-table-column label="标题" min-width="300"><template #default="{ row }"><div v-for="item in titleEntries(row)" :key="item.ref"><button class="candidate-title" @click.stop="openDetail(row, item.ref)">{{ item.title }}</button></div></template></el-table-column>
        <el-table-column label="来源" width="150"><template #default="{ row }">{{ [...new Set(row.refs.map(r => itemByRef.get(r)?.source_name))].filter(Boolean).join('、') }}</template></el-table-column>
        <el-table-column :label="viewingPending ? '当前情况' : '入选理由'" min-width="240"><template #default="{ row }">{{ briefReason(row) }}</template></el-table-column>
      </el-table>
      <div v-if="selectedRows.length > pageSize" class="pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" :total="selectedRows.length" layout="prev, pager, next" /></div>
    </section>
    <div v-else-if="!loading && !busy && !message" class="empty-state"><h2>{{lineId?'暂无选题':'还没有栏目'}}</h2><el-button v-if="lineId" :disabled="state.line?.enabled===false" @click="run('/api/collect',{line_id:lineId})">采集</el-button><RouterLink v-else to="/columns">新建栏目</RouterLink></div>
    <el-drawer v-model="detailOpen" class="candidate-drawer" title="来源与依据" size="560px">
      <template v-if="detailRow">
        <h2 class="detail-title">{{ itemByRef.get(detailRef)?.title }}</h2>
        <p>{{ detailReason(detailRow) }}</p>
        <div v-for="material in (detailRow.event?.materials || []).filter(m => m.status === 'read')" :key="material.material_id" class="source-entry"><a :href="material.final_url || material.url" target="_blank" rel="noopener noreferrer">{{ material.title || material.publisher || '已读报道' }} ↗</a></div>
        <div v-for="source in hotlistLinks(detailRow.refs.map(r => itemByRef.get(r)).filter(Boolean))" :key="source.url" class="source-entry"><a :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.label }} ↗</a></div>
      </template>
    </el-drawer>
  </section>
</template>
