<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ProductionWorkspace from './ProductionWorkspace.vue'

const state = ref({ status: 'idle', batch: null, items: [], results: [], progress: {}, review: null, excluded_sources: [] })
const loading = ref(true)
const sending = ref(false)
const requestError = ref('')
const view = ref('initial')
const sourceFilter = ref('all')
const page = ref(1)
const expandedRefs = ref([])
const activeRef = ref(null)
const detailKey = ref(null)
const detailOpen = ref(false)
const selectionOpen = ref(false)
const selectedEvents = ref([])
const productionWorkspace = ref(null)
const pageSize = 20
const rules = new Set(['观众想纠正你', '看翻车', '打假', '站队', '蹲后续', '当军师', '代入自己', '证明自己'])
let timer
let disposed = false
const review = computed(() => state.value.review || null)
const rankingBusy = computed(() => review.value?.ranking?.status === 'running')
const reviewBusy = computed(() => ['grouping', 'running', 'comparing'].includes(review.value?.status))
const busy = computed(() => sending.value || ['collecting', 'screening'].includes(state.value.status) || reviewBusy.value || rankingBusy.value)
const done = computed(() => review.value?.status === 'completed')
const message = computed(() => requestError.value || review.value?.ranking?.error || review.value?.error || state.value.error || '')
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
const finalRows = computed(() => reviewRows.value.filter(row => row.event?.comparison?.selected === true).sort((a, b) => (a.event.ranking?.rank ?? a.event.comparison.rank ?? Infinity) - (b.event.ranking?.rank ?? b.event.comparison.rank ?? Infinity)))
const ranked = computed(() => finalRows.value.length > 0 && finalRows.value.every(row => row.event?.ranking))
const reviewedRows = computed(() => reviewRows.value.filter(row => row.event?.assessment))
const passedRows = computed(() => reviewedRows.value.filter(row => row.event.assessment.decision === 'keep'))
const notSelectedRows = computed(() => passedRows.value.filter(row => row.event?.comparison?.selected === false))
const insufficientRows = computed(() => reviewRows.value.filter(row => row.event?.stage !== 'deferred' && row.event?.assessment?.decision === 'insufficient'))
const deferredRows = computed(() => reviewRows.value.filter(row => row.event?.stage === 'deferred'))
const detailRow = computed(() => reviewRows.value.find(row => row.key === detailKey.value))
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
  if (rankingBusy.value) return '正在比较候选并生成排名'
  if (state.value.status === 'collecting') return '正在采集'
  if (state.value.status === 'failed') return '筛选未完成，可继续'
  if (state.value.status === 'screening' && (!review.value || review.value.status === 'idle')) return `初筛 ${state.value.progress?.completed ?? 0} / ${state.value.progress?.total ?? state.value.items.length}`
  if (!review.value) return ''
  if (review.value.status === 'idle') {
    const total = state.value.items.length
    return total > 0 && state.value.progress?.completed === total ? '初筛已完成，可继续筛选' : total > 0 ? '等待筛选' : ''
  }
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
  return ({ stopped: '已停止，可继续筛选', failed: '筛选未完成，可继续', completed: '筛选完成' })[review.value.status] || ''
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
watch(() => state.value.batch?.id, () => { detailOpen.value = false; selectionOpen.value = false; detailKey.value = null })
watch(() => state.value.batch?.id, () => { selectedEvents.value = [] })
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
function nextTitle(row) {
  const next = reviewRows.value.find(value => value.key === row.event?.ranking?.ahead_of)
  return next ? itemByRef.value.get(next.refs[0])?.title : ''
}
function openDetail(row) { detailKey.value = row.key; detailOpen.value = true }
function rowSources(row) { return [...new Set(row.refs.map(ref => itemByRef.value.get(ref)?.source_name).filter(Boolean))].join(' · ') }
function sourceFacts(row, material) {
  const facts = [...(row.event?.ranking?.facts || []), ...(row.event?.assessment?.facts || [])]
  return facts.filter((fact, index) => (fact.url === material.url || fact.url === material.final_url) && facts.findIndex(value => value.url === fact.url && value.quote === fact.quote) === index)
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
      <div><h1>热点采集与筛选</h1></div>
      <div class="actions">
        <el-button type="primary" :disabled="loading || busy" @click="run('/api/collect', {})">采集一次</el-button>
        <el-button v-if="!done" :disabled="screenDisabled" @click="run('/api/screen', { batch_id: state.batch.id })">{{ screenLabel }}</el-button>
      </div>
    </header>
    <section v-if="state.batch" class="batch" aria-label="本批采集情况">
      <div class="batch-time">{{ date(state.batch.started_at) }} 采集 <span class="batch-separator">·</span> {{ state.items.length }} 条 <span class="batch-separator">·</span> <span class="batch-status">{{ progressText }}</span></div>
      <p v-if="state.excluded_sources?.some(source => source.source_id === 'bilibili')" class="excluded">已排除本批 B 站条目</p>
    </section>
    <div v-if="busy" class="progress-line">
      <span>{{ progressText }}</span>
      <el-button v-if="reviewBusy || rankingBusy || state.status === 'screening'" size="small" :disabled="sending" @click="run('/api/stop', { batch_id: state.batch.id })">停止</el-button>
    </div>
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
        <div v-if="view === 'final' && finalRows.length" class="shortlist-bar">
          <span>{{ ranked ? '按推荐顺序排列' : '按原筛选顺序排列' }}</span>
          <div><el-button type="primary" size="small" :disabled="!selectedEvents.length" @click="productionWorkspace?.openCreate()">已选 {{ selectedEvents.length }} · 批量创作</el-button><el-button link @click="selectionOpen = true">筛选与排名依据</el-button><el-button link :disabled="busy" :loading="rankingBusy" @click="run('/api/rank', { batch_id: state.batch.id })">{{ ranked ? '重新排名' : '生成排名' }}</el-button></div>
        </div>
        <el-table :data="visibleRows" :class="{ 'shortlist-table': view === 'final' }" row-key="key" :expand-row-keys="expandedKeys" :row-class-name="rowClass" table-layout="fixed" :empty-text="view === 'final' && !done ? '横向取舍完成后显示最终候选' : '没有符合条件的结果'" @row-click="activateRow">
          <el-table-column v-if="view !== 'final'" type="expand" width="20"><template #default="{ row }">
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
              <template v-if="row.event?.ranking"><h3>排名依据原句</h3><div v-for="fact in row.event.ranking.facts" :key="fact.url + fact.quote" class="evidence-entry"><a v-if="safeLink(fact.url)" :href="safeLink(fact.url)" target="_blank" rel="noopener noreferrer">查看原文</a><p class="quote">{{ fact.quote }}</p></div></template>
            </div>
          </template></el-table-column>
          <el-table-column v-if="view === 'final'" width="48"><template #default="{ row }"><el-checkbox v-model="selectedEvents" :value="row.event.id" :aria-label="`选择 ${itemByRef.get(row.refs[0])?.title}`" @click.stop /></template></el-table-column>
          <el-table-column v-if="view === 'final'" label="排名" width="66"><template #default="{ row }"><span class="rank-number" :class="{ 'rank-leading': (row.event.ranking?.rank ?? row.event.comparison.rank) <= 3 }">{{ row.event.ranking?.rank ?? row.event.comparison.rank ?? '—' }}</span></template></el-table-column>
          <el-table-column label="标题与来源" min-width="360"><template #default="{ row }">
            <template v-if="view === 'final'"><button class="candidate-title" :title="itemByRef.get(row.refs[0])?.title" @click="openDetail(row)">{{ itemByRef.get(row.refs[0])?.title }}</button><div class="candidate-source">{{ rowSources(row) }}</div></template>
            <template v-else><div v-for="ref in row.refs" :key="ref" class="title-line"><a v-if="safeLink(itemByRef.get(ref)?.url)" :href="safeLink(itemByRef.get(ref).url)" target="_blank" rel="noopener noreferrer">{{ itemByRef.get(ref)?.title }}</a><span v-else>{{ itemByRef.get(ref)?.title }}</span><span class="row-source"> · {{ itemByRef.get(ref)?.source_name }}</span></div><el-button class="evidence-action" link type="primary" :aria-expanded="isExpanded(row)" @click.stop="toggleEvidence(row)">{{ isExpanded(row) ? '收起依据' : '查看依据' }}</el-button></template>
          </template></el-table-column>
          <el-table-column v-if="view !== 'final'" label="规则" min-width="150"><template #default="{ row }">{{ mainRules(row).join('、') || '—' }}</template></el-table-column>
          <el-table-column :label="view === 'final' ? '推荐看点' : '判断与理由'" min-width="390"><template #default="{ row }">
            <span v-if="view === 'final'" class="candidate-hook" :title="row.event?.ranking?.hook || conclusion(row)">{{ row.event?.ranking?.hook || conclusion(row) }}</span>
            <span v-else class="reason">{{ conclusion(row) }}</span>
          </template></el-table-column>
          <el-table-column v-if="view === 'final'" label="" width="88"><template #default="{ row }"><el-button link type="primary" @click="openDetail(row)">查看依据</el-button></template></el-table-column>
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
    <el-collapse v-if="review && state.batch" class="process"><el-collapse-item title="采集与运行记录">
      <div class="source-summary"><span v-for="source in (state.batch.sources || []).filter(source => source.source_id !== 'bilibili')" :key="source.source_id" :class="{ failed: source.status === 'failed' }">{{ source.name }}：{{ source.status === 'failed' ? `失败（${source.error || '原因未记录'}）` : `${source.count ?? 0} 条` }}</span></div>
      <p class="usage">{{ usageText }} tokens</p>
      <template v-if="review.pipeline">
        <div class="process-wall">总历时：{{ duration(review.pipeline.wall?.elapsed_seconds, review.pipeline.wall?.estimated) }} · 开始：{{ date(review.pipeline.wall?.started_at) || '未记录' }} · 结束：{{ date(review.pipeline.wall?.finished_at) || '未记录' }}</div>
        <div v-for="(name, key) in stageNames" :key="key" class="process-stage"><strong>{{ name }}</strong><span>已处理 {{ number(review.pipeline.stages?.[key]?.completed) }} / {{ number(review.pipeline.stages?.[key]?.total) }}</span><span>调用 {{ number(review.pipeline.stages?.[key]?.cli_calls) }} 次</span><span>模型累计耗时 {{ duration(review.pipeline.stages?.[key]?.model_seconds, review.pipeline.stages?.[key]?.time_estimated) }}</span><span>阶段跨度 {{ duration(review.pipeline.stages?.[key]?.elapsed_seconds, review.pipeline.stages?.[key]?.time_estimated) }}</span><span>开始 {{ date(review.pipeline.stages?.[key]?.started_at) || '未记录' }} · 结束 {{ date(review.pipeline.stages?.[key]?.finished_at) || '未记录' }}</span><span>输入 {{ number(review.pipeline.stages?.[key]?.input_tokens) }}（缓存 {{ number(review.pipeline.stages?.[key]?.cached_input_tokens) }}） · 输出 {{ number(review.pipeline.stages?.[key]?.output_tokens) }}（推理 {{ number(review.pipeline.stages?.[key]?.reasoning_output_tokens) }}） tokens</span></div>
      </template>
    </el-collapse-item></el-collapse>
    <ProductionWorkspace ref="productionWorkspace" :batch-id="state.batch?.id" :selected-event-ids="selectedEvents" :candidates="finalRows.map(row => ({ id: row.event.id, title: itemByRef.get(row.refs[0])?.title }))" @created="selectedEvents = []" />
    <el-drawer v-model="detailOpen" class="candidate-drawer" title="候选依据" size="560px">
      <template v-if="detailRow">
        <div class="detail-rank">推荐第 {{ detailRow.event?.ranking?.rank ?? detailRow.event?.comparison?.rank }} 名 <span>{{ rowSources(detailRow) }}</span></div>
        <h2 class="detail-title">{{ itemByRef.get(detailRow.refs[0])?.title }}</h2>
        <section v-if="detailRow.event?.ranking" class="detail-section"><h3>推荐看点</h3><p>{{ detailRow.event.ranking.hook }}</p><h3>读者为什么参与</h3><p>{{ detailRow.event.ranking.participation }}</p><h3>为什么排在这里</h3><p>{{ readableReason(detailRow.event.ranking.reason) }}</p><p v-if="nextTitle(detailRow)" class="detail-next">下一名：{{ nextTitle(detailRow) }}</p><h3 v-if="detailRow.event.ranking.unknowns?.length">尚待核实</h3><ul><li v-for="unknown in detailRow.event.ranking.unknowns" :key="unknown">{{ unknown }}</li></ul></section>
        <section class="detail-section"><h3>原入选理由</h3><p>{{ conclusion(detailRow) }}</p><div class="detail-tags">{{ mainRules(detailRow).join(' · ') }}</div></section>
        <section class="detail-section"><h3>来源与原句</h3><div v-for="(material, index) in detailRow.event?.materials || []" :key="index" class="detail-source"><a v-if="safeLink(material.final_url || material.url)" :href="safeLink(material.final_url || material.url)" target="_blank" rel="noopener noreferrer">{{ material.title || material.url }}</a><p class="muted">{{ material.publisher }} · {{ material.published_at ? date(material.published_at) : '发布时间未知' }} · {{ material.status === 'read' ? '已读取' : '读取失败' }}</p><blockquote v-for="fact in sourceFacts(detailRow, material)" :key="fact.quote">{{ fact.quote }}</blockquote><p v-if="material.error" class="muted">{{ material.error }}</p><details v-if="material.content"><summary>查看完整正文</summary><p class="reason">{{ material.content }}</p></details></div></section>
      </template>
    </el-drawer>
    <el-drawer v-model="selectionOpen" class="candidate-drawer" title="筛选与排名依据" size="560px">
      <section class="detail-section"><h3>为什么是这 {{ finalRows.length }} 个</h3><p class="selection-funnel">{{ state.items.length }} 条榜单信息 → {{ initial.length }} 条初筛保留 → {{ reviewRows.length }} 个事件 → {{ reviewedRows.length }} 个已复筛 → {{ passedRows.length }} 个复筛保留 → {{ finalRows.length }} 个最终候选</p><p>初筛按八项观众动机判断；复筛检查正文能否支持具体看点；最终比较独立吸引点与证据，最多保留 8 个。</p><p v-if="deferredRows.length">另有 {{ deferredRows.length }} 个事件未进入取材，尚未完成正文比较。</p></section>
      <section class="detail-section"><h3>排名怎么判断</h3><p>先比较吸引力与参与空间，再看时效和独立看点；相近时考虑整理成本。点击每个候选查看与下一名的具体比较。</p><p class="muted">{{ ranked ? `排名更新于 ${date(review.ranking.created_at)}` : '当前显示原筛选顺序' }}。按来源筛选不改变全批名次。</p></section>
      <section class="detail-section"><h3>原筛选未入选记录</h3><div v-for="row in notSelectedRows" :key="row.key" class="selection-rejection"><strong>{{ itemByRef.get(row.refs[0])?.title }}</strong><p>{{ readableReason(row.event.comparison.reason) }}</p></div></section>
    </el-drawer>
  </main>
</template>
