<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from './api'
const emit = defineEmits(['changed'])
const data = ref(null), open = ref(false), form = ref({}), pending = ref(false), error = ref('')
const resultOpen = ref(false)
const history = ref({items:[],total:0}), historyPage = ref(1), historyLoading = ref(false), historyError = ref('')
let historySequence = 0
let timer, disposed = false
const collecting = computed(() => data.value?.run?.status === 'running')
const enabledSources = computed(() => data.value?.sources.filter(s => s.enabled) || [])
const selectedSources = computed(() => enabledSources.value.filter(s => form.value.scope === 'all' || (form.value.scope === 'custom' ? form.value.source_ids?.includes(s.id) : data.value?.lines.some(l => form.value.line_ids?.includes(l.id) && l.sources.includes(s.id)))))
const scopeHint = computed(() => {
 if (selectedSources.value.length) return `已选 ${selectedSources.value.length} 个渠道`
 if (form.value.scope === 'columns' && !form.value.line_ids?.length) return '请选择至少一个栏目'
 return '请选择至少一个有效渠道'
})
const limited = computed({ get: () => form.value.per_source_limit !== null, set: v => form.value.per_source_limit = v ? 20 : null })
const timeError = computed(() => {
 const times = form.value.daily_times || []
 if (!times.length || times.some(t => !t)) return '请选择采集时刻'
 if (new Set(times).size !== times.length) return '采集时刻不能重复'
 return ''
})
const summary = computed(() => {
 const s = data.value?.settings
 return s ? `${{all:'全渠道',columns:'按栏目',custom:'自定义渠道'}[s.scope]} · ${s.schedule_mode === 'daily' ? `每天 ${s.daily_times.join('、')}` : s.interval_minutes ? `每 ${s.interval_minutes} 分钟` : '手动'}` : ''
})
const totals = computed(() => {
 const t = {new:0,changed:0,unchanged:0,failed:0}
 for (const s of data.value?.run?.sources || []) { for (const k of ['new','changed','unchanged']) t[k] += s.changes?.[k] || 0; if (s.status === 'failed') t.failed++ }
 return t
})
function stamp(value) { return value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—' }
function statusLabel(value) { return {running:'采集中',completed:'已完成',partial:'部分失败',failed:'失败',interrupted:'已中断'}[value] || value }
async function loadHistory() {
 const token = ++historySequence; historyLoading.value = true
 try { const result = await api(`/api/collection/history?page=${historyPage.value}`); if (token === historySequence && !disposed) { history.value = result; historyError.value = '' } }
 catch(e) { if (token === historySequence) historyError.value = e.message }
 finally { if (token === historySequence) historyLoading.value = false }
}
watch([resultOpen, historyPage], () => { if (resultOpen.value) loadHistory() })
async function refresh() {
 try {
  const result = await api('/api/collection')
  const settings = result.settings
  if (!Array.isArray(settings.daily_times) || !settings.daily_times.length) {
   settings.daily_times = [settings.daily_time || '09:00']
  }
  data.value = result
  error.value = ''
  if (resultOpen.value) await loadHistory()
 } catch (e) { error.value = e.message }
}
async function poll() { await refresh(); if (!disposed) timer = setTimeout(poll, 5000) }
async function show() {
 error.value = ''; await refresh(); if (!data.value) return
 form.value = JSON.parse(JSON.stringify(data.value.settings))
 open.value = true
}
watch(() => form.value.schedule_mode, mode => { if (mode === 'interval' && !form.value.interval_minutes) form.value.interval_minutes = 60 })
async function save() {
 if (pending.value) return
 if (!selectedSources.value.length) return ElMessage.warning(scopeHint.value)
 if (form.value.schedule_mode === 'daily' && timeError.value) return ElMessage.warning(timeError.value)
 pending.value = true; error.value = ''
 try {
  const {revision,daily_time,...settings} = form.value
  if (settings.schedule_mode !== 'interval') settings.interval_minutes = null
  settings.daily_times = timeError.value ? data.value.settings.daily_times : [...settings.daily_times].sort()
  data.value = await api('/api/collection/settings', {expected_revision:revision,settings})
  form.value = JSON.parse(JSON.stringify(data.value.settings))
  await refresh(); emit('changed'); open.value = false
  ElMessage.success('采集设置已保存')
 } catch (e) { error.value = e.message } finally { pending.value = false }
}
async function collect() {
 if (!data.value || pending.value || collecting.value) return
 pending.value = true; error.value = ''
 try {
  await api('/api/collect', {expected_revision:data.value.settings.revision})
  await refresh(); emit('changed'); resultOpen.value = true
 } catch (e) { error.value = e.message; resultOpen.value = true } finally { pending.value = false }
}
async function retry() {
 pending.value = true; error.value = ''
 try { await api('/api/collect', {expected_revision:data.value.settings.revision,retry_run_id:data.value.run.id}); await refresh(); emit('changed') }
 catch (e) { error.value = e.message } finally { pending.value = false }
}
onMounted(poll); onBeforeUnmount(() => {disposed = true; clearTimeout(timer)})
</script>
<template>
 <header class="page-heading collection-heading">
  <div class="collection-title">
   <slot />
   <p v-if="summary" class="muted collection-summary">采集计划：{{summary}}</p>
  </div>
  <div class="collection-actions">
   <el-button v-if="data?.run?.id||data?.schedule_error" class="collection-record" link @click="resultOpen=true">{{collecting?'采集中…':data?.schedule_error?'定时采集失败':totals.failed?`${totals.failed} 个渠道失败`:'采集记录'}}</el-button>
   <el-button @click="show">采集设置</el-button>
   <el-button type="primary" :disabled="!data||pending||collecting" @click="collect">立即采集</el-button>
  </div>
 </header>
 <el-dialog v-model="open" class="collection-dialog" title="采集设置" width="560px" :close-on-click-modal="false" :close-on-press-escape="!pending" :show-close="!pending">
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <el-form label-position="top" :disabled="pending" @submit.prevent="save">
   <el-form-item label="采集范围">
    <el-radio-group v-model="form.scope" class="collection-segments"><el-radio-button value="all">全渠道</el-radio-button><el-radio-button value="columns">按栏目</el-radio-button><el-radio-button value="custom">自定义渠道</el-radio-button></el-radio-group>
    <el-checkbox-group v-if="form.scope==='columns'" v-model="form.line_ids" class="collection-options"><el-checkbox v-for="l in data?.lines" :key="l.id" :value="l.id">{{l.name}}</el-checkbox></el-checkbox-group>
    <el-checkbox-group v-if="form.scope==='custom'" v-model="form.source_ids" class="collection-options"><el-checkbox v-for="s in enabledSources" :key="s.id" :value="s.id">{{s.name}}</el-checkbox></el-checkbox-group>
    <span class="field-hint">{{scopeHint}}</span>
   </el-form-item>
   <el-form-item label="每渠道读取条数">
    <div class="field-inline"><el-radio-group v-model="limited"><el-radio-button :value="false">不限</el-radio-button><el-radio-button :value="true">指定条数</el-radio-button></el-radio-group><el-input-number v-if="limited" v-model="form.per_source_limit" :min="1" :max="10000" :precision="0" controls-position="right" aria-label="每渠道读取条数"/><span v-if="limited">条</span></div>
    <span class="field-hint">读取各渠道最新列表，重复内容自动合并。</span>
   </el-form-item>
   <el-form-item label="采集计划">
    <el-radio-group v-model="form.schedule_mode" class="collection-segments"><el-radio-button value="manual">手动</el-radio-button><el-radio-button value="interval">按间隔</el-radio-button><el-radio-button value="daily">每天定时</el-radio-button></el-radio-group>
    <div v-if="form.schedule_mode==='interval'" class="field-inline schedule-value"><span>每</span><el-input-number v-model="form.interval_minutes" :min="1" :max="10080" :precision="0" controls-position="right" aria-label="采集间隔"/><span>分钟采集一次</span></div>
    <div v-if="form.schedule_mode==='daily'" class="field-inline schedule-value">
     <span>北京时间</span>
     <div v-for="(_, i) in form.daily_times" :key="i" class="clock-entry">
      <el-time-picker v-model="form.daily_times[i]" format="HH:mm" value-format="HH:mm" placeholder="选择时刻" :clearable="false" :aria-label="`采集时刻 ${i+1}`"/>
      <el-button v-if="form.daily_times.length>1" text :aria-label="`移除采集时刻 ${i+1}`" @click="form.daily_times.splice(i,1)">×</el-button>
     </div>
     <el-button text type="primary" @click="form.daily_times.push('')">＋ 添加时刻</el-button>
    </div>
    <span v-if="form.schedule_mode==='daily' && timeError" class="field-hint">{{timeError}}</span>
    <span v-if="form.schedule_mode!=='manual'" class="field-hint">服务运行时自动采集，关闭网页不影响。</span>
   </el-form-item>
  </el-form>
  <template #footer><div class="dialog-actions"><div class="actions"><el-button :disabled="pending" @click="open=false">取消</el-button><el-button type="primary" :loading="pending" @click="save">保存设置</el-button></div></div></template>
 </el-dialog>
 <el-drawer v-model="resultOpen" title="采集记录" size="min(480px, 100vw)">
  <el-alert v-if="error||data?.schedule_error" :title="error||data.schedule_error" type="error" :closable="false"/>
  <div v-if="historyError" class="error-retry"><el-alert :title="historyError" type="error" :closable="false"/><el-button @click="loadHistory">重试</el-button></div>
  <div v-if="!history.items.length" class="empty-state">{{historyLoading?'正在加载记录…':'暂无采集记录'}}</div>
  <article v-for="run in history.items" :key="run.id" class="collection-history-row">
   <div class="history-heading">
    <RouterLink v-if="run.tracked" :to="{path:'/pool',query:{batch_id:run.id}}" :title="run.finished_at?`结束 ${stamp(run.finished_at)}`:undefined" @click="resultOpen=false">{{stamp(run.started_at)}}</RouterLink>
    <span v-else>{{stamp(run.started_at)}}</span>
    <span class="muted">{{run.count}} 条</span>
    <span class="status-pill" :class="run.status==='running'?'progress':run.status==='completed'?'success':'warning'">{{statusLabel(run.status)}}</span>
   </div>
   <p v-if="run.error" class="action-error">{{run.error}}</p>
   <p v-for="failure in run.failures" :key="failure.name" class="action-error">{{failure.name}}：{{failure.error}}</p>
   <el-button v-if="run.id===data?.run?.id&&run.failures.length" :disabled="collecting||pending" @click="retry">重试 {{run.failures.length}} 个失败渠道</el-button>
  </article>
  <el-pagination v-if="history.total>20" v-model:current-page="historyPage" :page-size="20" :total="history.total" layout="prev,pager,next"/>
 </el-drawer>
</template>
<style scoped>
.history-heading{display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:14px}.history-heading>:first-child{margin-right:auto}.collection-history-row{padding:16px 0;border-bottom:1px solid #e2e8e5}.collection-history-row p{font-size:13px;line-height:1.7;margin:10px 0}
.collection-heading{flex-wrap:wrap;gap:14px 24px;margin-bottom:20px}
.collection-title{min-width:0}
.collection-summary{margin:4px 0 0;line-height:1.5;overflow-wrap:anywhere}
.collection-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.collection-actions>.el-button{margin:0}
.collection-actions>.collection-record{margin-right:6px}
.collection-segments{display:flex;width:100%}.collection-segments :deep(.el-radio-button){flex:1}.collection-segments :deep(.el-radio-button__inner){width:100%}
.collection-options{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;width:100%;max-height:180px;overflow:auto;margin-top:12px}.collection-options .el-checkbox{margin:0}.collection-options :deep(.el-checkbox__label){white-space:normal}
.clock-entry{display:flex;align-items:center}.clock-entry :deep(.el-date-editor.el-input){width:104px}.clock-entry>.el-button{padding:5px}
.field-inline{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.field-inline .el-input-number{width:120px}.field-inline .el-date-editor{width:140px}.schedule-value{margin-top:12px}.field-hint{display:block;width:100%;font-size:12px;line-height:1.6;color:#7c8983;margin-top:8px}
@media(max-width:700px){.collection-options{grid-template-columns:1fr}}
</style>
