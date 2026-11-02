<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from './api'
const props = defineProps({ column: Object })
const emit = defineEmits(['close', 'changed'])
const data = ref(null), error = ref(''), loading = ref(false), stopping = ref(false), resuming = ref(false)
const opened = ref([]), filters = ref({}), pages = ref({}), now = ref(Date.now())
const tables = {}, expandedRows = ref({})
let generation = 0, timer, clock
const names = { initial: '初筛', selection: '二筛', verification: '正文核验' }
const labels = { completed: '已完成', running: '进行中', stopped: '已停止', failed: '未完成', waiting: '未开始' }
const decisions = { advance: '保留', drop: '淘汰', insufficient: '待补充', deferred: '暂缓' }
const workLabels = { reading: '读取正文', judging: '核验中', searching: '补充资料', failed: '失败', stopped: '已停止', waiting: '待核验' }
const active = computed(() => ['queued', 'screening'].includes(data.value?.status))
const resumable = computed(() => ['stopped', 'partial', 'failed'].includes(data.value?.status))
const stages = computed(() => data.value?.stages || [])
const current = computed(() => stages.value.find(s => s.status !== 'completed') || stages.value[2])
const activeIndex = computed(() => data.value?.preparing ? 0 : stages.value.findIndex(s => s.status !== 'completed') < 0 ? 3 : stages.value.findIndex(s => s.status !== 'completed'))
const elapsed = computed(() => duration(data.value?.started_at, data.value?.finished_at))
function duration(start, end) {
 if (!start) return ''
 const seconds = Math.max(0, Math.floor(((end ? Date.parse(end) : now.value) - Date.parse(start)) / 1000))
 return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
function stepStatus(stage) { return { completed: 'success', running: 'process', failed: 'error', stopped: 'wait', waiting: 'wait' }[stage.status] }
function progressText(stage) { return stage.total === null ? labels[stage.status] : `${labels[stage.status]} · ${stage.completed}/${stage.total}` }
function resultText(row, stage) {
 if (stage.id === 'selection' && row.checked && !row.decision) return '待汇总'
 if (row.decision) return row.decision === 'advance' && stage.id === 'verification' ? '可做稿' : decisions[row.decision] || row.decision
 if (stage.id === 'verification') return workLabels[row.status] || '待核验'
 return stage.status === 'running' ? '待返回' : stage.status === 'stopped' ? '未完成' : '待筛选'
}
function issue(text) { return /extension protocol|code=unsupported|浏览器扩展协议/.test(text || '') ? '浏览器扩展版本不兼容，更新后可重试。' : text }
function resultKey(row) { return row.decision || (row.checked ? 'checked' : 'pending') }
function options(stage) {
 const values = [...new Set(stage.rows.map(resultKey))]
 return values.map(value => ({ value, label: value === 'checked' ? '待汇总' : value === 'pending' ? '未完成' : value === 'advance' && stage.id === 'verification' ? '可做稿' : decisions[value], count: stage.rows.filter(row => resultKey(row) === value).length }))
}
function filtered(stage) { return stage.rows.filter(row => !filters.value[stage.id] || resultKey(row) === filters.value[stage.id]) }
function displayed(stage) { return filtered(stage).slice(((pages.value[stage.id] || 1) - 1) * 20, (pages.value[stage.id] || 1) * 20) }
function waitingText(stage) {
 if (data.value?.status === 'queued') return '排队等待执行'
 if (data.value?.preparing) return '等待线索整理完成'
 if (stage.total === null) return '等待上一环节完成'
 return stage.total === 0 ? '没有需要进入本环节的选题' : '暂无记录'
}
function workingText(stage) {
 if (stage.status !== 'running') return ''
 if (stage.comparing) return '分段评估已完成，正在汇总比较'
 if (stage.call_started_at) return `等待模型返回 · ${duration(stage.call_started_at)}`
 if (stage.id === 'verification') {
  const reading = stage.rows.filter(r => r.status === 'reading').length
  const judging = stage.rows.filter(r => r.status === 'judging').length
  return [reading && `正在读取 ${reading} 个选题的正文`, judging && `正在核验 ${judging} 个选题`].filter(Boolean).join(' · ') || '正在准备正文核验'
 }
 return '正在准备筛选'
}
async function openStage(id) {
 if (!opened.value.includes(id)) opened.value.push(id)
 await nextTick()
 document.getElementById(`process-stage-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
async function refresh(id, token) {
 try {
  const result = await api(`/api/screen/process?batch_id=${encodeURIComponent(id)}`)
  if (token !== generation) return
  const first = !data.value
  data.value = result; error.value = ''
  if (first) opened.value = [current.value?.id || 'initial']
  if (!active.value) stopping.value = false
 } catch (e) { if (token === generation) error.value = e.message }
 finally {
  if (token === generation) { loading.value = false; if (active.value || error.value) timer = setTimeout(() => refresh(id, token), 2000) }
 }
}
async function stop() {
 const token = generation, id = data.value.batch_id
 stopping.value = true
 try { await api('/api/stop', { batch_id: id }); if (token === generation) emit('changed') }
 catch (e) { if (token === generation) { error.value = e.message; stopping.value = false } }
}
async function resume() {
 if (resuming.value || !resumable.value) return
 const token = generation, id = data.value.batch_id
 resuming.value = true; error.value = ''
 try {
  const plan = await api('/api/screen/preview', {columns: [{line_id: props.column.line.id, action: 'resume'}]})
  if (token !== generation) return
  if (plan.columns[0]?.previous_id !== id) throw new Error('栏目已有新任务，请重新查看筛选过程')
  await api('/api/screen/start', {token: plan.token})
  if (token !== generation) return
  emit('changed'); clearTimeout(timer); await refresh(id, token)
 } catch (e) { if (token === generation) error.value = e.message }
 finally { if (token === generation) resuming.value = false }
}
watch(() => props.column?.batch?.id, id => {
 generation++; clearTimeout(timer); clearInterval(clock)
 data.value = null; error.value = ''; stopping.value = false; resuming.value = false; opened.value = []; filters.value = {}; pages.value = {}; expandedRows.value = {}
 loading.value = !!id
 if (id) { refresh(id, generation); clock = setInterval(() => now.value = Date.now(), 1000) }
}, { immediate: true })
watch(() => props.column?.status, status => {
 if (props.column?.batch?.id && ['queued', 'screening'].includes(status) && data.value && !active.value) {
  clearTimeout(timer); refresh(props.column.batch.id, generation)
 }
})
onBeforeUnmount(() => { generation++; clearTimeout(timer); clearInterval(clock) })
</script>
<template>
 <el-drawer :model-value="!!column" :title="`${column?.line.name || ''} · 筛选过程`" size="760px" class="screening-drawer" destroy-on-close @close="emit('close')">
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <div v-if="loading" class="loading-state">正在加载…</div>
  <div v-else-if="!column?.batch" class="process-empty">尚未筛选</div>
  <template v-if="data">
   <div class="process-overview">
    <el-steps :active="activeIndex" align-center finish-status="success">
     <el-step v-for="stage in stages" :key="stage.id" :status="stepStatus(stage)">
      <template #title><button class="step-link" @click="openStage(stage.id)">{{names[stage.id]}}</button></template>
     </el-step>
    </el-steps>
    <div class="run-summary"><span>{{data.input_count}} 条线索<span v-if="elapsed"> · 用时 {{elapsed}}</span></span><span v-if="stopping" role="status">正在停止…</span><span v-else-if="data.status==='queued'">排队中</span></div>
    <div v-if="data.preparing" class="preparing" role="status">{{data.status==='screening'?'正在整理线索':data.status==='queued'?'等待整理线索':data.status==='stopped'?'线索整理已停止':'线索整理未完成'}}<span v-if="data.preparing_started_at"> · {{duration(data.preparing_started_at)}}</span></div>
    <el-alert v-if="data.error&&!['stopped','queued','screening'].includes(data.status)" :title="issue(data.error)" type="error" :closable="false" />
   </div>
   <el-collapse v-model="opened" class="process-stages">
    <el-collapse-item v-for="stage in stages" :id="`process-stage-${stage.id}`" :key="stage.id" :name="stage.id">
     <template #title><div class="stage-heading"><strong>{{names[stage.id]}}</strong><span>{{progressText(stage)}}</span></div></template>
     <div v-if="stage.total!==null" class="stage-progress"><el-progress :percentage="stage.total ? Math.round(stage.completed/stage.total*100) : 100" :show-text="false" :stroke-width="4" :status="stage.status==='failed'?'exception':stage.status==='completed'?'success':undefined"/><span v-if="workingText(stage)" role="status">{{workingText(stage)}}</span></div>
     <div v-if="stage.rows.length" class="stage-filter"><el-select v-model="filters[stage.id]" :aria-label="`${names[stage.id]}结果`" placeholder="全部结果" clearable @change="pages[stage.id]=1"><el-option v-for="option in options(stage)" :key="option.value" :value="option.value" :label="`${option.label} ${option.count}`"/></el-select></div>
     <el-table v-if="stage.rows.length" :ref="table=>tables[stage.id]=table" :data="displayed(stage)" row-key="id" :expand-row-keys="expandedRows[stage.id]||[]" class="process-table" empty-text="暂无匹配结果" @expand-change="(_,rows)=>expandedRows[stage.id]=rows.map(r=>r.id)">
      <el-table-column type="expand" width="32"><template #default="{row}">
       <div class="process-detail">
        <p v-if="row.reason">{{row.reason}}</p>
        <p v-else class="muted">{{row.decision?'未记录理由':'尚未返回筛选结果'}}</p>
        <p v-if="row.error" class="action-error">{{issue(row.error)}}</p>
        <div v-if="row.reading?.length" class="source-reading"><div v-for="source in row.reading" :key="source.id" class="reading-entry"><div><a v-if="source.url" :href="source.url" target="_blank" rel="noopener noreferrer">{{source.source || source.title}} ↗</a><span v-else>{{source.source || source.title}}</span><span :class="source.status==='failed'?'action-error':'muted'">{{{read:'已读取',failed:'读取失败',reading:'读取中',waiting:'未读取'}[source.status]}}</span></div><p v-if="source.error" class="action-error">{{issue(source.error)}}</p></div></div>
        <ul v-if="row.unknowns?.length" class="verification-questions"><li v-for="question in row.unknowns" :key="question">待核实：{{question}}</li></ul>
        <blockquote v-for="(quote,index) in row.evidence" :key="index"><p>{{quote.quote}}</p><a v-if="quote.url" :href="quote.url" target="_blank" rel="noopener noreferrer">{{quote.source || quote.title || '来源'}} ↗</a></blockquote>
        <details v-if="stage.id==='initial'&&row.inputs?.length" class="initial-inputs"><summary>参与初筛的线索</summary><div v-for="(input,index) in row.inputs" :key="index"><a v-if="input.url||input.entry_url" :href="input.url||input.entry_url" target="_blank" rel="noopener noreferrer">{{input.title}} ↗</a><span v-else>{{input.title}}</span><p v-if="input.summary">{{input.summary}}</p></div></details>
       </div>
      </template></el-table-column>
      <el-table-column label="选题" min-width="210"><template #default="{row}"><button class="process-title" :aria-expanded="expandedRows[stage.id]?.includes(row.id)||false" @click="tables[stage.id]?.toggleRowExpansion(row)">{{row.title}}</button><span v-if="stage.id==='initial'" class="process-source">{{row.sources.join('、')}}</span></template></el-table-column>
      <el-table-column v-if="stage.id==='selection'" label="角度" min-width="160"><template #default="{row}">{{row.angle || '—'}}</template></el-table-column>
      <el-table-column v-if="stage.id==='verification'" label="正文" width="112"><template #default="{row}"><span v-if="row.reading.some(s=>s.status==='reading')">读取中</span><span v-else>{{row.reading.filter(s=>s.status==='read').length}}/{{row.reading.length}} 已读取</span><span v-if="row.reading.some(s=>s.status==='failed')" class="reading-failure">{{row.reading.filter(s=>s.status==='failed').length}} 个失败</span></template></el-table-column>
      <el-table-column label="结果" width="88"><template #default="{row}"><span class="result-text" :class="row.decision==='advance'?'is-kept':row.decision==='insufficient'||row.status==='failed'?'is-unfinished':''">{{resultText(row,stage)}}</span></template></el-table-column>
     </el-table>
     <div v-else class="process-empty">{{waitingText(stage)}}</div>
     <el-pagination v-if="filtered(stage).length>20" v-model:current-page="pages[stage.id]" :page-size="20" :total="filtered(stage).length" layout="prev,pager,next"/>
    </el-collapse-item>
   </el-collapse>
  </template>
  <template v-if="active||resumable" #footer><div class="process-footer"><span v-if="active">关闭网页仍会继续；退出本地服务会中断。</span><span v-else>沿用原名单，继续未完成部分。</span><el-button v-if="active" :disabled="stopping" @click="stop">{{stopping?'正在停止…':data.status==='queued'?'取消排队':'停止筛选'}}</el-button><el-button v-else type="primary" :loading="resuming" @click="resume">继续筛选</el-button></div></template>
 </el-drawer>
</template>
<style scoped>
.process-overview{padding:24px 0 16px}.step-link{border:0;background:none;color:inherit;font-size:14px;font-weight:600;line-height:24px;padding:0 4px}.step-link:hover{text-decoration:underline}.process-overview :deep(.el-step__title){line-height:28px}.process-overview :deep(.el-step__icon){width:28px;height:28px}.process-overview :deep(.el-step__line){top:13px}.run-summary{display:flex;justify-content:space-between;gap:16px;margin-top:24px;color:#7c8983;font-size:12px}.preparing{margin-top:12px;font-size:13px}.process-overview .el-alert{margin-top:16px}.stage-heading{display:flex;align-items:center;gap:16px;padding-right:12px;line-height:20px;flex:1}.stage-heading strong{font-size:14px;font-weight:600}.stage-heading span{margin-left:auto;color:#7c8983;font-size:12px;font-weight:400}.process-stages :deep(.el-collapse-item__header){height:58px}.process-stages :deep(.el-collapse-item__content){padding-bottom:20px}.stage-progress{margin:0 0 16px}.stage-progress>span{display:block;margin-top:8px;color:#61746b;font-size:12px}.stage-filter{display:flex;align-items:center;justify-content:space-between;margin:0 0 12px;gap:16px}.stage-filter .el-select{width:150px}.process-table{font-size:13px}.process-table :deep(.cell){padding:12px 8px;line-height:1.7}.process-title{display:block;border:0;padding:0;background:none;font:inherit;text-align:left;color:#314c40}.process-title:hover{color:#207469;text-decoration:underline}.process-source{display:block;font-size:12px;color:#839088;margin-top:4px}.result-text{white-space:nowrap;color:#7c8983}.result-text.is-kept{color:#207469}.result-text.is-unfinished,.reading-failure{color:#a16e27}.reading-failure{display:block;font-size:12px}.process-detail{padding:16px 16px 16px 40px;background:#f8faf9;font-size:13px;line-height:1.8}.process-detail p{margin:0 0 12px}.process-detail blockquote{border-left:2px solid #c7d9d2;padding:0 0 0 12px;margin:16px 0}.process-detail blockquote p{margin:0 0 4px}.process-detail blockquote a{font-size:12px}.source-reading{margin:16px 0}.reading-entry+.reading-entry{margin-top:12px}.reading-entry>div{display:flex;justify-content:space-between;gap:16px}.reading-entry p{margin:4px 0 0}.verification-questions{margin:12px 0;padding-left:18px;color:#8c6f3f}.initial-inputs{margin-top:16px}.initial-inputs summary{cursor:pointer;color:#207469}.initial-inputs>div{margin-top:12px}.initial-inputs p{margin-top:4px}.process-empty{padding:28px 12px;text-align:center;color:#8b9790;font-size:13px}.process-stages .el-pagination{justify-content:flex-end;margin-top:12px}.process-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;min-height:30px}.process-footer>span{color:#7c8983;font-size:12px;line-height:1.7}.process-footer .el-button{flex-shrink:0}@media(max-width:580px){.process-detail{padding:12px}.process-footer{flex-wrap:wrap}.stage-heading{gap:8px}}
</style>
