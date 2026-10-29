<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import RichEditor from './RichEditor.vue'

const props = defineProps({ batchId: String, selectedEventIds: { type: Array, default: () => [] }, candidates: { type: Array, default: () => [] } })
const emit = defineEmits(['created'])
const data = ref({ jobs: [], accounts: [], capabilities: {} })
const tab = ref('jobs')
const error = ref('')
const notice = ref('')
const pending = ref(false)
const createOpen = ref(false)
const targets = ref([])
const selectedJobs = ref([])
const confirmOpen = ref(false)
const confirmationJobs = ref([])
const reviewOpen = ref(false)
const reviewingId = ref(null)
const edit = ref({ title: '', summary: '', body_html: '', theme: 'simple', work_statements: null, first_publish: null, original: null })
const editRevision = ref(null)
const targetAccount = ref('')
const richEditor = ref(null)
const previewHtml = ref('')
const previewError = ref('')
let previewTimer
let previewSequence = 0
const accountForm = ref({ platform: 'wechat', display_name: '', mode: 'browser' })
let timer
let disposed = false
const currentJob = computed(() => data.value.jobs.find(job => job.id === reviewingId.value))
const selected = computed(() => data.value.jobs.filter(job => selectedJobs.value.includes(job.id)))
const modified = computed(() => !!currentJob.value && JSON.stringify(edit.value) !== JSON.stringify(editFromJob(currentJob.value)))
const targetModified = computed(() => !!currentJob.value && targetAccount.value !== currentJob.value.target?.account_id)
const eligibleAccounts = computed(() => data.value.accounts.filter(account => account.platform === currentJob.value?.target?.platform))
const stageLabels = { queued: '排队中', researching: '补充资料', writing: '写作中', editing: '编辑中', checking: '核验中', formatting: '排版中', ready_review: '待审稿', syncing: '同步草稿中', remote_verified: '平台草稿已核对', publishing: '提交中', submitted: '已提交', reviewing: '平台审核中', published: '已发布', needs_login: '需要登录', needs_attention: '需要处理', failed: '失败', cancelled: '已取消', outcome_unknown: '结果未知，待核验', blocked: '受阻' }
const platformLabels = { wechat: '公众号', toutiao: '今日头条' }
const accountStatuses = { needs_login: '需要登录', starting: '正在连接', connected: '已连接', needs_attention: '需要处理', failed: '连接失败' }
const capabilityLabels = { connect: '连接账号', save_draft: '同步草稿', inspect_draft: '回读草稿', publish: '发布', verify: '核验结果' }
const accountModes = { browser: '现有浏览器专用窗口', profile: '独立浏览器资料' }
function status(job) {
  const s = job.status === 'failed' || job.status === 'outcome_unknown' || job.status === 'needs_login' || job.status === 'blocked' ? job.status : job.stage || job.status
  return stageLabels[s] || `未知状态（${s || '未提供'}）`
}
function platform(value) { return platformLabels[value] || value || '未知平台' }
function jobAccount(job) { return data.value.accounts.find(a => a.id === job.target?.account_id) }
function accountName(job) { return jobAccount(job)?.display_name || job.packet?.account?.display_name || job.target?.account_id || '未知账号' }
function verification(account, feature) { return ({ verified: '已实机验证', not_verified: '代码已接入，未实机验证', not_implemented: '未接入' })[account.verification?.[feature]] || '验收状态未提供' }
function editFromJob(job) { const draft = job.draft || {}; return { title: draft.title || '', summary: draft.summary || '', body_html: draft.body_html || draft.html || '', theme: draft.theme || 'simple', work_statements: Array.isArray(draft.work_statements) ? [...draft.work_statements] : null, first_publish: draft.first_publish ?? null, original: draft.original ?? null } }
function hasCapability(job, feature) { return jobAccount(job)?.capabilities?.[feature] === true }
function capabilityReason(job, feature) { const account = jobAccount(job); return account?.capabilities?.[feature] === true ? '' : account?.capability_reason || (account ? '该账号未提供此操作的可用能力。' : '未找到目标账号，无法确认平台能力。') }
function titleLimitError(job) {
  const caps = jobAccount(job)?.capabilities || {}; const length = (job.draft?.title || '').trim().length
  if ((Number.isInteger(caps.title_min) && length < caps.title_min) || (Number.isInteger(caps.title_max) && length > caps.title_max)) return `标题须为 ${caps.title_min ?? 0}–${caps.title_max ?? '不限'} 字，当前 ${length} 字，请修改后再同步。`
  return ''
}
function statementError(job) { if (job.target?.platform !== 'toutiao') return ''; const draft = job.draft || {}; return !Array.isArray(draft.work_statements) || draft.first_publish !== false || draft.original !== false ? '请先审阅并保存作品声明，确认首发和原创处于关闭状态。' : '' }
function safeLink(url) { try { const p = new URL(url); return ['http:', 'https:'].includes(p.protocol) && !p.username && !p.password ? p.href : null } catch { return null } }
function coverUrl(url) {
  if (typeof url !== 'string') return null
  if (/^\/api\/production\/media\/[A-Za-z0-9_-]+\.png$/.test(url)) return url
  return /^\/media\/[A-Za-z0-9_-]+\.png$/.test(url) ? `/api/production${url}` : null
}
function printable(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
function canEdit(job) { return !!job && !job.pending_operation && !job.operation_uncertain && (['ready_review', 'remote_verified', 'needs_attention'].includes(job.stage) || (job.stage === 'failed' && job.external_operation === 'publish' && job.remote?.status === 'failed')) && job.status !== 'outcome_unknown' && !['submitted', 'reviewing', 'published', 'outcome_unknown'].includes(job.remote?.status) }
function canSync(job) { return !titleLimitError(job) && !statementError(job) && hasCapability(job, 'save_draft') && canEdit(job) && ['ready_review', 'remote_verified', 'needs_attention'].includes(job.stage) && !['unknown', 'outcome_unknown', 'submitted', 'reviewing', 'published'].includes(job.remote?.status) && !(job.stage === 'needs_attention' && job.remote?.status === 'saved') }
function canPublish(job) { return !job.pending_operation && !job.operation_uncertain && hasCapability(job, 'publish') && hasCapability(job, 'inspect_draft') && job.stage === 'remote_verified' && !!job.content_hash && job.content_hash === job.remote?.verified_hash && !!job.remote?.verified_identity && !!job.remote?.fingerprint && !!job.remote?.draft_id }
function verifyCapability(job) { return job.external_operation !== 'publish' && job.status !== 'outcome_unknown' && !job.operation_uncertain && job.remote?.draft_id && ['saved', 'draft_verified'].includes(job.remote?.status) ? 'inspect_draft' : 'verify' }
function canVerify(job) { return !job.pending_operation && (!!job.remote || job.status === 'outcome_unknown' || job.operation_uncertain) && hasCapability(job, verifyCapability(job)) }
function unavailableReason(job) {
  if (canEdit(job) && titleLimitError(job)) return titleLimitError(job)
  if (canEdit(job) && statementError(job)) return statementError(job)
  if (job.stage === 'remote_verified' && !hasCapability(job, 'publish')) return capabilityReason(job, 'publish')
  if ((job.remote || job.status === 'outcome_unknown') && !hasCapability(job, verifyCapability(job)) && (job.stage === 'needs_attention' || job.status === 'outcome_unknown' || ['submitted', 'reviewing'].includes(job.stage))) return capabilityReason(job, verifyCapability(job))
  if (canEdit(job) && !hasCapability(job, 'save_draft')) return capabilityReason(job, 'save_draft')
  return ''
}
function canRetry(job) { return job.status === 'failed' && ['queued', 'researching', 'writing', 'editing', 'checking', 'formatting'].includes(job.failed_stage) }
function canCancel(job) { return !job.pending_operation && !job.operation_uncertain && !['syncing', 'publishing', 'cancelled'].includes(job.stage) && job.status !== 'outcome_unknown' && !['submitted', 'reviewing', 'published', 'outcome_unknown'].includes(job.remote?.status) }
function previewDocument(html, mode) { const native = mode === 'toutiao_native' ? 'body{font-size:17px;line-height:1.75;color:#222}p{margin:20px 0}' : ''; return `<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}${native}</style></head><body>${html || ''}</body></html>` }
async function request(path, body, method = 'POST') {
  const response = await fetch(path, body === undefined ? undefined : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : `请求失败（${response.status}）`)
  return result
}
async function refresh() {
  try {
    const result = await request('/api/production')
    data.value = { jobs: result.jobs || [], accounts: result.accounts || [], capabilities: result.capabilities || {} }
    error.value = ''
  } catch (e) { error.value = `读取创作状态失败：${e.message}` }
}
async function poll() { await refresh(); if (!disposed) timer = window.setTimeout(poll, 3000) }
onMounted(poll)
onBeforeUnmount(() => { disposed = true; window.clearTimeout(timer); window.clearTimeout(previewTimer) })
watch(() => [edit.value.body_html, edit.value.theme, currentJob.value?.target?.platform, currentJob.value?.draft?.presentation_mode, reviewOpen.value], () => {
  window.clearTimeout(previewTimer)
  const sequence = ++previewSequence
  if (!reviewOpen.value || !currentJob.value) return
  previewTimer = window.setTimeout(async () => {
    try { const result = await request('/api/production/preview', { platform: currentJob.value.target.platform, theme: edit.value.theme, body_html: edit.value.body_html, presentation_mode: currentJob.value.draft?.presentation_mode || null }); if (sequence === previewSequence) { previewHtml.value = result.html; previewError.value = '' } }
    catch (e) { if (sequence === previewSequence) { previewError.value = `预览未更新：${e.message}`; previewHtml.value = '' } }
  }, 350)
})
function reportResults(result, label) {
  const rows = result.results || []
  const failures = rows.filter(row => !row.ok)
  notice.value = `${label}：${rows.length - failures.length} 项已受理${failures.length ? `，${failures.length} 项未受理：${failures.map(row => `${row.job_id || row.event_id || ''} ${row.error || '原因未提供'}`).join('；')}` : ''}`
  return !failures.length
}
function openCreate() { targets.value = []; createOpen.value = true }
defineExpose({ openCreate })
async function createJobs() {
  pending.value = true; notice.value = ''
  try {
    const result = await request('/api/production/jobs', { batch_id: props.batchId, event_ids: props.selectedEventIds, targets: data.value.accounts.filter(a => targets.value.includes(a.id)).map(a => ({ platform: a.platform, account_id: a.id })) })
    if (reportResults(result, '创建任务')) { createOpen.value = false; emit('created') }
    tab.value = 'jobs'; await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function runAction(action, jobs) {
  if (!jobs.length) return
  pending.value = true; notice.value = ''
  try {
    const body = { job_ids: jobs.map(j => j.id), action, expected_revisions: Object.fromEntries(jobs.map(j => [j.id, j.revision])) }
    if (action === 'confirm_publish') body.expected_approvals = Object.fromEntries(jobs.map(j => [j.id, { revision: j.revision, content_hash: j.content_hash, account_id: j.target.account_id, account_identity: j.remote.verified_identity, draft_id: j.remote.draft_id, remote_fingerprint: j.remote.fingerprint }]))
    const result = await request('/api/production/actions', body)
    reportResults(result, ({ sync: '同步草稿', confirm_publish: '确认并发布', verify: '核验', retry: '重试', cancel: '取消', redo_writing: '重做写作', redo_editing: '重做编辑', redo_formatting: '重做排版' })[action])
    selectedJobs.value = []; confirmOpen.value = false; await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
function openConfirmation(jobs) { confirmationJobs.value = jobs.map(job => JSON.parse(JSON.stringify(job))); confirmOpen.value = true }
function openReview(job) { reviewingId.value = job.id; edit.value = editFromJob(job); targetAccount.value = job.target?.account_id || ''; editRevision.value = job.revision; previewHtml.value = job.draft?.html || ''; reviewOpen.value = true }
async function saveTarget() {
  pending.value = true; notice.value = ''
  try { const job = await request(`/api/production/jobs/${encodeURIComponent(reviewingId.value)}`, { changes: { target_account_id: targetAccount.value }, expected_revision: editRevision.value }, 'PATCH'); editRevision.value = job.revision; targetAccount.value = job.target?.account_id || ''; edit.value = editFromJob(job); notice.value = '目标账号已更新，请重新同步并确认当前版本。'; await refresh() }
  catch (e) { notice.value = `修改目标账号失败：${e.message}` } finally { pending.value = false }
}
async function saveEdit() {
  pending.value = true; notice.value = ''
  try { const current = editFromJob(currentJob.value); const changes = Object.fromEntries(Object.entries(edit.value).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(current[key]))); const job = await request(`/api/production/jobs/${encodeURIComponent(reviewingId.value)}`, { changes, expected_revision: editRevision.value }, 'PATCH'); editRevision.value = job.revision; edit.value = editFromJob(job); notice.value = '已保存当前稿件；实质修改会撤销原发布确认，请重新同步草稿。'; await refresh() }
  catch (e) { notice.value = `保存失败：${e.message}` } finally { pending.value = false }
}
async function uploadCover(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (!file) return
  if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10000000) { notice.value = '封面仅支持 10 MB 以内的 PNG 或 JPEG。'; return }
  if (modified.value || targetModified.value) { notice.value = '请先保存正文或目标账号修改，再替换封面。'; return }
  pending.value = true; notice.value = ''
  try {
    const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('读取图片失败')); reader.readAsDataURL(file) })
    const job = await request(`/api/production/jobs/${encodeURIComponent(reviewingId.value)}/cover`, { filename: file.name, content_type: file.type, content_base64: encoded, expected_revision: editRevision.value })
    editRevision.value = job.revision; notice.value = '封面已替换，请重新同步草稿。'; await refresh()
  } catch (e) { notice.value = `封面替换失败：${e.message}` } finally { pending.value = false }
}
async function uploadBodyImage(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (!file) return
  if (targetModified.value) { notice.value = '请先保存目标账号修改，再插入配图。'; return }
  if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10000000) { notice.value = '配图仅支持 10 MB 以内的 PNG 或 JPEG。'; return }
  pending.value = true; notice.value = ''
  try {
    const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('读取图片失败')); reader.readAsDataURL(file) })
    const result = await request(`/api/production/jobs/${encodeURIComponent(reviewingId.value)}/body-image`, { filename: file.name, content_type: file.type, content_base64: encoded, expected_revision: editRevision.value })
    if (!coverUrl(result.url)) throw new Error('配图地址无效')
    richEditor.value?.insertFigure(coverUrl(result.url)); notice.value = '配图已插入正文，请编辑图注并保存稿件。'
  } catch (e) { notice.value = `配图上传失败：${e.message}` } finally { pending.value = false }
}
async function addAccount() {
  pending.value = true; notice.value = ''
  try { await request('/api/production/accounts', accountForm.value); accountForm.value.display_name = ''; notice.value = '账号已添加，请连接并检查实际登录状态。'; await refresh() }
  catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function connect(account) {
  pending.value = true; notice.value = ''
  try { const result = await request(`/api/production/accounts/${encodeURIComponent(account.id)}/connect`, {}); notice.value = result.message || '已发起连接，请查看账号状态。'; await refresh() }
  catch (e) { notice.value = `连接失败：${e.message}` } finally { pending.value = false }
}
</script>

<template>
  <section class="production" aria-label="创作与发布">
    <div class="production-heading"><div><p class="eyebrow">CONTENT PRODUCTION</p><h2>创作与发布</h2><p>每篇稿件独立推进，审阅当前版本并核对平台草稿后再发布。</p></div><el-radio-group v-model="tab" aria-label="生产工作区"><el-radio-button value="jobs">稿件任务 {{ data.jobs.length }}</el-radio-button><el-radio-button value="accounts">账号 {{ data.accounts.length }}</el-radio-button></el-radio-group></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-alert v-if="notice" class="production-notice" :title="notice" type="info" :closable="true" @close="notice = ''" show-icon />
    <template v-if="tab === 'jobs'">
      <div v-if="data.jobs.length" class="production-bar"><span>已选 {{ selected.length }} 项 · 操作仅应用于符合当前状态的任务</span><div class="production-actions"><el-button size="small" :disabled="pending || !selected.some(canSync)" @click="runAction('sync', selected.filter(canSync))">同步草稿 {{ selected.filter(canSync).length }}</el-button><el-button size="small" :disabled="pending || !selected.some(canVerify)" @click="runAction('verify', selected.filter(canVerify))">核验结果 {{ selected.filter(canVerify).length }}</el-button><el-button size="small" :disabled="pending || !selected.some(canRetry)" @click="runAction('retry', selected.filter(canRetry))">重试 {{ selected.filter(canRetry).length }}</el-button><el-button size="small" :disabled="pending || !selected.some(canCancel)" @click="runAction('cancel', selected.filter(canCancel))">取消任务 {{ selected.filter(canCancel).length }}</el-button><el-button size="small" type="primary" :disabled="pending || !selected.some(canPublish)" @click="openConfirmation(selected.filter(canPublish))">确认并发布 {{ selected.filter(canPublish).length }}</el-button></div></div>
      <div v-if="data.jobs.length" class="job-list">
        <article v-for="job in data.jobs" :key="job.id" class="job-row">
          <el-checkbox v-model="selectedJobs" :value="job.id" :aria-label="`选择 ${job.draft?.title || job.source_title}`" />
          <div class="job-main"><button class="job-title" @click="openReview(job)">{{ job.draft?.title || job.source_title || '标题尚未生成' }}</button><p>{{ platform(job.target?.platform) }} · {{ accountName(job) }} <span>· 版本 {{ job.revision }}</span></p><p v-if="job.error || job.remote?.message" class="job-error">{{ job.error || job.remote.message }}</p><p v-if="unavailableReason(job)">{{ unavailableReason(job) }}</p></div>
          <div class="job-state"><span class="status-dot" :class="{ attention: job.error || job.status === 'outcome_unknown', complete: job.stage === 'published' }"></span>{{ status(job) }}</div>
          <div class="job-row-actions"><el-button link type="primary" @click="openReview(job)">查看稿件</el-button><el-button v-if="canSync(job)" link @click="runAction('sync', [job])">同步草稿</el-button><el-button v-if="canVerify(job)" link @click="runAction('verify', [job])">核验</el-button><el-button v-if="canRetry(job)" link @click="runAction('retry', [job])">重试</el-button></div>
        </article>
      </div>
      <div v-else class="production-empty"><strong>还没有创作任务</strong><p>在上方最终候选中选择选题，再选择目标账号开始创作。</p></div>
    </template>
    <template v-else>
      <div class="account-intro">账号连接状态由应用实际检查返回；浏览器已登录不代表此处的连接已就绪。</div>
      <div class="account-list"><article v-for="account in data.accounts" :key="account.id" class="account-card"><div><span class="account-platform">{{ platform(account.platform) }}</span><h3>{{ account.display_name || account.id }}</h3><p>连接方式：{{ accountModes[account.mode] || (account.mode ? `未知方式（${account.mode}）` : '未提供') }}</p><p>连接状态：{{ accountStatuses[account.status] || (account.status ? `未知状态（${account.status}）` : '未提供') }}</p><p v-if="account.identity?.name">平台身份：{{ account.identity.name }}</p><p v-if="account.message">{{ account.message }}</p><p v-if="account.capability_reason">{{ account.capability_reason }}</p><div v-if="account.capabilities" class="account-capabilities"><span v-for="(label, feature) in capabilityLabels" :key="feature">{{ label }}：{{ account.capabilities[feature] === true ? verification(account, feature) : account.capabilities[feature] === false ? '未接入' : '未知' }}</span></div><p v-else>未提供账号能力。</p><p v-if="account.capabilities?.supports_summary === false">摘要仅供本地审稿，不会同步到平台。</p><p v-if="Number.isInteger(account.capabilities?.title_min) || Number.isInteger(account.capabilities?.title_max)">标题限制：{{ account.capabilities.title_min ?? 0 }}–{{ account.capabilities.title_max ?? '不限' }} 字</p></div><el-button :disabled="pending || account.capabilities?.connect === false" @click="connect(account)">连接 / 登录</el-button></article></div>
      <p v-if="!data.accounts.length" class="muted">尚无账号，请先添加。</p>
      <div class="account-add"><el-select v-model="accountForm.platform" aria-label="平台"><el-option label="公众号" value="wechat" /><el-option label="今日头条" value="toutiao" /></el-select><el-input v-model="accountForm.display_name" placeholder="账号名称" aria-label="账号名称" /><el-select v-model="accountForm.mode" aria-label="连接方式"><el-option label="现有浏览器专用窗口" value="browser" /><el-option label="独立浏览器资料" value="profile" /></el-select><el-button type="primary" :disabled="pending || !accountForm.display_name.trim()" @click="addAccount">添加账号</el-button></div><p class="muted">默认连接现有浏览器专用窗口；也可选择独立浏览器资料，按连接状态完成登录。</p>
      <details class="capabilities"><summary>查看平台汇总能力</summary><p>具体操作能力以各账号所显示的连接方式和能力为准。</p><div v-for="(features, name) in data.capabilities" :key="name" class="capability-line"><strong>{{ platform(name) }}</strong><span v-for="(available, feature) in features" :key="feature">{{ capabilityLabels[feature] || feature }}：{{ available === true ? '已接入' : available === false ? '未接入' : '未知' }}</span></div><p v-if="!Object.keys(data.capabilities).length">平台未返回能力信息。</p></details>
    </template>
  </section>

  <el-dialog v-model="createOpen" title="批量创作" width="600px" class="production-dialog"><el-alert v-if="notice" :title="notice" type="info" :closable="false" /><p>已选择 {{ selectedEventIds.length }} 个选题。每个选题与目标账号分别创建任务。</p><ul class="create-list"><li v-for="candidate in candidates.filter(c => selectedEventIds.includes(c.id))" :key="candidate.id">{{ candidate.title }}</li></ul><p class="muted">选择目标平台账号。创作不代表授权发布。</p><el-checkbox-group v-model="targets" class="target-list"><el-checkbox v-for="account in data.accounts" :key="account.id" :value="account.id">{{ platform(account.platform) }} · {{ account.display_name || account.id }}（{{ accountStatuses[account.status] || (account.status ? `未知状态（${account.status}）` : '状态未提供') }}）</el-checkbox></el-checkbox-group><p v-if="!data.accounts.length" class="muted">请先在“账号”中添加账号。</p><template #footer><el-button @click="createOpen = false">取消</el-button><el-button type="primary" :loading="pending" :disabled="!targets.length || !selectedEventIds.length" @click="createJobs">创建后台任务</el-button></template></el-dialog>

  <el-drawer v-model="reviewOpen" title="审阅稿件" size="min(1200px, 100%)" class="production-drawer">
    <template v-if="currentJob"><div class="review-heading"><div><span class="account-platform">{{ platform(currentJob.target?.platform) }} · {{ accountName(currentJob) }}</span><h2>{{ currentJob.draft?.title || currentJob.source_title }}</h2><p>版本 {{ editRevision }} · {{ status(currentJob) }} <span v-if="currentJob.revision !== editRevision" class="job-error">· 版本已变化，请关闭后重新打开</span></p></div><div><el-button :disabled="!canEdit(currentJob) || pending || !modified || targetModified || currentJob.revision !== editRevision" @click="saveEdit">保存编辑</el-button><el-button v-if="canSync(currentJob)" type="primary" :disabled="modified || targetModified || pending || currentJob.revision !== editRevision" @click="runAction('sync', [currentJob])">同步草稿</el-button><el-button v-if="canPublish(currentJob)" type="primary" :disabled="modified || targetModified || pending || currentJob.revision !== editRevision" @click="openConfirmation([currentJob])">确认并发布</el-button></div></div>
      <el-alert v-if="notice" :title="notice" type="info" :closable="false" />
      <el-alert v-if="currentJob.draft?.fact_check_scope" class="fact-check-scope" :title="currentJob.draft.fact_check_scope" type="warning" :closable="false" show-icon />
      <el-alert v-if="jobAccount(currentJob)?.capabilities?.supports_summary === false" class="fact-check-scope" title="摘要仅供本地审稿，不会同步到目标平台。" type="info" :closable="false" show-icon />
      <el-alert v-if="titleLimitError({ ...currentJob, draft: { ...currentJob.draft, title: edit.title } })" class="fact-check-scope" :title="titleLimitError({ ...currentJob, draft: { ...currentJob.draft, title: edit.title } })" type="warning" :closable="false" show-icon />
      <section class="target-account"><label for="review-target-account">目标账号</label><el-select id="review-target-account" v-model="targetAccount" :disabled="!canEdit(currentJob) || pending || modified || currentJob.revision !== editRevision" aria-label="目标账号"><el-option v-for="account in eligibleAccounts" :key="account.id" :value="account.id" :label="`${account.display_name || account.id} · ${accountStatuses[account.status] || account.status || '状态未提供'}`" /></el-select><el-button :disabled="!targetModified || !canEdit(currentJob) || pending || modified || currentJob.revision !== editRevision" @click="saveTarget">保存目标账号</el-button><p class="muted">仅可选择同平台账号。更换后须重新同步并确认；旧账号中的平台草稿不会删除，切回后可能产生新草稿。请先保存正文修改。</p></section>
      <div class="review-grid"><div class="editor-pane"><label>标题<el-input v-model="edit.title" :disabled="!canEdit(currentJob) || targetModified" /></label><label>摘要<el-input v-model="edit.summary" type="textarea" :rows="3" :disabled="!canEdit(currentJob) || targetModified" /></label><label v-if="currentJob.target?.platform === 'wechat'">公众号排版主题<el-select v-model="edit.theme" :disabled="!canEdit(currentJob) || targetModified"><el-option label="简洁" value="simple" /><el-option label="默认" value="default" /></el-select></label><div class="editor-label">正文</div><RichEditor ref="richEditor" v-model="edit.body_html" :disabled="!canEdit(currentJob) || pending || targetModified" :platform="currentJob.target?.platform" :presentation-mode="currentJob.draft?.presentation_mode" @image="uploadBodyImage" /><p class="muted">保存实质修改、切换主题或替换封面后，原发布确认失效，需重新同步并核对。</p><div class="cover-control"><img v-if="coverUrl(currentJob.draft?.cover_url)" :src="coverUrl(currentJob.draft.cover_url)" alt="当前封面" /><label v-if="canEdit(currentJob)" class="cover-upload">替换封面<input type="file" accept="image/png,image/jpeg" :disabled="pending" @change="uploadCover" /></label></div>
        <section v-if="currentJob.target?.platform === 'toutiao'" class="statement-editor"><h3>作品声明</h3><p class="muted">仅按已知事实选择。“取材网络”和“引用 AI”能否同时在平台生效尚未实机验证，须通过回读核对；首发和原创目前仅支持关闭。</p><div v-if="edit.work_statements === null"><span>来源与 AI 声明尚未确认。</span><el-button link :disabled="!canEdit(currentJob) || targetModified" @click="edit.work_statements = []">选择声明</el-button></div><el-checkbox-group v-else v-model="edit.work_statements" :disabled="!canEdit(currentJob) || targetModified"><el-checkbox value="取材网络">取材网络</el-checkbox><el-checkbox value="引用AI">引用 AI</el-checkbox></el-checkbox-group><div>头条首发：<el-radio-group v-model="edit.first_publish" :disabled="!canEdit(currentJob) || targetModified"><el-radio :value="false">关闭</el-radio><el-radio :value="true" disabled>开启（尚未支持）</el-radio></el-radio-group><span v-if="edit.first_publish === null" class="muted">尚未确认</span></div><div>原创：<el-radio-group v-model="edit.original" :disabled="!canEdit(currentJob) || targetModified"><el-radio :value="false">关闭</el-radio><el-radio :value="true" disabled>开启（尚未支持）</el-radio></el-radio-group><span v-if="edit.original === null" class="muted">尚未确认</span></div></section>
      </div><div class="preview-pane"><div class="preview-caption">手机宽度预览 <span>服务端排版，以平台实际回读为准</span></div><article class="phone-preview"><h1>{{ edit.title || '标题' }}</h1><p v-if="jobAccount(currentJob)?.capabilities?.supports_summary !== false" class="preview-summary">{{ edit.summary }}</p><img v-if="coverUrl(currentJob.draft?.cover_url)" :src="coverUrl(currentJob.draft.cover_url)" alt="封面" /><p v-if="previewError" class="job-error">{{ previewError }}</p><iframe v-else class="body-preview" title="正文排版预览" sandbox :srcdoc="previewDocument(previewHtml, currentJob.draft?.presentation_mode)"></iframe></article></div></div>
      <div class="review-more"><details><summary>原标题与最终标题理由</summary><p>原标题：{{ currentJob.source_title }}</p><p>最终标题：{{ currentJob.draft?.title }}</p><pre>{{ printable(currentJob.draft?.title_review || '尚未提供') }}</pre></details><details><summary>来源、核验与编辑说明</summary><pre>{{ printable({ sources: currentJob.draft?.sources, fact_checks: currentJob.draft?.fact_checks, unresolved: currentJob.draft?.unresolved, editorial_notes: currentJob.draft?.editorial_notes }) }}</pre></details><details><summary>操作记录</summary><pre>{{ printable(currentJob.history || []) }}</pre></details><div class="review-links"><a v-if="safeLink(currentJob.remote?.url)" :href="safeLink(currentJob.remote.url)" target="_blank" rel="noopener noreferrer">打开平台草稿或预览</a></div><div v-if="canEdit(currentJob)" class="production-actions"><el-button size="small" :disabled="pending || modified || targetModified" @click="runAction('redo_writing', [currentJob])">重做写作</el-button><el-button size="small" :disabled="pending || modified || targetModified" @click="runAction('redo_editing', [currentJob])">重做编辑</el-button><el-button size="small" :disabled="pending || modified || targetModified" @click="runAction('redo_formatting', [currentJob])">重做排版</el-button></div></div>
    </template>
  </el-drawer>

  <el-dialog v-model="confirmOpen" title="确认以下版本并发布" width="min(900px, 94vw)" class="production-dialog"><p>请核对每篇完整内容、版本和目标账号。确认后将提交平台，最终结果以核验状态为准。</p><div class="confirm-list"><article v-for="job in confirmationJobs" :key="job.id" class="confirm-card"><div class="confirm-meta">{{ platform(job.target?.platform) }} · {{ accountName(job) }} · 版本 {{ job.revision }}</div><p class="confirm-identity">已核验平台账号：{{ job.remote?.verified_identity }}<br>平台草稿：{{ job.remote?.draft_id }}<br>内容指纹：{{ job.content_hash }}<br>草稿指纹：{{ job.remote?.fingerprint }}</p><h3>{{ job.draft?.title }}</h3><p v-if="jobAccount(job)?.capabilities?.supports_summary === false" class="confirm-meta">以下摘要仅供本地审稿，不会同步到平台</p><p>{{ job.draft?.summary }}</p><img v-if="coverUrl(job.draft?.cover_url)" :src="coverUrl(job.draft.cover_url)" alt="封面" /><p v-if="job.target?.platform === 'toutiao'" class="confirm-identity">作品声明：{{ job.draft?.work_statements === null || job.draft?.work_statements === undefined ? '尚未确认' : (job.draft.work_statements.join('、') || '无') }}<br>头条首发：{{ job.draft?.first_publish === false ? '关闭' : job.draft?.first_publish === true ? '开启' : '尚未确认' }} · 原创：{{ job.draft?.original === false ? '关闭' : job.draft?.original === true ? '开启' : '尚未确认' }}</p><iframe class="body-preview confirm-body" title="确认正文排版" sandbox :srcdoc="previewDocument(job.draft?.html, job.draft?.presentation_mode)"></iframe></article></div><template #footer><el-button @click="confirmOpen = false">返回审阅</el-button><el-button type="primary" :loading="pending" @click="runAction('confirm_publish', confirmationJobs)">确认并发布 {{ confirmationJobs.length }} 篇</el-button></template></el-dialog>
</template>
