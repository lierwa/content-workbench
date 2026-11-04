<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from './api'
import DraftPromptSettings from './DraftPromptSettings.vue'
import { weekdayNames, scheduleLabel } from './screenSchedule'
const props = defineProps({ modelValue: Boolean, initialIds: Array, editSchedule: Boolean, runOnce: Boolean, lines: {type:Array,default:()=>[]} })
const emit = defineEmits(['update:modelValue', 'started'])
const options = ref({ columns: [], batches: [] }), settings = ref({})
const manualIds = ref([]), scheduleIds = ref([])
const mode = ref('time'), batches = ref([]), preview = ref(null)
const loading = ref(false), starting = ref(false), error = ref('')
const execution = ref('now'), minutes = ref(60), weekdays = ref([]), times = ref([])
const editingTimeOnly = ref(false), draftLine = ref(null)
async function draftSaved(){
 emit('started')
 try { options.value=await api('/api/screen/options') }
 catch(e) { error.value=e.message }
}
const immediate = computed(() => execution.value === 'now')
const ids = computed({
 get: () => immediate.value ? manualIds.value : scheduleIds.value,
 set: value => { if (immediate.value) manualIds.value = value; else scheduleIds.value = value }
})
const hasPlan = computed(() => selected.value.some(c => c.schedule?.mode && c.schedule.mode !== 'off'))
const validPlan = computed(() => {
 if (!ids.value.length) return false
 if (execution.value === 'interval') return Number.isInteger(minutes.value) && minutes.value > 0
 return times.value.length > 0 && times.value.every(Boolean) && new Set(times.value).size === times.value.length
   && (execution.value !== 'weekly' || weekdays.value.length > 0)
})
const opening = ref(false)
let sequence = 0, openSequence = 0, timer, disposed = false
function setExecution(value) {
 execution.value = value
 const first = selected.value[0]?.schedule
 const same = selected.value.every(c => scheduleLabel(c.schedule) === scheduleLabel(first))
 const existing = same && first?.mode === value ? first : {}
 minutes.value = existing.minutes ?? 60
 weekdays.value = [...(existing.weekdays ?? [1,2,3,4,5])]
 times.value = [...(existing.times ?? ['09:00'])]
}
function showSavedExecution() {
 const plans = options.value.columns.filter(c => scheduleIds.value.includes(c.line_id)).map(c => c.schedule)
 const first = plans[0]
 if (plans.some(p => scheduleLabel(p) !== scheduleLabel(first))) execution.value = 'mixed'
 else setExecution(first && first.mode !== 'off' ? first.mode : 'daily')
}
function openAutomation(initialIds, timeOnly = false) {
 scheduleIds.value = [...initialIds]
 editingTimeOnly.value = timeOnly
 showSavedExecution()
}
function rangeLabel(column) {
 const scope = column.schedule_scope
 if (!scope) return `最近 ${column.hours} 小时`
 if (scope.mode === 'time') return `${stamp(scope.start)} 至 ${stamp(scope.end)}`
 if (scope.mode === 'batches') {
   const names = scope.batch_ids.map(id => {
     const batch = options.value.batches.find(b => b.id === id)
     return batch ? stamp(batch.started_at) : id
   })
   return `采集批次 ${names.join('、')}`
 }
 return `最近 ${scope.count} ${scope.unit === 'hours' ? '小时' : '个采集批次'}`
}
function addTime() {
 if (times.value.every(Boolean)) times.value.push('')
}
async function saveSchedule(stop = false) {
 if (opening.value || starting.value || !ids.value.length || (!stop && !validPlan.value)) return
 const plan = { mode: stop ? 'off' : execution.value }
 if (plan.mode === 'interval') plan.minutes = minutes.value
 if (['daily','weekly'].includes(plan.mode)) plan.times = [...times.value].sort()
 if (plan.mode === 'weekly') plan.weekdays = [...weekdays.value].sort((a,b)=>a-b)
 starting.value = true; error.value = ''
 try {
   const body = { expected_revision: options.value.revision, line_ids: [...ids.value], schedule: plan }
   if (!stop && !editingTimeOnly.value) {
     body.scopes = Object.fromEntries(selected.value.map(c => [c.line_id, formScope(c, true)]))
   }
   await api('/api/screen/schedule', body)
   emit('started')
   try {
     const result = await api('/api/screen/options')
     for (const c of result.columns) {
       settings.value[c.line_id] ??= initialSetting(c)
     }
     options.value = result
     manualIds.value = manualIds.value.filter(id => result.columns.some(c => c.line_id === id && c.enabled && !c.running))
     execution.value = 'now'
     ElMessage.success(stop ? '已关闭筛选定时' : '筛选定时已保存')
   } catch {
     emit('update:modelValue', false)
     ElMessage.warning('定时设置已保存，请重新打开查看')
   }
 } catch (e) { error.value = e.message }
 finally { starting.value = false }
}
async function saveScopes() {
 starting.value = true; error.value = ''
 try {
  await api('/api/screen/schedule', {expected_revision: options.value.revision, line_ids: [...ids.value], scopes: Object.fromEntries(selected.value.map(c => [c.line_id, formScope(c, true)]))})
  emit('started'); emit('update:modelValue', false); ElMessage.success('选题设置已保存')
 } catch(e) { error.value=e.message } finally { starting.value=false }
}
const selected = computed(() => options.value.columns.filter(c => ids.value.includes(c.line_id)))
const available = computed(() => options.value.columns.filter(c => c.enabled && (!props.runOnce || !c.running)).map(c => c.line_id))
const availableBatches = computed(() => options.value.batches.filter(b => b.selectable))
const allSelected = computed(() => available.value.length > 0 && available.value.every(id => ids.value.includes(id)))
const total = computed(() => preview.value?.columns.reduce((sum, c) => sum + c.items.length, 0) || 0)
const endsPrevious = computed(() => selected.value.some(c => c.unfinished && preview.value?.columns.some(p => p.line_id === c.line_id && p.items.length)))
function toggleColumn(id, checked) {
 ids.value = checked ? [...ids.value, id] : ids.value.filter(v => v !== id)
}
function close() { if (!starting.value) emit('update:modelValue', false) }
function datesFor(hours) { const end = new Date(); return [new Date(end.getTime() - hours * 3600000), end] }
function initialSetting(column) {
 const scope = column.schedule_scope
 if (scope?.mode === 'time') {
   return { dates: [new Date(scope.start), new Date(scope.end)] }
 }
 const hours = scope?.unit === 'hours' ? scope.count : column.hours
 const dates = datesFor(hours)
 return { dates, rollingDates: dates, rollingHours: hours }
}
function shortcuts(column) {
 return [...new Set([24, 48, 72, 168, column.hours])].sort((a,b) => a-b).map(hours => ({
   text: `最近 ${hours} 小时`, value: () => {
     const dates = datesFor(hours)
     Object.assign(settings.value[column.line_id], { rollingDates: dates, rollingHours: hours })
     return dates
   }
 }))
}
function stamp(value) { return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) }
function batchLabel(batch) { return `${stamp(batch.started_at)} · ${batch.count} 条` }
function formScope(column, recurring = false) {
 const s = settings.value[column.line_id]
 if (mode.value === 'batches') return { mode: 'batches', batch_ids: [...batches.value] }
 if (recurring && s.rollingDates && s.dates.every((date, i) => +date === +s.rollingDates[i])) {
   return { unit: 'hours', count: s.rollingHours }
 }
 return { mode: 'time', hours: 'custom', start: s.dates[0].toISOString(), end: s.dates[1].toISOString() }
}
const hasRange = computed(() => {
 if (!selected.value.length) return false
 if (mode.value === 'batches') return batches.value.length > 0
 return selected.value.every(c => {
   const dates = settings.value[c.line_id]?.dates
   return dates?.length === 2 && +dates[0] < +dates[1] && +dates[1] <= Date.now() + 5000
 })
})
function requestBody() {
 return { use_saved_scope: props.runOnce, columns: selected.value.map(c => ({
   line_id: c.line_id, action: c.unfinished ? 'replace' : 'new', ...formScope(c)
 })) }
}
async function calculate(token) {
 if (props.runOnce ? !selected.value.length : !hasRange.value) {
  loading.value = false; return
 }
 try {
  const result = await api('/api/screen/preview', requestBody())
  if (token !== sequence || disposed || !props.modelValue) return
  preview.value = result; error.value = ''
 } catch (e) { if (token === sequence) error.value = e.message }
 finally { if (token === sequence) loading.value = false }
}
function invalidate() {
 sequence++; clearTimeout(timer); preview.value = null; error.value = ''
 if (!props.modelValue || !props.runOnce || !immediate.value || opening.value) { loading.value = false; return }
 loading.value = true; const token = sequence; timer = setTimeout(() => calculate(token), 300)
}
async function start() {
 if (!preview.value || loading.value || starting.value || !total.value) return
 starting.value = true; error.value = ''
 try {
  const result = await api('/api/screen/start', { token: preview.value.token })
  emit('started', result); emit('update:modelValue', false)
  ElMessage.success('已加入筛选队列')
 } catch (e) { error.value = e.message; preview.value = null }
 finally { starting.value = false }
}
watch([ids, settings, mode, batches, execution], invalidate, { deep: true })
watch(() => props.modelValue, async open => {
 const request = ++openSequence
 sequence++; clearTimeout(timer); preview.value = null; error.value = ''
 if (!open) return
 opening.value = true; execution.value = 'now'; options.value = { columns: [], batches: [] }; ids.value = []
 try {
  const result = await api('/api/screen/options')
  if (request !== openSequence || disposed || !props.modelValue) return
  options.value = result
  settings.value = Object.fromEntries(result.columns.map(c => [c.line_id, initialSetting(c)]))
  manualIds.value = (props.initialIds || []).filter(id => available.value.includes(id))
  mode.value = 'time'; batches.value = []
  const saved = selected.value.map(c => c.schedule_scope)
  const first = saved[0]
  if (first?.mode === 'batches' && saved.every(scope => scope?.mode === 'batches'
      && JSON.stringify(scope.batch_ids) === JSON.stringify(first.batch_ids))) {
    mode.value = 'batches'
    batches.value = [...first.batch_ids]
  }
  if (props.editSchedule) openAutomation(props.initialIds, true)
 } catch (e) { if (request === openSequence) error.value = e.message }
 finally {
  if (request === openSequence) {
   opening.value = false
   if (!error.value) invalidate()
  }
 }
})
onBeforeUnmount(() => { disposed = true; sequence++; clearTimeout(timer) })
</script>
<template>
 <el-dialog :model-value="modelValue" :title="runOnce ? '执行一次' : immediate ? '选题设置' : selected.map(c=>c.name).join('、')+' · 定时'" width="640px" class="screen-run-dialog" :close-on-click-modal="false" :close-on-press-escape="!starting" :show-close="!starting" @close="close">
  <div v-if="error" class="scope-error"><el-alert :title="error" type="error" :closable="false"/><el-button v-if="runOnce && immediate && options.columns.length" :disabled="loading || starting" @click="invalidate">重新检查</el-button></div>
  <template v-if="!opening && options.columns.length">
  <div v-if="!immediate" class="timing-line">
   <label for="screen-execution">执行时间</label>
   <el-select id="screen-execution" :model-value="execution" aria-label="执行时间" :disabled="starting || opening" @change="setExecution">
    <el-option v-if="execution === 'mixed'" label="各栏目不同" value="mixed" disabled/>
    <el-option label="按间隔" value="interval"/><el-option label="每天" value="daily"/><el-option label="每周" value="weekly"/>
   </el-select>
  </div>
  <div v-if="execution === 'mixed'" class="saved-plans">
   <div v-for="column in selected" :key="column.line_id" class="timing-line">
    <span>{{column.name}} · {{scheduleLabel(column.schedule) || '未设定时'}}</span>
   </div>
  </div>
  <template v-else-if="immediate">
  <template v-if="!runOnce">
  <div class="scope-mode">
   <el-radio-group v-model="mode" :disabled="starting" aria-label="取数方式">
    <el-radio-button value="time">按时间</el-radio-button>
    <el-tooltip content="暂无可用批次；旧采集未保存逐条批次关联" :disabled="!!availableBatches.length"><el-radio-button value="batches" :disabled="!availableBatches.length">按采集批次</el-radio-button></el-tooltip>
   </el-radio-group>
   <el-checkbox :model-value="allSelected" :indeterminate="ids.length > 0 && !allSelected" :disabled="starting || !available.length" @change="checked => ids = checked ? [...available] : []">全部栏目</el-checkbox>
  </div>
  <el-select v-if="mode === 'batches'" v-model="batches" multiple :disabled="starting" aria-label="采集批次" placeholder="选择采集批次" class="scope-batches"><el-option v-for="batch in availableBatches" :key="batch.id" :label="batchLabel(batch)" :value="batch.id"/></el-select>
  <div class="scope-columns" :class="{ 'batch-columns': mode === 'batches' }">
   <div v-for="column in options.columns" :key="column.line_id" class="scope-column">
    <el-checkbox :model-value="ids.includes(column.line_id)" :disabled="starting || !column.enabled || (runOnce && column.running)" @change="checked => toggleColumn(column.line_id, checked)">{{column.name}}</el-checkbox>
    <span v-if="runOnce && column.running" class="scope-unavailable">正在筛选</span>
    <span v-else-if="!column.enabled" class="scope-unavailable">已停用</span>
    <el-date-picker v-else-if="mode === 'time'" v-model="settings[column.line_id].dates" type="datetimerange" :shortcuts="shortcuts(column)" :disabled="starting || !ids.includes(column.line_id)" :clearable="false" :aria-label="`${column.name}时间范围`" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" format="MM-DD HH:mm" class="scope-range"/>
   </div>
  </div>
  </template>
  <div v-else class="scope-columns"><div v-for="column in options.columns" :key="column.line_id" class="scope-column"><el-checkbox :model-value="ids.includes(column.line_id)" :disabled="starting || !column.enabled || (runOnce && column.running)" @change="checked => toggleColumn(column.line_id, checked)">{{column.name}}</el-checkbox><span class="scope-unavailable">{{column.running ? '正在筛选' : rangeLabel(column)}}</span></div></div>
  <p v-if="preview && !total" class="scope-empty" role="status">所选范围没有新增或更新的线索</p>
  <section v-if="!runOnce" class="automatic-settings" aria-label="自动筛选">
   <div class="saved-summary">

    <button v-for="column in options.columns" :key="column.line_id" class="schedule-edit" @click="openAutomation([column.line_id], true)">{{column.name}} · {{scheduleLabel(column.schedule) || '未设定时'}}</button>
   </div>
   <el-button link type="primary" :disabled="starting || !hasRange" @click="openAutomation(manualIds)">设置自动</el-button>
  </section>
  <section v-if="!runOnce&&lines.length" class="draft-settings"><h3>二创选稿</h3><div v-for="line in lines" :key="line.id"><span>{{line.name}}</span><el-button size="small" @click="draftLine=line">选稿设置</el-button></div></section>
  </template>
  <div v-else class="recurrence">
   <div v-if="execution === 'interval'" class="timing-line"><label>间隔</label><el-input-number v-model="minutes" :min="1" :max="525600" :precision="0" controls-position="right" aria-label="筛选间隔"/><span>分钟</span></div>
   <template v-else>
    <div v-if="execution === 'weekly'" class="timing-line"><label>星期</label><el-checkbox-group v-model="weekdays" aria-label="每周执行日" class="weekdays"><el-checkbox-button v-for="(day,i) in weekdayNames" :key="day" :value="i+1" :aria-label="`周${day}`">{{day}}</el-checkbox-button></el-checkbox-group></div>
    <div class="timing-line time-row"><label>北京时间</label><div class="time-inputs">
     <div v-for="(_,i) in times" :key="i" class="time-entry"><el-time-picker v-model="times[i]" format="HH:mm" value-format="HH:mm" placeholder="选择时刻" :clearable="false" :aria-label="`执行时刻 ${i+1}`"/>
      <el-button v-if="times.length>1" text :aria-label="`移除时刻 ${i+1}`" @click="times.splice(i,1)">×</el-button></div>
     <el-button text type="primary" @click="addTime">＋ 添加时刻</el-button>
    </div></div>
   </template>

  </div>
  </template>
  <span v-else-if="opening" role="status">正在读取栏目设置…</span>
  <template #footer>
   <div class="dialog-actions">
    <el-button v-if="!immediate && execution !== 'mixed' && hasPlan" text type="danger" :disabled="starting" @click="saveSchedule(true)">关闭定时</el-button>
    <span v-if="runOnce && immediate && endsPrevious" class="scope-consequence">新一轮会结束旧任务，保留已完成结果。</span>
    <div class="actions">
     <el-button :disabled="starting" @click="immediate ? close() : execution = 'now'">{{immediate ? '取消' : '返回'}}</el-button>
     <el-button v-if="!opening && options.columns.length && immediate && runOnce" type="primary" :disabled="!preview || loading || !total" :loading="starting" @click="start">
      {{loading ? '正在检查范围' : '开始执行'}}
     </el-button>
     <el-button v-else-if="!opening && options.columns.length && immediate" type="primary" :disabled="!hasRange" :loading="starting" @click="saveScopes">保存设置</el-button>
     <el-button v-else-if="!opening && options.columns.length && execution !== 'mixed'" type="primary" :disabled="!validPlan" :loading="starting" @click="saveSchedule()">保存定时</el-button>
    </div>
   </div>
  </template>
 </el-dialog>
 <DraftPromptSettings v-if="draftLine" :line="draftLine" @saved="draftSaved" @close="draftLine=null"/>
</template>
<style scoped>
.draft-settings{border-top:1px solid #edf0ee;padding-top:16px;margin-top:16px}.draft-settings h3{font-size:14px;margin:0 0 8px}.draft-settings>div{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;font-size:14px}
.schedule-edit{border:0;padding:3px 0;background:none;font:inherit;color:#207469;cursor:pointer;text-align:left}
.automatic-settings{display:flex;align-items:center;justify-content:flex-end;gap:16px;flex-wrap:wrap;margin-top:16px}
.automatic-settings:has(.saved-summary){border-top:1px solid #edf0ee;padding-top:12px}
.saved-summary{flex:1;display:grid;gap:4px;font-size:13px;color:#52675d;line-height:1.6}
.scope-columns.batch-columns{flex-direction:row;flex-wrap:wrap;gap:8px 20px}
.batch-columns .scope-column{min-height:32px}
.batch-columns .scope-column>.el-checkbox{width:auto}
.timing-line{display:flex;align-items:center;gap:12px;margin-bottom:16px;min-width:0}
.timing-line>label{width:66px;flex-shrink:0;font-size:14px;color:#485b53}
.timing-line>.el-select{width:150px}.recurrence .timing-line:last-child{margin-bottom:0}
.time-inputs{display:flex;flex:1;align-items:center;flex-wrap:wrap;gap:8px 16px;min-width:0}
.time-row{align-items:flex-start}.time-row>label{padding-top:7px}
.time-entry{display:flex;align-items:center}.time-entry :deep(.el-date-editor){width:108px;height:38px}
.time-entry>.el-button{padding:5px}.weekdays{display:flex;flex-wrap:wrap}
.weekdays :deep(.el-checkbox-button__inner){padding:8px 11px}
@media(max-width:480px){.weekdays :deep(.el-checkbox-button__inner){padding:7px}.timing-line{gap:6px}.timing-line>label{width:58px}}

:global(.el-dialog.screen-run-dialog){width:640px;max-width:calc(100vw - 32px);margin:8vh auto;max-height:84vh;padding:24px}
:global(.screen-run-dialog .el-dialog__header){padding-bottom:24px}
:global(.screen-run-dialog .el-dialog__body){min-height:0;overflow:auto}
:global(.screen-run-dialog .el-dialog__footer){border-top:1px solid #edf0ee;margin-top:20px;padding-top:16px}
.scope-mode{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.scope-mode .el-checkbox{margin:0}.scope-batches{margin-bottom:16px}
.scope-columns{display:flex;flex-direction:column;gap:16px}.scope-column{display:flex;align-items:center;gap:20px;min-height:38px}.scope-column>.el-checkbox{margin:0;width:108px;flex:none}.scope-column :deep(.el-checkbox__label){white-space:normal;line-height:20px}.scope-range.el-date-editor{width:auto;min-width:0;flex:1;height:38px}.scope-range :deep(.el-range-input){min-width:0}.scope-unavailable{font-size:13px;color:#7c8983}
.scope-error{margin-bottom:16px}.scope-error>.el-button{margin-top:8px}.scope-empty{font-size:13px;color:#7c8983;margin:16px 0 0}.scope-consequence{color:#7c8983;font-size:12px;text-align:left}.dialog-actions{gap:12px}.dialog-actions .actions{margin-left:auto}
@media(max-width:580px){.scope-mode{flex-wrap:wrap}.scope-column{flex-wrap:wrap;gap:8px}.scope-column>.el-checkbox{width:100%}.scope-range.el-date-editor{flex-basis:100%}}
</style>
