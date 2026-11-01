<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { api, isPrototype } from './api'
import Icon from './Icon.vue'
import { creation } from './workflow'
import { sourceLinks, hotlistLinks } from './sources'
import RichEditor from './RichEditor.vue'
import ArticleBody from './ArticleBody.vue'

const route = useRoute()
const router = useRouter()
const data = ref({ jobs: [], accounts: [] })
const error = ref('')
const notice = ref('')
const pending = ref(false)
const loading = ref(true)
const targets = computed({ get: () => creation.targets || [], set: value => { creation.targets=value } })
const selectedJobs = ref([])
const filter = ref('all')
const columnFilter = ref('')
const workColumns = computed(()=>[...new Map((data.value.works||[]).map(w=>[w.line_id,{id:w.line_id,name:w.line_name}])).values()])
const confirmOpen = ref(false)
const confirmationJobs = ref([])
const accountOpen = ref(false)
const accountForm = ref({ platform: 'wechat', display_name: '', mode: 'browser' })
const titleOpen = ref(false)
const title = ref('')
const titleRevision = ref(null)
const bodyOpen = ref(false)
const body = ref('')
const bodyRevision = ref(null)
const bodyOriginal = ref('')
const titleOriginal = ref('')
const dirty = computed(()=>bodyOpen.value && (body.value!==bodyOriginal.value || title.value!==titleOriginal.value))
const richEditor = ref(null)
const entriesOpen = ref(false)
const entries = ref([])
const entriesRevision = ref(null)
const comparing = ref(false)
const originals = ref([])
const originalIndex = ref(0)
const sourceError = ref('')
const currentJob = computed(() => data.value.jobs.find(j => j.id === route.params.id))
const currentWork = computed(()=>(data.value.works||[]).find(w=>w.job_ids.includes(currentJob.value?.id)))
const workJobs = work => data.value.jobs.filter(j=>work?.job_ids.includes(j.id))
const visibleWorks = computed(()=>(data.value.works||[]).filter(w=>(!columnFilter.value||w.line_id===columnFilter.value)&&(filter.value==='all'||(filter.value==='saved'?workJobs(w).every(j=>['saved','draft_verified'].includes(j.platform_save?.status)):workJobs(w).some(j=>visibleJobs.value.includes(j))))))
const articleSources = computed(() => sourceLinks(currentJob.value?.draft?.sources || currentJob.value?.packet?.materials))
const hotlistSources = computed(() => hotlistLinks(currentJob.value?.packet?.items))
const original = computed(() => originals.value[originalIndex.value])
const originalParagraphs = computed(() => {
  const paragraphs = (original.value?.content || '').split(/\n+/).filter(p => p.trim())
  if (paragraphs[0]?.replace(/\s/g, '') === original.value?.title?.replace(/\s/g, '')) paragraphs.shift()
  return paragraphs
})
async function compareOriginal() {
  comparing.value = !comparing.value
  if (!comparing.value || originals.value.length) return
  try { originals.value = (await request(`/api/production/jobs/${currentJob.value.id}/sources`)).sources; sourceError.value = '' }
  catch (e) { sourceError.value = e.message }
}
function inlineRuns(node, bold = false) {
  if (node.nodeType === Node.TEXT_NODE) return [{ text: node.textContent, bold }]
  if (node.nodeType !== Node.ELEMENT_NODE || ['SCRIPT','STYLE','IFRAME','OBJECT'].includes(node.tagName)) return []
  const strong = bold || ['B','STRONG'].includes(node.tagName) || /^(bold|[6-9]00)$/.test(node.style.fontWeight)
  return [...node.childNodes].flatMap(child => inlineRuns(child, strong))
}
const selected = computed(() => data.value.jobs.filter(j => selectedJobs.value.includes(j.id)))
const creating = computed(() => route.path === '/drafts/new')
const accountsPage = computed(() => route.path === '/accounts')
const visibleJobs = computed(() => data.value.jobs.filter(j => filter.value === 'all' ||
  (filter.value === 'attention' ? !!j.error || ['failed','outcome_unknown','needs_attention','blocked','needs_login'].includes(j.status) :
  filter.value === 'saved' ? ['saved','draft_verified'].includes(j.platform_save?.status) : ['queued','researching','writing','editing','checking','formatting'].includes(j.stage))))
let timer
let disposed = false
const stageLabels = { queued: '排队中', researching: '补充资料', writing: '写作中', editing: '编辑中', checking: '核验中', formatting: '排版中', ready_review: '待审稿', syncing: '同步草稿中', resuming: '继续同步中', remote_verified: '平台草稿已核对', publishing: '提交中', submitted: '已提交', reviewing: '平台审核中', published: '已发布', needs_login: '需要登录', needs_attention: '需要处理', failed: '失败', cancelled: '已取消', outcome_unknown: '结果未知，待核验', blocked: '受阻' }
const platformLabels = { wechat: '公众号', toutiao: '今日头条' }
const accountStatuses = { needs_login: '需要登录', starting: '正在连接', connected: '已核验', needs_check: '待核验', needs_attention: '需要处理', failed: '连接失败' }
const capabilityLabels = { connect: '连接账号', save_draft: '同步草稿', inspect_draft: '回读草稿', publish: '发布', verify: '核验结果' }
const accountModes = { browser: '现有浏览器专用窗口', profile: '独立浏览器资料' }
function status(job) {
  if (['saved','draft_verified'].includes(job.platform_save?.status) && job.editorial_approved !== true) return '草稿已存，待重审'
  if (job.stage === 'ready_review') return job.error ? '待处理' : job.editorial_approved ? '待存草稿' : '待审稿'
  const s = job.status === 'failed' || job.status === 'outcome_unknown' || job.status === 'needs_login' || job.status === 'blocked' ? job.status : job.stage || job.status
  return stageLabels[s] || `未知状态（${s || '未提供'}）`
}
function platform(value) { return platformLabels[value] || value || '未知平台' }
function jobAccount(job) { return data.value.accounts.find(a => a.id === job.target?.account_id) }
function accountName(job) { return jobAccount(job)?.identity?.name || jobAccount(job)?.display_name || job.packet?.account?.display_name || job.target?.account_id || '未知账号' }
function verification(account, feature) { return ({ verified: '已实机验证', not_verified: '代码已接入，未实机验证', not_implemented: '未接入' })[account.verification?.[feature]] || '验收状态未提供' }
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
  if (isPrototype && /^\.\/prototype-media\/[A-Za-z0-9_.-]+$/.test(url)) return url
  if (/^\/api\/production\/media\/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg)$/.test(url)) return url
  return /^\/media\/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg)$/.test(url) ? `/api/production${url}` : null
}
function printable(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2) }
function canEdit(job) { return !!job && !job.pending_operation && !job.operation_uncertain && (['ready_review', 'remote_verified', 'needs_attention'].includes(job.stage) || (job.stage === 'failed' && job.external_operation === 'publish' && job.remote?.status === 'failed')) && job.status !== 'outcome_unknown' && !['submitted', 'reviewing', 'published', 'outcome_unknown'].includes(job.remote?.status) }
function canSync(job) { return !(job.stage === 'remote_verified' && job.content_hash === job.remote?.verified_hash) && job.editorial_approved === true && !titleLimitError(job) && !statementError(job) && hasCapability(job, 'save_draft') && canEdit(job) && ['ready_review', 'remote_verified', 'needs_attention'].includes(job.stage) && !['unknown', 'outcome_unknown', 'submitted', 'reviewing', 'published'].includes(job.remote?.status) && !(job.stage === 'needs_attention' && job.remote?.status === 'saved') }
function canPublish(job) { return job.editorial_approved === true && !job.remote?.publish_blockers?.length && !job.pending_operation && !job.operation_uncertain && hasCapability(job, 'publish') && hasCapability(job, 'inspect_draft') && job.stage === 'remote_verified' && !!job.content_hash && job.content_hash === job.remote?.verified_hash && !!job.remote?.verified_identity && !!job.remote?.fingerprint && !!job.remote?.draft_id }
function verifyCapability(job) { return job.external_operation !== 'publish' && job.remote?.draft_id && ['saved', 'draft_verified'].includes(job.remote?.status) ? 'inspect_draft' : 'verify' }
function canVerify(job) { return !job.pending_operation && (!!job.remote || job.status === 'outcome_unknown' || job.operation_uncertain) && hasCapability(job, verifyCapability(job)) }
function unavailableReason(job) {
  if (canEdit(job) && titleLimitError(job)) return titleLimitError(job)
  if (canEdit(job) && statementError(job)) return statementError(job)
  if (job.stage === 'remote_verified' && !hasCapability(job, 'publish')) return capabilityReason(job, 'publish')
  if ((job.remote || job.status === 'outcome_unknown') && !hasCapability(job, verifyCapability(job)) && (job.stage === 'needs_attention' || job.status === 'outcome_unknown' || ['submitted', 'reviewing'].includes(job.stage))) return capabilityReason(job, verifyCapability(job))
  if (canEdit(job) && !hasCapability(job, 'save_draft')) return capabilityReason(job, 'save_draft')
  return ''
}
function canResume(job) { return !job.pending_operation && job.status === 'outcome_unknown' && job.external_operation === 'sync' && !job.approval }
function canRetry(job) { return job.status === 'failed' && ['queued', 'researching', 'writing', 'editing', 'checking', 'formatting'].includes(job.failed_stage) }
function canCancel(job) { return !job.pending_operation && !job.operation_uncertain && !['syncing', 'publishing', 'cancelled'].includes(job.stage) && job.status !== 'outcome_unknown' && !['submitted', 'reviewing', 'published', 'outcome_unknown'].includes(job.remote?.status) }

function bodyBlocks(job) {
  const doc = new DOMParser().parseFromString(job?.draft?.body_html || job?.draft?.html || '', 'text/html')
  doc.querySelectorAll('script,style,iframe,object,template').forEach(node => node.remove())
  return [...doc.querySelectorAll('h1,h2,h3,p,li,figcaption,img')].filter(node =>
    (node.tagName === 'IMG' || !node.parentElement?.closest('p,li,figcaption')) && (node.tagName === 'IMG' || node.textContent.trim())
  ).map(node => ({ type: node.tagName === 'IMG' ? 'image' : /^H[1-3]$/.test(node.tagName) ? 'heading' : 'text',
    text: node.textContent.trim(), runs: inlineRuns(node), src: coverUrl(node.getAttribute('src')) }))
}
function draftLink(job) {
  if (safeLink(job?.remote?.url)) return safeLink(job.remote.url)
  if (job?.target?.platform === 'toutiao' && /^\d+$/.test(job?.remote?.draft_id || '')) return `https://mp.toutiao.com/profile_v4/graphic/publish?pgc_id=${job.remote.draft_id}`
  return null
}
function problem(job) {
  const error = job?.error || ''
  if (/Previous operation is still settling/.test(error)) return '浏览器上一操作尚未结束，草稿进度已保留。'
  if (/RPC timed out|code=timeout/.test(error)) return '平台页面响应超时，草稿进度已保留。'
  if (/observed content changed|coverage changed/.test(error)) return '平台页面发生变化，同步已暂停。'
  if (/element not visible/.test(error)) return '平台控件未就绪，同步已暂停。'
  return error
}
function publishReason(job) {return job.remote?.publish_blockers?.join('；') || unavailableReason(job) || (!canPublish(job)?'完成审稿及平台草稿核对后可发布':'')}
function primary(job) {
  if (canEdit(job) && job.editorial_approved !== true) return { action: 'review', label: '审稿' }
  if (job.stage === 'remote_verified' && job.content_hash === job.remote?.verified_hash) return null
  if (canResume(job)) return { action: 'resume', label: job.remote?.draft_id ? '继续更新' : '继续存稿' }
  if (canEdit(job) && job.editorial_approved !== true) return { action: 'review', label: '审稿' }
  if (canSync(job)) return { action: 'sync', label: job.remote?.draft_id ? '更新草稿' : '存草稿' }
  if (canRetry(job)) return { action: 'retry', label: '重试' }
  if (canVerify(job)) return { action: 'verify', label: '核验' }
  return null
}
async function request(path, body, method = 'POST') {
  return api(path,body,method)
}
async function refresh() {
  try { const result = await request('/api/production'); data.value = result; error.value = '' }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}
async function poll() { await refresh(); if (!disposed) timer = window.setTimeout(poll, 3000) }
function beforeUnload(event){if(dirty.value){event.preventDefault();event.returnValue=''}}
async function discard(){if(pending.value)return false;if(!dirty.value)return true;try{await ElMessageBox.confirm('放弃尚未保存的修改？','离开编辑',{confirmButtonText:'放弃修改',cancelButtonText:'继续编辑'});return true}catch{return false}}
async function closeEditor(){if(await discard()){bodyOpen.value=false;notice.value=''}}
onBeforeRouteLeave(discard)
onBeforeRouteUpdate(discard)
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);poll()})
onBeforeUnmount(() => { disposed = true; window.clearTimeout(timer);window.removeEventListener('beforeunload',beforeUnload) })
watch(() => route.fullPath, () => { notice.value = ''; titleOpen.value = false; bodyOpen.value = false; entriesOpen.value = false; selectedJobs.value = []; comparing.value = false; originals.value = []; originalIndex.value = 0 })
function report(result) {
  const rows = result.results || []
  const failures = rows.filter(row => !row.ok)
  notice.value = failures.length ? failures.map(row => row.error || '操作失败').join('；') : '已受理'
  return !failures.length
}
async function createJobs() {
  pending.value = true; notice.value = ''
  try {
    const result = await request('/api/production/jobs', { batch_id: creation.batchId, event_ids: creation.events.map(e => e.id), expected_versions: Object.fromEntries(creation.events.map(e => [e.id, e.version])),
      targets: data.value.accounts.filter(a => targets.value.includes(a.id)).map(a => ({ platform: a.platform, account_id: a.id })) })
    if (report(result)) {
      creation.events = []
      const jobs=result.results.filter(r => r.ok)
      router.replace(jobs.length === 1 ? `/drafts/${jobs[0].job_id}` : '/drafts')
    }
    await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function runAction(action, jobs) {
  if (!jobs.length) return
  pending.value = true; notice.value = ''
  try {
    const payload = { job_ids: jobs.map(j => j.id), action, expected_revisions: Object.fromEntries(jobs.map(j => [j.id, j.revision])) }
    if (action === 'confirm_publish') payload.expected_approvals = Object.fromEntries(jobs.map(j => [j.id, {
      revision: j.revision, content_hash: j.content_hash, account_id: j.target.account_id, account_identity: j.remote.verified_identity,
      draft_id: j.remote.draft_id, remote_fingerprint: j.remote.fingerprint }]))
    report(await request('/api/production/actions', payload))
    selectedJobs.value = []; confirmOpen.value = false; await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
function openConfirmation(jobs) { confirmationJobs.value = JSON.parse(JSON.stringify(jobs)); confirmOpen.value = true }
function openTitle() { title.value = currentJob.value.draft.title; titleRevision.value = currentJob.value.revision; titleOpen.value = true }
function moveEntry(list, index, offset) {
  const target=index+offset
  if (target < 0 || target >= list.length) return
  const [entry]=list.splice(index,1); list.splice(target,0,entry)
}
function openEntries() { entries.value=currentJob.value.packet.entries.map(e => ({ ...e })); entriesRevision.value=currentJob.value.revision; entriesOpen.value=true }
async function saveEntries() {
  pending.value=true; notice.value=''
  try {
    await request(`/api/production/jobs/${currentJob.value.id}/entries`, { expected_revision:entriesRevision.value, entry_ids:entries.value.map(e => e.id) })
    entriesOpen.value=false; await refresh()
  } catch(e) { notice.value=e.message } finally { pending.value=false }
}
function openBody() {body.value=currentJob.value.draft.body_html;title.value=currentJob.value.draft.title;bodyOriginal.value=body.value;titleOriginal.value=title.value;bodyRevision.value=currentJob.value.revision;notice.value='';bodyOpen.value=true}
async function saveBody() {
  pending.value=true; notice.value=''
  try {
    await request(`/api/production/jobs/${currentJob.value.id}`, { expected_revision:bodyRevision.value, changes:{ body_html:body.value,title:title.value } }, 'PATCH')
    bodyOpen.value=false; await refresh()
  } catch(e) { notice.value=e.message } finally { pending.value=false }
}
async function uploadBodyImage(event) {
  const file=event.target.files?.[0]; event.target.value=''
  if (!file) return
  if (!['image/png','image/jpeg'].includes(file.type) || file.size > 10000000) { notice.value='请选择 10 MB 以内的 PNG 或 JPEG'; return }
  pending.value=true
  try {
    const encoded=await new Promise((resolve,reject) => { const reader=new FileReader(); reader.onload=() => resolve(String(reader.result).split(',')[1]); reader.onerror=() => reject(new Error('读取图片失败')); reader.readAsDataURL(file) })
    const result=await request(`/api/production/jobs/${currentJob.value.id}/body-image`, { filename:file.name, content_type:file.type, content_base64:encoded, expected_revision:bodyRevision.value })
    richEditor.value?.insertFigure(result.url)
  } catch(e) { notice.value=e.message } finally { pending.value=false }
}
async function saveTitle() {
  pending.value = true
  try { await request(`/api/production/jobs/${currentJob.value.id}`, { expected_revision: titleRevision.value, changes: { title: title.value } }, 'PATCH'); titleOpen.value = false; await refresh() }
  catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function uploadCover(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (!file) return
  if (!['image/png','image/jpeg'].includes(file.type) || file.size > 10000000) { notice.value = '请选择 10 MB 以内的 PNG 或 JPEG'; return }
  const job = currentJob.value
  pending.value = true
  try {
    const encoded = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('读取图片失败')); reader.readAsDataURL(file) })
    await request(`/api/production/jobs/${job.id}/cover`, { filename: file.name, content_type: file.type, content_base64: encoded, expected_revision: job.revision })
    await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function removeCover() {
  const job = currentJob.value
  pending.value = true
  try {
    await request(`/api/production/jobs/${job.id}`, { expected_revision: job.revision, changes: { cover_url: null } }, 'PATCH')
    await refresh()
  } catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function addAccount() {
  pending.value = true
  try { await request('/api/production/accounts', accountForm.value); accountForm.value.display_name = ''; accountOpen.value = false; await refresh() }
  catch (e) { notice.value = e.message } finally { pending.value = false }
}
async function connect(account) {
  pending.value = true
  try { const result = await request(`/api/production/accounts/${account.id}/connect`, {}); notice.value = result.message || '连接中'; await refresh() }
  catch (e) { notice.value = e.message } finally { pending.value = false }
}
</script>

<template>
  <section class="work-page">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-if="notice" :title="notice" type="info" @close="notice=''" />
    <template v-if="accountsPage">
      <header class="page-heading"><h1>账号</h1><el-button type="primary" @click="accountOpen=true"><Icon name="plus"/>添加账号</el-button></header>
      <div class="surface"><div v-for="account in data.accounts" :key="account.id" class="account-row"><span class="platform-mark" :class="account.platform">{{account.platform==='wechat'?'微':'头'}}</span><div><strong>{{account.identity?.name||account.display_name}}</strong><p class="muted">{{platform(account.platform)}}</p><p v-if="account.error" class="action-error">{{account.error}}</p></div><span class="status-pill" :class="account.status==='connected'?'success':'neutral'">{{accountStatuses[account.status]||'待连接'}}</span><el-button :disabled="pending||account.capabilities?.connect===false" @click="connect(account)">{{account.identity?.key?'核验':'登录'}}</el-button></div></div>
      <div v-if="!loading&&!data.accounts.length" class="empty-state"><h2>暂无账号</h2><el-button @click="accountOpen=true">添加账号</el-button></div>
    </template>
    <template v-else-if="creating">
      <header class="page-heading"><div class="actions"><RouterLink class="back-link" aria-label="返回选题" :to="{path:'/topics',query:{column:creation.lineId}}"><Icon name="back"/></RouterLink><h1>{{creation.format==='digest'?'制作集合稿':'制作单篇'}}</h1></div></header>
      <div class="creation-layout"><section class="surface creation-topics"><div class="section-heading"><h2>已选内容</h2><span class="muted">{{creation.events.length}} 条</span></div><ol class="entry-list"><li v-for="(event,index) in creation.events" :key="event.id"><span>{{event.title}}</span><div class="actions"><template v-if="creation.format==='digest'"><el-button text :disabled="index===0||pending" :aria-label="`上移 ${event.title}`" @click="moveEntry(creation.events,index,-1)">上移</el-button><el-button text :disabled="index===creation.events.length-1||pending" :aria-label="`下移 ${event.title}`" @click="moveEntry(creation.events,index,1)">下移</el-button></template><el-button text :disabled="pending" :aria-label="`移出 ${event.title}`" @click="creation.events.splice(index,1)">移出</el-button></div></li></ol><RouterLink v-if="!creation.events.length" :to="{path:'/topics',query:{column:creation.lineId}}">选择选题</RouterLink></section><aside class="surface creation-targets"><h2>目标账号</h2><el-checkbox-group v-model="targets" class="target-list"><el-checkbox v-for="account in data.accounts" :key="account.id" :value="account.id">{{platform(account.platform)}} · {{account.identity?.name||account.display_name}}</el-checkbox></el-checkbox-group><RouterLink v-if="!loading&&!data.accounts.length" to="/accounts">添加账号</RouterLink><el-button class="wide-button" type="primary" :loading="pending" :disabled="!targets.length||!creation.events.length" @click="createJobs">开始制作</el-button></aside></div>
    </template>
    <template v-else-if="currentJob">
      <template v-if="bodyOpen">
        <header class="page-heading editor-heading"><div class="actions"><h1>编辑稿件</h1><span class="status-pill neutral">{{platform(currentJob.target.platform)}}</span><span v-if="dirty" class="muted">未保存</span></div><div class="actions"><el-button :disabled="pending" @click="closeEditor">取消</el-button><el-button type="primary" :loading="pending" :disabled="!body.trim()||!title.trim()" @click="saveBody">保存修改</el-button></div></header>
        <section class="editor-sheet"><label class="sr-only" for="article-title">文章标题</label><textarea id="article-title" v-model="title" class="editor-title" aria-label="文章标题" rows="2" :disabled="pending"/><RichEditor ref="richEditor" v-model="body" :disabled="pending" :platform="currentJob.target.platform" :presentation-mode="currentJob.draft.presentation_mode" @image="uploadBodyImage" /></section>
      </template>
      <template v-else>
        <header class="page-heading"><RouterLink to="/drafts" class="back-link"><Icon name="back"/>稿件</RouterLink><div class="actions"><el-button @click="compareOriginal">{{comparing?'收起材料':'材料'}}</el-button><el-dropdown trigger="click" @command="command=>command==='entries'?openEntries():runAction(command,[currentJob])"><el-button aria-label="稿件更多操作"><Icon name="more"/></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="review" :disabled="!canEdit(currentJob)||pending">重新审稿</el-dropdown-item><el-dropdown-item v-if="currentJob.packet?.format==='digest'" command="entries" :disabled="!canEdit(currentJob)||pending">调整条目</el-dropdown-item><el-dropdown-item command="cancel" :disabled="!canCancel(currentJob)||pending" divided>取消稿件</el-dropdown-item></el-dropdown-menu></template></el-dropdown><el-button v-if="canEdit(currentJob)" type="primary" :disabled="pending" @click="openBody"><Icon name="edit"/>编辑稿件</el-button></div></header>
        <div class="work-layout" :class="{'is-comparing':comparing}">
          <aside v-if="comparing" class="original-read surface"><div class="section-heading"><h2>材料</h2><a v-if="original&&safeLink(original.url)" :href="safeLink(original.url)" target="_blank" rel="noopener noreferrer">原文 ↗</a></div><el-select v-if="originals.length>1" v-model="originalIndex" aria-label="选择报道"><el-option v-for="(source,index) in originals" :key="index" :value="index" :label="source.title"/></el-select><p v-if="sourceError" class="action-error">{{sourceError}}</p><h3 v-if="original">{{original.title}}</h3><p v-for="(paragraph,index) in originalParagraphs" :key="index">{{paragraph}}</p><div class="material-links"><a v-for="source in [...articleSources,...hotlistSources]" :key="source.url" :href="safeLink(source.url)" target="_blank" rel="noopener noreferrer">{{source.label||source.title}} ↗</a></div></aside>
          <article class="article-read surface"><div class="article-meta">{{currentWork?.line_name||currentJob.packet?.line?.name}}<span class="dot">·</span>{{platform(currentJob.target.platform)}}</div><h1>{{currentJob.draft?.title||currentJob.source_title}}</h1><ArticleBody :html="currentJob.draft?.body_html||currentJob.draft?.html||''"/></article>
          <aside class="delivery-rail"><section class="surface delivery-panel"><h2>平台稿件</h2><div v-for="job in workJobs(currentWork)" :key="job.id" class="delivery-card" :class="{active:job.id===currentJob.id}"><RouterLink :to="`/drafts/${job.id}`" class="delivery-title"><span class="platform-mark small" :class="job.target.platform">{{job.target.platform==='wechat'?'微':'头'}}</span><strong>{{platform(job.target.platform)}}</strong><Icon v-if="job.id===currentJob.id" name="check" :size="16"/></RouterLink><p class="muted">{{accountName(job)}}</p><span class="status-pill" :class="job.stage==='remote_verified'?'success':job.error?'warning':'neutral'">{{status(job)}}</span><template v-if="job.id===currentJob.id"><p v-if="problem(job)" class="action-error" role="alert">{{problem(job)}}</p><details v-if="job.error && problem(job)!==job.error" class="error-details"><summary>错误详情</summary><p>{{job.error}}</p></details><div class="delivery-actions"><a v-if="draftLink(job)" :href="draftLink(job)" target="_blank" rel="noopener noreferrer">平台草稿 ↗</a><a v-else-if="job.remote?.draft_id" :href="job.target.platform==='wechat'?'https://mp.weixin.qq.com/':'https://mp.toutiao.com/'" target="_blank" rel="noopener noreferrer">平台后台 ↗</a><el-button v-if="primary(job)" size="small" :disabled="pending" @click="runAction(primary(job).action,[job])">{{primary(job).label}}</el-button></div><el-tooltip :content="publishReason(job)" :disabled="canPublish(job)"><span class="publish-action"><el-button :disabled="!canPublish(job)||pending" @click="openConfirmation([job])">发布</el-button></span></el-tooltip></template></div></section><section class="surface cover-panel"><h2>封面</h2><img v-if="coverUrl(currentJob.draft?.cover_url)" :src="coverUrl(currentJob.draft.cover_url)" alt="封面"/><span v-else class="muted">未设置</span><label v-if="canEdit(currentJob)" class="upload-button" :class="{disabled:pending}">更换封面<input type="file" accept="image/png,image/jpeg" :disabled="pending" @change="uploadCover"/></label><el-button v-if="canEdit(currentJob) && currentJob.draft?.cover_url" text :disabled="pending" @click="removeCover">移除封面</el-button></section></aside>
        </div>
      </template>
    </template>
    <div v-else-if="route.params.id&&!loading" class="empty-state"><h2>稿件不存在</h2><RouterLink to="/drafts">返回稿件</RouterLink></div>
    <template v-else>
      <header class="page-heading"><div class="actions"><h1>稿件</h1><el-select v-model="columnFilter" aria-label="稿件栏目" style="width:150px"><el-option label="全部栏目" value=""/><el-option v-for="column in workColumns" :key="column.id" :label="column.name" :value="column.id"/></el-select></div><el-button @click="router.push('/topics')">选择选题<Icon name="arrow"/></el-button></header>
      <div class="filter-tabs"><button v-for="option in [{id:'all',name:'全部'},{id:'working',name:'制作中'},{id:'attention',name:'待处理'},{id:'saved',name:'已存草稿'}]" :key="option.id" :class="{active:filter===option.id}" @click="filter=option.id">{{option.name}}</button></div>
      <div class="surface work-list"><article v-for="work in visibleWorks" :key="work.id" class="work-row"><span class="resource-icon"><Icon name="drafts"/></span><div class="work-summary"><RouterLink :to="`/drafts/${work.job_ids[0]}`" class="draft-title">{{work.title}}</RouterLink><div class="muted">{{work.line_name}}<span class="dot">·</span>{{work.format==='digest'?'集合稿':'单篇'}}</div></div><div class="work-platforms"><RouterLink v-for="job in workJobs(work)" :key="job.id" :to="`/drafts/${job.id}`"><span class="platform-mark tiny" :class="job.target.platform">{{job.target.platform==='wechat'?'微':'头'}}</span><span>{{platform(job.target.platform)}}</span><span class="status-dot" :class="job.stage==='remote_verified'?'success':job.error?'warning':'neutral'"></span><span class="muted">{{status(job)}}</span></RouterLink></div><RouterLink :to="`/drafts/${work.job_ids[0]}`" class="icon-button" :aria-label="`打开 ${work.title}`"><Icon name="arrow"/></RouterLink></article></div>
      <div v-if="!loading&&!visibleWorks.length" class="empty-state"><h2>暂无稿件</h2><el-button @click="router.push('/topics')">选择选题</el-button></div>
    </template>
  </section>
  <el-dialog v-model="entriesOpen" title="调整集合条目" width="620px" :close-on-click-modal="false"><ol class="entry-list"><li v-for="(entry,index) in entries" :key="entry.id"><span>{{entry.title}}</span><div class="actions"><el-button text :disabled="index===0||pending" @click="moveEntry(entries,index,-1)">上移</el-button><el-button text :disabled="index===entries.length-1||pending" @click="moveEntry(entries,index,1)">下移</el-button><el-button text :disabled="pending" @click="entries.splice(index,1)">移出</el-button></div></li></ol><p v-if="notice" class="action-error">{{notice}}</p><template #footer><el-button :disabled="pending" @click="entriesOpen=false">取消修改</el-button><el-button type="primary" :loading="pending" @click="saveEntries">{{entries.length?'保存并重新组稿':'取消这份稿件'}}</el-button></template></el-dialog>
  <el-dialog v-model="confirmOpen" title="确认发布" width="560px"><div v-for="job in confirmationJobs" :key="job.id" class="publish-row"><strong>{{job.draft.title}}</strong><p>{{platform(job.target.platform)}} · {{accountName(job)}}</p><a v-if="draftLink(job)" :href="draftLink(job)" target="_blank" rel="noopener noreferrer">平台草稿 ↗</a></div><template #footer><el-button @click="confirmOpen=false">取消</el-button><el-button type="primary" :loading="pending" @click="runAction('confirm_publish',confirmationJobs)">发布 {{confirmationJobs.length}} 篇</el-button></template></el-dialog>
  <el-dialog v-model="accountOpen" title="添加账号" width="440px"><el-form label-position="top" @submit.prevent="addAccount"><el-form-item label="平台"><el-select v-model="accountForm.platform" aria-label="平台"><el-option label="公众号" value="wechat"/><el-option label="今日头条" value="toutiao"/></el-select></el-form-item><el-form-item label="账号名称"><el-input v-model="accountForm.display_name" aria-label="账号名称"/></el-form-item></el-form><p v-if="notice" class="action-error">{{notice}}</p><template #footer><el-button @click="accountOpen=false">取消</el-button><el-button type="primary" :loading="pending" :disabled="!accountForm.display_name.trim()" @click="addAccount">添加</el-button></template></el-dialog>
</template>
