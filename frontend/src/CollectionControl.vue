<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from './api'
defineProps({ compact: Boolean })
const emit = defineEmits(['changed'])
const data = ref(null), open = ref(false), form = ref({}), pending = ref(false), error = ref('')
const resultOpen = ref(false)
let timer, disposed = false
const collecting = computed(() => data.value?.run?.status === 'running')
const enabledSources = computed(() => data.value?.sources.filter(s => s.enabled) || [])
const selectedSources = computed(() => enabledSources.value.filter(s => form.value.scope === 'all' || (form.value.scope === 'custom' ? form.value.source_ids?.includes(s.id) : data.value?.lines.some(l => form.value.line_ids?.includes(l.id) && l.sources.includes(s.id)))))
const limited = computed({ get: () => form.value.per_source_limit !== null, set: v => form.value.per_source_limit = v ? 20 : null })
const summary = computed(() => {
 const s = data.value?.settings
 return s ? `${{all:'全渠道',columns:'按栏目',custom:'自定义渠道'}[s.scope]} · ${s.schedule_mode === 'daily' ? `每天 ${s.daily_time}` : s.interval_minutes ? `每 ${s.interval_minutes} 分钟` : '手动'}` : ''
})
const totals = computed(() => {
 const t = {new:0,changed:0,unchanged:0,failed:0}
 for (const s of data.value?.run?.sources || []) { for (const k of ['new','changed','unchanged']) t[k] += s.changes?.[k] || 0; if (s.status === 'failed') t.failed++ }
 return t
})
const nextTime = computed(() => data.value?.next_at ? new Date(data.value.next_at * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}) : '')
async function refresh() { try { data.value = await api('/api/collection') } catch (e) { error.value = e.message } }
async function poll() { await refresh(); if (!disposed) timer = setTimeout(poll, 5000) }
async function show() {
 error.value = ''; await refresh(); if (!data.value) return
 form.value = {...JSON.parse(JSON.stringify(data.value.settings)), schedule_mode:data.value.settings.schedule_mode || (data.value.settings.interval_minutes ? 'interval' : 'manual'), daily_time:data.value.settings.daily_time || '09:00'}
 open.value = true
}
watch(() => form.value.schedule_mode, mode => { if (mode === 'interval' && !form.value.interval_minutes) form.value.interval_minutes = 60 })
async function save() {
 pending.value = true; error.value = ''
 try {
  const {revision,...settings} = form.value
  if (settings.schedule_mode !== 'interval') settings.interval_minutes = null
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
 <div class="actions collection-control">
  <span v-if="!compact" class="muted collection-summary">{{summary}}</span>
  <el-button v-if="data?.run?.id||data?.schedule_error" text @click="resultOpen=true">{{collecting?'采集中…':data?.schedule_error?'定时采集失败':totals.failed?`${totals.failed} 个渠道失败`:'采集记录'}}</el-button>
  <el-button @click="show">采集设置</el-button>
  <el-button :disabled="!data||pending||collecting" @click="collect">立即采集</el-button>
 </div>
 <el-dialog v-model="open" class="collection-dialog" title="采集设置" width="560px" :close-on-click-modal="false" :close-on-press-escape="!pending" :show-close="!pending">
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <el-form label-position="top" :disabled="pending" @submit.prevent="save">
   <el-form-item label="采集范围">
    <el-radio-group v-model="form.scope" class="collection-segments"><el-radio-button value="all">全渠道</el-radio-button><el-radio-button value="columns">按栏目</el-radio-button><el-radio-button value="custom">自定义渠道</el-radio-button></el-radio-group>
    <el-checkbox-group v-if="form.scope==='columns'" v-model="form.line_ids" class="collection-options"><el-checkbox v-for="l in data?.lines" :key="l.id" :value="l.id">{{l.name}}</el-checkbox></el-checkbox-group>
    <el-checkbox-group v-if="form.scope==='custom'" v-model="form.source_ids" class="collection-options"><el-checkbox v-for="s in enabledSources" :key="s.id" :value="s.id">{{s.name}}</el-checkbox></el-checkbox-group>
    <span class="field-hint">{{selectedSources.length?`已选 ${selectedSources.length} 个渠道`:'请选择至少一个有效渠道'}}</span>
   </el-form-item>
   <el-form-item label="每渠道读取条数">
    <div class="field-inline"><el-radio-group v-model="limited"><el-radio-button :value="false">不限</el-radio-button><el-radio-button :value="true">指定条数</el-radio-button></el-radio-group><el-input-number v-if="limited" v-model="form.per_source_limit" :min="1" :max="10000" :precision="0" controls-position="right" aria-label="每渠道读取条数"/><span v-if="limited">条</span></div>
    <span class="field-hint">读取各渠道最新列表，重复内容自动合并。</span>
   </el-form-item>
   <el-form-item label="采集计划">
    <el-radio-group v-model="form.schedule_mode" class="collection-segments"><el-radio-button value="manual">手动</el-radio-button><el-radio-button value="interval">按间隔</el-radio-button><el-radio-button value="daily">每天定时</el-radio-button></el-radio-group>
    <div v-if="form.schedule_mode==='interval'" class="field-inline schedule-value"><span>每</span><el-input-number v-model="form.interval_minutes" :min="1" :max="10080" :precision="0" controls-position="right" aria-label="采集间隔"/><span>分钟采集一次</span></div>
    <div v-if="form.schedule_mode==='daily'" class="field-inline schedule-value"><span>每天</span><el-time-picker v-model="form.daily_time" format="HH:mm" value-format="HH:mm" :clearable="false" aria-label="每天采集时间"/><span>北京时间</span></div>
    <span v-if="form.schedule_mode!=='manual'" class="field-hint">服务运行时自动采集，关闭网页不影响。</span>
   </el-form-item>
  </el-form>
  <template #footer><div class="dialog-actions"><div class="actions"><el-button :disabled="pending" @click="open=false">取消</el-button><el-button type="primary" :loading="pending" :disabled="!selectedSources.length" @click="save">保存设置</el-button></div></div></template>
 </el-dialog>
 <el-drawer v-model="resultOpen" title="采集记录" size="480px">
  <el-alert v-if="error||data?.schedule_error" :title="error||data.schedule_error" type="error" :closable="false"/>
  <p v-if="nextTime" class="muted">下次采集：{{nextTime}}（北京时间）</p>
  <p>{{collecting?'正在采集':'最近一次采集'}} · 新增 {{totals.new}} · 更新 {{totals.changed}} · 重复 {{totals.unchanged}}</p>
  <p v-for="s in data?.run?.sources.filter(s=>s.status==='failed')||[]" :key="s.source_id" class="action-error">{{s.name}}：{{s.error}}</p>
  <el-button v-if="totals.failed" :disabled="collecting||pending" @click="retry">重试 {{totals.failed}} 个失败渠道</el-button>
 </el-drawer>
</template>
<style scoped>
.collection-segments{display:flex;width:100%}.collection-segments :deep(.el-radio-button){flex:1}.collection-segments :deep(.el-radio-button__inner){width:100%}
.collection-options{display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;width:100%;max-height:180px;overflow:auto;margin-top:12px}.collection-options .el-checkbox{margin:0}.collection-options :deep(.el-checkbox__label){white-space:normal}
.field-inline{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.field-inline .el-input-number{width:120px}.field-inline .el-date-editor{width:140px}.schedule-value{margin-top:12px}.field-hint{display:block;width:100%;font-size:12px;line-height:1.6;color:#7c8983;margin-top:8px}
@media(max-width:700px){.collection-summary{display:none}.collection-options{grid-template-columns:1fr}}
</style>
