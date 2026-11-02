<script setup>
import { computed, ref, watch } from 'vue'
import { api } from './api'
const props = defineProps({modelValue:Boolean,job:Object,editable:Boolean})
const emit = defineEmits(['update:modelValue','updated','edit'])
const evidence = ref(null), loading = ref(false), pending = ref(false), error = ref('')
let requestId = 0
const busy = computed(() => props.job?.local_operation === 'review' && ['queued','running'].includes(props.job?.status))
const failed = computed(() => props.job?.local_operation === 'review' && props.job?.status === 'failed')
const verdict = computed(() => evidence.value?.editorial_review)
const current = computed(() => props.job?.content_review?.current ?? props.job?.editorial_approved)
const heading = computed(() => busy.value ? props.job.status === 'queued' ? '审稿排队中' : '正在检查标题、正文与来源' : failed.value ? '本次审稿未完成' : props.job?.editorial_approved ? '当前稿件已通过审稿' : !current.value ? verdict.value ? '稿件已变化，需要检查当前版本' : '当前稿件尚未审稿' : verdict.value?.decision === 'insufficient' ? '材料不足，需要补充依据' : '需要修改')
const checkLabels = {source_binding:'来源依据',title:'标题',contribution:'内容价值',facts:'事实核对',expression:'表达'}
watch(() => [props.modelValue,props.job?.id,props.job?.revision,props.job?.status,props.job?.updated_at], async ([open,id]) => {
 const request = ++requestId
 if (!open || !id) return
 loading.value = true
 try { const data = await api(`/api/production/jobs/${id}/sources`); if (request === requestId) evidence.value = data.evidence }
 catch (e) { if (request === requestId) error.value = e.message }
 finally { if (request === requestId) loading.value = false }
}, {immediate:true})
async function review() {
 pending.value = true; error.value = ''
 try {
  const j = props.job
  const r = await api('/api/production/actions', {job_ids:[j.id],action:failed.value?'retry':'review',expected_revisions:{[j.id]:j.revision}})
  const failure = r.results.find(r => !r.ok)
  if (failure) throw new Error(failure.error)
  emit('updated')
 } catch (e) { error.value = e.message } finally { pending.value = false }
}
function edit(text='') { emit('update:modelValue',false); emit('edit',text) }
</script>
<template>
 <el-drawer :model-value="modelValue" title="审稿报告" size="560px" @close="emit('update:modelValue',false)">
  <section class="review-report" aria-live="polite">
   <el-alert v-if="error" :title="error" type="error" :closable="false"/>
   <div class="review-status" :class="job?.editorial_approved?'passed':''"><h2>{{heading}}</h2><p v-if="busy">完成后会在这里显示结果，可以离开页面稍后查看。</p><p v-else-if="failed">{{job.error}}</p><p v-else-if="job?.editorial_approved">{{job.target.platform==='local'?'本地稿已就绪，可继续编辑或查看成稿。':'可以继续存入平台草稿。'}}</p><p v-else>检查已保存的稿件，给出具体问题和修改建议。</p></div>
   <div v-if="!busy&&(editable||failed)&&(!job?.editorial_approved||failed)" class="actions review-actions"><el-button v-if="!current||failed||!verdict" type="primary" :loading="pending" @click="review">{{failed?'重试审稿':'检查当前稿件'}}</el-button><el-button v-if="editable" :disabled="pending" @click="edit()">编辑稿件</el-button></div>
   <p v-if="loading&&!verdict" class="muted">正在读取报告…</p>
   <template v-if="verdict&&!busy">
    <p v-if="!current" class="muted">以下是上次报告，尚不代表当前稿件。</p>
    <section v-for="(issue,index) in verdict.issues||[]" :key="index" class="review-issue"><h3>问题 {{index+1}}</h3><blockquote v-if="issue.text">{{issue.text}}</blockquote><p>{{issue.reason}}</p><p><strong>修改建议：</strong>{{issue.fix}}</p><el-button v-if="editable&&current" text @click="edit(issue.text)">{{issue.text?'定位到正文修改':'编辑稿件'}}</el-button></section>
    <details v-for="(check,key) in verdict.checks||{}" :key="key" class="review-check"><summary><span>{{checkLabels[key]||key}}</span><span :class="check.decision==='pass'?'review-pass':'review-warning'">{{{pass:'通过',revise:'需修改',insufficient:'缺材料'}[check.decision]||'待核对'}}</span></summary><p>{{check.reason}}</p></details>
   </template>
  </section>
 </el-drawer>
</template>
<style scoped>
.review-report{padding-top:16px;font-size:14px;line-height:1.7}.review-status{padding:16px;background:#f7f8f8;border-radius:8px}.review-status.passed{background:#edf6f2}.review-status h2{font-size:16px;margin:0 0 8px}.review-status p{margin:0;color:#66756e}.review-actions{margin:16px 0 24px}.review-issue{padding:16px 0;border-bottom:1px solid #e2e8e5}.review-issue h3{font-size:14px;margin:0 0 8px}.review-issue p{margin:8px 0}.review-issue blockquote{margin:12px 0;border-left:3px solid #d5e4de;padding:8px 12px;background:#f7f9f8}.review-check{border-bottom:1px solid #e2e8e5}.review-check summary{display:flex;justify-content:space-between;cursor:pointer;padding:14px 0}.review-check p{color:#66756e;margin:0 0 16px}.review-pass{color:#207469}.review-warning{color:#a16e27}
</style>
