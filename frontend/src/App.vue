<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const state = ref({ status: 'idle', batch: null, items: [], results: [], progress: {}, review: null, excluded_sources: [] })
const loading = ref(true)
const sending = ref(false)
const requestError = ref('')
const view = ref('initial')
const sourceFilter = ref('all')
const page = ref(1)
const expandedRefs = ref([])
const activeRef = ref(null)
const pageSize = 20
const rules = new Set(['观众想纠正你', '看翻车', '打假', '站队', '蹲后续', '当军师', '代入自己', '证明自己'])
let timer
let disposed = false
const review = computed(() => state.value.review || null)
const reviewBusy = computed(() => ['grouping', 'running', 'comparing'].includes(review.value?.status))
const busy = computed(() => sending.value || ['collecting', 'screening'].includes(state.value.status) || reviewBusy.value)
const done = computed(() => review.value?.status === 'completed')
const message = computed(() => requestError.value || review.value?.error || state.value.error || '')
const initial = computed(() => state.value.results.filter(result => Array.isArray(result.rules) && result.rules.length && result.rules.every(rule => rules.has(rule)) && typeof result.reason === 'string' && result.reason.trim()))
const hasInitial = computed(() => initial.value.length > 0 || state.value.status === 'completed' || done.value)
const itemByRef = computed(() => new Map(state.value.items.map(item => [item.ref, item])))
const initialByRef = computed(() => new Map(initial.value.map(item => [item.ref, item])))
const reviewRows = computed(() => {
  const used = new Set()
  const grouped = (review.value?.events || []).map(event => {
    const refs = (event.refs || []).filter(ref => initialByRef.value.has(ref))
    refs.forEach(ref => used.add(ref))
    return { key: event.id, refs, event }
  }).filter(row => row.refs.length)
  const waiting = initial.value.filter(item => !used.has(item.ref)).map(item => ({ key: item.ref, refs: [item.ref], event: null }))
  return [...grouped, ...waiting]
})
const expandedKeys = computed(() => reviewRows.value.filter(row => row.refs.some(ref => expandedRefs.value.includes(ref))).map(row => row.key))
const finalRows = computed(() => reviewRows.value.filter(row => row.event?.comparison?.selected === true).sort((a, b) => (a.event.comparison.rank ?? Infinity) - (b.event.comparison.rank ?? Infinity)))
const insufficientRows = computed(() => reviewRows.value.filter(row => row.event?.stage !== 'deferred' && row.event?.assessment?.decision === 'insufficient'))
const deferredRows = computed(() => reviewRows.value.filter(row => row.event?.stage === 'deferred'))
const selectedRows = computed(() => {
  const base = ({ final: finalRows.value, insufficient: insufficientRows.value, deferred: deferredRows.value })[view.value] || reviewRows.value
  return sourceFilter.value === 'all' ? base : base.filter(row => row.refs.some(ref => itemByRef.value.get(ref)?.source_id === sourceFilter.value))
})
const visibleRows = computed(() => selectedRows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const sources = computed(() => [...new Map(state.value.items.map(item => [item.source_id, item.source_name])).entries()])
const screenDisabled = computed(() => loading.value || busy.value || !state.value.batch || !state.value.items.length || done.value)
const screenLabel = computed(() => initial.value.length || state.value.results.length ? '继续筛选' : '筛选这批')
const phaseNames = { grouping: '整理同一事件', planning: '确定取材顺序', discovery: '查找来源', reading: '读取正文', assessment: '复筛判断', reviewing: '读取材料与复筛', running: '读取材料与复筛', comparing: '横向取舍', comparison: '横向取舍', initial: '初筛', screening: '初筛' }
const stageNames = { initial: '初筛', planning: '合并事件与取材计划', discovery: '查找来源', reading: '读取正文', assessment: '复筛判断', comparison: '横向取舍' }
const phaseText = computed(() => phaseNames[review.value?.phase] || phaseNames[review.value?.status] || '筛选')
const progressText = computed(() => {
  if (state.value.status === 'collecting') return '正在采集'
  if (state.value.status === 'screening' && (!review.value || review.value.status === 'idle')) return `初筛 ${state.value.progress?.completed ?? 0} / ${state.value.progress?.total ?? state.value.items.length}`
  if (!review.value) return ''
  if (reviewBusy.value) {
    const pipeline = review.value.pipeline
    if (pipeline?.active_stages?.length) return pipeline.active_stages.map(name => {
      const stage = pipeline.stages?.[name]
      const progress = stage?.total == null || stage?.completed == null ? '' : ` ${stage.completed} / ${stage.total}`
      return `${stageNames[name] || phaseNames[name] || name}${progress}`
    }).join(' · ')
    const p = review.value.progress || {}
    return `${phaseText.value}${review.value.phase !== 'reviewing' || p.total == null ? '' : ` ${p.completed ?? 0} / ${p.total}`}`
  }
  return ({ stopped: '已停止，可继续筛选', failed: '筛选未完成，可继续', completed: '筛选完成', idle: '初筛已完成，可继续筛选' })[review.value.status] || ''
})
const usageText = computed(() => {
  const u = review.value?.usage
  if (!u) return ''
  const n = value => value == null ? '未记录' : Number(value).toLocaleString('zh-CN')
  const missing = review.value?.pipeline?.usage_unrecorded_calls
  return `输入 ${n(u.input_tokens)}（缓存 ${n(u.cached_input_tokens)}） · 输出 ${n(u.output_tokens)}（推理 ${n(u.reasoning_output_tokens)}）${missing > 0 ? ` · ${missing} 次调用未记录用量` : ''}`
})
watch([view, sourceFilter], () => { page.value = 1 })
watch(() => state.value.batch?.id, () => { page.value = 1; sourceFilter.value = 'all'; view.value = 'initial'; expandedRefs.value = []; activeRef.value = null })
watch(done, value => { if (value) view.value = 'final' })
function date(value) {
  if (!value) return ''
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(d).replaceAll('/', '-')
}
function number(value) { return value == null ? '未记录' : Number(value).toLocaleString('zh-CN') }
function duration(value, estimated) {
  if (value == null) return '未记录'
  const seconds = Math.round(value)
  const text = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return text + (estimated ? '（估算）' : '')
}
function safeLink(url) { try { const p = new URL(url); return ['http:', 'https:'].includes(p.protocol) ? p.href : null } catch { return null } }
function readableReason(value) {
  return (value || '').replace(/\b(?:toutiao|baidu|weibo|zhihu):\d+\b/g, ref => {
    const title = itemByRef.value.get(ref)?.title
    return title ? `“${title}”` : ref
  })
}
function metrics(item) {
  const data = item.metrics || {}
  const fields = [['HotValue', '热度'], ['hotScore', '热度'], ['display', '热度']]
  return fields.filter(([key]) => data[key] !== null && data[key] !== undefined && data[key] !== '').map(([key, name]) => `${name} ${data[key]}`).join(' · ') || '—'
}
function conclusion(row) {
  const event = row.event
  if (!event) return '等待处理'
  if (event.stage === 'deferred') return `未进入取材：${readableReason(event.plan?.reason) || '未提供原因'}`
  if (event.comparison) return event.comparison.selected ? readableReason(event.comparison.reason) : `未入选：${readableReason(event.comparison.reason)}`
  if (event.assessment) {
    const label = { keep: '待横向比较', drop: '不保留', insufficient: '材料不足' }[event.assessment.decision] || '已判断'
    return `${label}：${readableReason(event.assessment.reason)}`
  }
  return ({ queued: '等待处理', searching: '查找来源', reading: '读取正文', judging: '正在判断', judged: '已判断', failed: '处理失败，需继续' })[event.stage] || '等待处理'
}
function mainRules(row) {
  if (view.value === 'final' && Array.isArray(row.event?.comparison?.final_rules)) return row.event.comparison.final_rules
  return row.event?.assessment?.rules?.length ? row.event.assessment.rules : [...new Set(row.refs.flatMap(ref => initialByRef.value.get(ref)?.rules || []))]
}
function isExpanded(row) { return row.refs.some(ref => expandedRefs.value.includes(ref)) }
function toggleEvidence(row) {
  if (isExpanded(row)) {
    expandedRefs.value = expandedRefs.value.filter(ref => !row.refs.includes(ref))
    if (row.refs.includes(activeRef.value)) activeRef.value = null
  } else {
    expandedRefs.value = [...expandedRefs.value, row.refs[0]]
    activeRef.value = row.refs[0]
  }
}
function rowClass({ row }) { return row.refs.includes(activeRef.value) ? 'viewing-row' : '' }
function activateRow(row, _column, event) { if (isExpanded(row) && !event.target.closest('a, button')) activeRef.value = row.refs[0] }
async function request(path, options) {
  const response = await fetch(path, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.error || `请求失败（${response.status}）`)
  return body
}
async function refresh() {
  try { const next = await request('/api/state'); state.value = { ...next, items: next.items || [], results: next.results || [] }; requestError.value = '' }
  catch (error) { requestError.value = `读取状态失败：${error.message}` }
  finally { loading.value = false }
}
async function run(path, body) {
  sending.value = true; requestError.value = ''
  try { await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); await refresh() }
  catch (error) { requestError.value = error.message }
  finally { sending.value = false }
}
async function poll() { await refresh(); if (!disposed) timer = window.setTimeout(poll, 3000) }
onMounted(poll)
onBeforeUnmount(() => { disposed = true; window.clearTimeout(timer) })
</script>

<template>
  <main class="workspace">
    <header class="header">
      <div><h1>热点采集与筛选</h1><p class="subtitle">采集当前热点，筛选值得关注的内容</p></div>
      <div class="actions">
        <el-button type="primary" :disabled="loading || busy" @click="run('/api/collect', {})">采集一次</el-button>
        <el-button :disabled="screenDisabled" @click="run('/api/screen', { batch_id: state.batch.id })">{{ screenLabel }}</el-button>
      </div>
    </header>
    <section v-if="state.batch" class="batch" aria-label="本批采集情况">
      <div class="batch-time">采集时间 <strong>{{ date(state.batch.started_at) }}</strong></div>
      <div class="source-summary"><span v-for="source in (state.batch.sources || []).filter(source => source.source_id !== 'bilibili')" :key="source.source_id" :class="{ failed: source.status === 'failed' }">{{ source.name }}：{{ source.status === 'failed' ? `失败${source.error ? `（${source.error}）` : ''}` : `${source.count ?? 0} 条` }}</span></div>
      <p v-if="state.excluded_sources?.some(source => source.source_id === 'bilibili')" class="excluded">已排除本批 B 站条目</p>
    </section>
    <div v-if="progressText || usageText" class="progress-line">
      <span>{{ progressText }}</span><span v-if="usageText" class="usage">{{ usageText }} tokens</span>
      <el-button v-if="reviewBusy || state.status === 'screening'" size="small" :disabled="sending" @click="run('/api/stop', { batch_id: state.batch.id })">停止</el-button>
    </div>
    <el-collapse v-if="review" class="process"><el-collapse-item title="本轮过程">
      <template v-if="review.pipeline">
        <div class="process-wall">总历时：{{ duration(review.pipeline.wall?.elapsed_seconds, review.pipeline.wall?.estimated) }} · 开始：{{ date(review.pipeline.wall?.started_at) || '未记录' }} · 结束：{{ date(review.pipeline.wall?.finished_at) || '未记录' }}{{ review.pipeline.wall?.estimated ? '（时间估算）' : '' }}</div>
        <div v-for="(name, key) in stageNames" :key="key" class="process-stage">
          <strong>{{ name }}</strong>
          <span>已处理 {{ number(review.pipeline.stages?.[key]?.completed) }} / {{ number(review.pipeline.stages?.[key]?.total) }}</span>
          <span>调用 {{ number(review.pipeline.stages?.[key]?.cli_calls) }} 次</span>
          <span>模型累计耗时 {{ duration(review.pipeline.stages?.[key]?.model_seconds, review.pipeline.stages?.[key]?.time_estimated) }}</span>
          <span>阶段跨度 {{ duration(review.pipeline.stages?.[key]?.elapsed_seconds, review.pipeline.stages?.[key]?.time_estimated) }}</span>
          <span>开始 {{ date(review.pipeline.stages?.[key]?.started_at) || '未记录' }} · 结束 {{ date(review.pipeline.stages?.[key]?.finished_at) || '未记录' }}{{ review.pipeline.stages?.[key]?.time_estimated ? '（时间估算）' : '' }}</span>
          <span v-if="review.pipeline.stages?.[key]?.input_tokens != null || review.pipeline.stages?.[key]?.output_tokens != null">输入 {{ number(review.pipeline.stages?.[key]?.input_tokens) }}（缓存 {{ number(review.pipeline.stages?.[key]?.cached_input_tokens) }}） · 输出 {{ number(review.pipeline.stages?.[key]?.output_tokens) }}（推理 {{ number(review.pipeline.stages?.[key]?.reasoning_output_tokens) }}） tokens</span>
        </div>
      </template>
      <p v-else class="muted">本轮阶段记录尚不可用。</p>
    </el-collapse-item></el-collapse>
    <el-alert v-if="message" class="error" :title="message" type="error" :closable="false" show-icon />
    <section v-if="!loading && state.batch && state.items.length" class="content">
      <div class="toolbar">
        <div v-if="hasInitial" class="result-filters">
          <el-radio-group v-model="view" aria-label="筛选结果">
            <el-radio-button value="final">最终候选 {{ finalRows.length }}</el-radio-button>
            <el-radio-button value="initial">初筛保留 {{ initial.length }}</el-radio-button>
            <el-radio-button value="insufficient">材料不足 {{ insufficientRows.length }}</el-radio-button>
            <el-radio-button v-if="deferredRows.length" value="deferred">未进入取材 {{ deferredRows.length }}</el-radio-button>
          </el-radio-group>
        </div>
        <h2 v-else>本批条目 <span>{{ state.items.length }} 条</span></h2>
        <el-select v-model="sourceFilter" class="source-select" aria-label="按来源筛选"><el-option label="全部来源" value="all" /><el-option v-for="[id, name] in sources" :key="id" :label="name" :value="id" /></el-select>
      </div>
      <template v-if="hasInitial">
        <el-table :data="visibleRows" row-key="key" :expand-row-keys="expandedKeys" :row-class-name="rowClass" table-layout="fixed" :empty-text="view === 'final' && !done ? '横向取舍完成后显示最终候选' : '没有符合条件的结果'" @row-click="activateRow">
          <el-table-column type="expand" width="20"><template #default="{ row }">
            <div class="evidence" @click="activeRef = row.refs[0]">
              <h3>初筛理由</h3><div v-for="ref in row.refs" :key="ref" class="evidence-entry"><strong>{{ itemByRef.get(ref)?.title }}</strong><p>{{ initialByRef.get(ref)?.rules?.join('、') }}：{{ readableReason(initialByRef.get(ref)?.reason) }}</p></div>
              <template v-if="row.event?.stage === 'deferred'"><h3>未进入取材</h3><p>{{ readableReason(row.event.plan?.reason) || '未提供原因' }}</p></template>
              <h3>来源材料</h3>
              <template v-if="row.event?.materials?.length"><div v-for="(material, index) in row.event.materials" :key="index" class="evidence-entry">
                <div><a v-if="safeLink(material.final_url || material.url)" :href="safeLink(material.final_url || material.url)" target="_blank" rel="noopener noreferrer">{{ material.title || material.url }}</a><span v-else>{{ material.title || material.url || '来源' }}</span> <span class="muted">{{ material.publisher }} {{ material.published_at ? `· 发布 ${date(material.published_at)}` : '' }} · {{ material.status === 'read' ? '已读取' : '读取失败' }}{{ material.error ? `：${material.error}` : '' }}</span></div>
                <p v-for="fact in (row.event.assessment?.facts || []).filter(f => f.url === material.url || f.url === material.final_url)" :key="fact.quote" class="quote">{{ fact.quote }}</p>
                <el-collapse v-if="material.content"><el-collapse-item title="查看来源正文"><div class="reason">{{ material.content }}</div></el-collapse-item></el-collapse>
              </div></template><p v-else class="muted">尚无已读取材料</p>
              <h3>复筛判断</h3><p><template v-if="row.event?.assessment?.rules?.length">{{ row.event.assessment.rules.join('、') }}：</template>{{ readableReason(row.event?.assessment?.reason) || '尚未完成' }}</p>
              <h3>横向取舍</h3><p>{{ readableReason(row.event?.comparison?.reason) || '尚未完成' }}</p>
            </div>
          </template></el-table-column>
          <el-table-column label="标题与来源" min-width="360"><template #default="{ row }"><div v-for="ref in row.refs" :key="ref" class="title-line"><a v-if="safeLink(itemByRef.get(ref)?.url)" :href="safeLink(itemByRef.get(ref).url)" target="_blank" rel="noopener noreferrer">{{ itemByRef.get(ref)?.title }}</a><span v-else>{{ itemByRef.get(ref)?.title }}</span><span class="row-source"> · {{ itemByRef.get(ref)?.source_name }}</span></div><el-button class="evidence-action" link type="primary" :aria-expanded="isExpanded(row)" @click.stop="toggleEvidence(row)">{{ isExpanded(row) ? '收起依据' : '查看依据' }}</el-button></template></el-table-column>
          <el-table-column label="规则" min-width="150"><template #default="{ row }">{{ mainRules(row).join('、') || '—' }}</template></el-table-column>
          <el-table-column label="判断与理由" min-width="420"><template #default="{ row }"><span class="reason">{{ conclusion(row) }}</span></template></el-table-column>
        </el-table>
        <div v-if="selectedRows.length > pageSize" class="pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" :total="selectedRows.length" layout="total, prev, pager, next" /></div>
      </template>
      <el-table v-else :data="state.items.filter(item => sourceFilter === 'all' || item.source_id === sourceFilter).slice((page - 1) * pageSize, page * pageSize)" table-layout="fixed" empty-text="没有符合条件的条目">
        <el-table-column prop="source_name" label="来源" width="120" /><el-table-column label="排名" width="90"><template #default="{ row }">{{ row.rank ?? '—' }}</template></el-table-column><el-table-column label="标题" min-width="360"><template #default="{ row }"><a v-if="safeLink(row.url)" :href="safeLink(row.url)" target="_blank" rel="noopener noreferrer">{{ row.title }}</a><span v-else>{{ row.title }}</span></template></el-table-column><el-table-column label="来源指标" min-width="180"><template #default="{ row }">{{ metrics(row) }}</template></el-table-column>
      </el-table>
      <div v-if="!hasInitial && state.items.filter(item => sourceFilter === 'all' || item.source_id === sourceFilter).length > pageSize" class="pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" :total="state.items.filter(item => sourceFilter === 'all' || item.source_id === sourceFilter).length" layout="total, prev, pager, next" /></div>
    </section>
    <div v-else-if="!loading && !busy && !state.batch && !message" class="empty">还没有采集内容，点击“采集一次”开始。</div>
    <div v-else-if="!loading && !busy && state.batch && !state.items.length && !message" class="empty">本批没有采集到条目。</div>
  </main>
</template>
