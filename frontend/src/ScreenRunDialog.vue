<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from './api'
const props = defineProps({ modelValue: Boolean, initialIds: Array })
const emit = defineEmits(['update:modelValue', 'started'])
const options = ref({ columns: [], batches: [] }), ids = ref([]), settings = ref({})
const mode = ref('time'), batches = ref([]), preview = ref(null)
const loading = ref(false), starting = ref(false), error = ref('')
let sequence = 0, timer, disposed = false
const selected = computed(() => options.value.columns.filter(c => ids.value.includes(c.line_id)))
const available = computed(() => options.value.columns.filter(c => c.enabled && !c.running).map(c => c.line_id))
const availableBatches = computed(() => options.value.batches.filter(b => b.selectable))
const allSelected = computed(() => available.value.length > 0 && available.value.every(id => ids.value.includes(id)))
const total = computed(() => preview.value?.columns.reduce((sum, c) => sum + c.items.length, 0) || 0)
const endsPrevious = computed(() => selected.value.some(c => c.unfinished && preview.value?.columns.some(p => p.line_id === c.line_id && p.items.length)))
function toggleColumn(id, checked) { ids.value = checked ? [...ids.value, id] : ids.value.filter(v => v !== id) }
function close() { if (!starting.value) emit('update:modelValue', false) }
function datesFor(hours) { const end = new Date(); return [new Date(end.getTime() - hours * 3600000), end] }
function shortcuts(hours) { return [...new Set([24, 48, 72, 168, hours])].sort((a,b) => a-b).map(h => ({ text: `最近 ${h} 小时`, value: () => datesFor(h) })) }
function stamp(value) { return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) }
function batchLabel(batch) { return `${stamp(batch.started_at)} · ${batch.count} 条` }
function requestBody() {
 return { columns: selected.value.map(c => {
  const s = settings.value[c.line_id]
  return { line_id: c.line_id, action: c.unfinished ? 'replace' : 'new', mode: mode.value,
   hours: mode.value === 'time' ? 'custom' : s.hours, batch_ids: batches.value,
   start: mode.value === 'time' ? s.dates?.[0]?.toISOString() : undefined,
   end: mode.value === 'time' ? s.dates?.[1]?.toISOString() : undefined }
 }) }
}
async function calculate(token) {
 if (!selected.value.length || (mode.value === 'batches' && !batches.value.length) ||
  (mode.value === 'time' && selected.value.some(c => settings.value[c.line_id].dates?.length !== 2))) {
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
 if (!props.modelValue) return
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
watch([ids, settings, mode, batches], invalidate, { deep: true })
watch(() => props.modelValue, async open => {
 sequence++; clearTimeout(timer); preview.value = null; error.value = ''
 if (!open) return
 loading.value = true; const token = sequence
 try {
  const result = await api('/api/screen/options')
  if (token !== sequence || disposed || !props.modelValue) return
  options.value = result
  settings.value = Object.fromEntries(result.columns.map(c => [c.line_id, { hours: c.hours, dates: datesFor(c.hours) }]))
  ids.value = (props.initialIds || []).filter(id => available.value.includes(id))
  mode.value = 'time'; batches.value = []; invalidate()
 } catch (e) { if (token === sequence) { error.value = e.message; loading.value = false } }
})
onBeforeUnmount(() => { disposed = true; sequence++; clearTimeout(timer) })
</script>
<template>
 <el-dialog :model-value="modelValue" title="更新选题" width="640px" class="screen-run-dialog" :close-on-click-modal="false" :close-on-press-escape="!starting" :show-close="!starting" @close="close">
  <div v-if="error" class="scope-error"><el-alert :title="error" type="error" :closable="false"/><el-button :disabled="loading || starting" @click="invalidate">重新检查</el-button></div>
  <div class="scope-mode">
   <el-radio-group v-model="mode" :disabled="starting" aria-label="取数方式">
    <el-radio-button value="time">按时间</el-radio-button>
    <el-tooltip content="暂无可用批次；旧采集未保存逐条批次关联" :disabled="!!availableBatches.length"><el-radio-button value="batches" :disabled="!availableBatches.length">按采集批次</el-radio-button></el-tooltip>
   </el-radio-group>
   <el-checkbox :model-value="allSelected" :indeterminate="ids.length > 0 && !allSelected" :disabled="starting || !available.length" @change="checked => ids = checked ? [...available] : []">全部栏目</el-checkbox>
  </div>
  <el-select v-if="mode === 'batches'" v-model="batches" multiple :disabled="starting" aria-label="采集批次" placeholder="选择采集批次" class="scope-batches"><el-option v-for="batch in availableBatches" :key="batch.id" :label="batchLabel(batch)" :value="batch.id"/></el-select>
  <div class="scope-columns">
   <div v-for="column in options.columns" :key="column.line_id" class="scope-column">
    <el-checkbox :model-value="ids.includes(column.line_id)" :disabled="starting || !column.enabled || column.running" @change="checked => toggleColumn(column.line_id, checked)">{{column.name}}</el-checkbox>
    <span v-if="column.running" class="scope-unavailable">正在筛选</span>
    <span v-else-if="!column.enabled" class="scope-unavailable">已停用</span>
    <el-date-picker v-else-if="mode === 'time'" v-model="settings[column.line_id].dates" type="datetimerange" :shortcuts="shortcuts(column.hours)" :disabled="starting || !ids.includes(column.line_id)" :clearable="false" :aria-label="`${column.name}时间范围`" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" format="MM-DD HH:mm" class="scope-range"/>
    <el-select v-else v-model="settings[column.line_id].hours" :disabled="starting || !ids.includes(column.line_id)" :aria-label="`${column.name}内容时效`" class="scope-age"><el-option v-for="hours in [...new Set([column.hours,24,48,72,168])]" :key="hours" :label="`最近 ${hours} 小时`" :value="hours"/></el-select>
   </div>
  </div>
  <p v-if="preview && !total" class="scope-empty" role="status">所选范围没有新增或更新的线索</p>
  <template #footer><div class="dialog-actions"><span v-if="endsPrevious" class="scope-consequence">新一轮会结束旧任务，保留已完成结果。</span><div class="actions"><el-button :disabled="starting" @click="close">取消</el-button><el-button type="primary" :disabled="!preview || loading || !total" :loading="starting" @click="start">{{loading ? '正在检查范围' : '开始新一轮'}}</el-button></div></div></template>
 </el-dialog>
</template>
<style scoped>
:global(.el-dialog.screen-run-dialog){width:640px;max-width:calc(100vw - 32px);margin:8vh auto;max-height:84vh;padding:24px}
:global(.screen-run-dialog .el-dialog__header){padding-bottom:24px}
:global(.screen-run-dialog .el-dialog__body){min-height:0;overflow:auto}
:global(.screen-run-dialog .el-dialog__footer){border-top:1px solid #edf0ee;margin-top:20px;padding-top:16px}
.scope-mode{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.scope-mode .el-checkbox{margin:0}.scope-batches{margin-bottom:16px}
.scope-columns{display:flex;flex-direction:column;gap:16px}.scope-column{display:flex;align-items:center;gap:20px;min-height:38px}.scope-column>.el-checkbox{margin:0;width:108px;flex:none}.scope-column :deep(.el-checkbox__label){white-space:normal;line-height:20px}.scope-range.el-date-editor{width:auto;min-width:0;flex:1;height:38px}.scope-range :deep(.el-range-input){min-width:0}.scope-age{flex:1}.scope-unavailable{font-size:13px;color:#7c8983}
.scope-error{margin-bottom:16px}.scope-error>.el-button{margin-top:8px}.scope-empty{font-size:13px;color:#7c8983;margin:16px 0 0}.scope-consequence{color:#7c8983;font-size:12px;text-align:left}.dialog-actions{gap:12px}.dialog-actions .actions{margin-left:auto}
@media(max-width:580px){.scope-mode{flex-wrap:wrap}.scope-column{flex-wrap:wrap;gap:8px}.scope-column>.el-checkbox{width:100%}.scope-range.el-date-editor{flex-basis:100%}.scope-age{flex-basis:100%}}
</style>
