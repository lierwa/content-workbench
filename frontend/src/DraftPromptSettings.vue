<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, isPrototype } from './api'

const props = defineProps({ line: Object })
const emit = defineEmits(['close','saved'])
const panel = ref(true), info = ref(null), error = ref(''), working = ref('')
const autoDraft = ref(false)
const rules = ref(''), limit = ref(2), sample = ref('custom'), text = ref('')
const preview = ref(null), showPreview = ref(false), current = ref(null), previous = ref(null)
const valid = computed(()=>!!rules.value.trim() && Number.isInteger(limit.value) && limit.value >= 1 && limit.value <= 10)
const canTest = computed(()=>valid.value && (sample.value !== 'custom' || !!text.value.trim()))
function request(action) {
 return api('/api/topics/draft-prompt', {action,line_id:props.line.id,rules:rules.value,limit:limit.value,sample:sample.value,text:sample.value==='custom'?text.value:''})
}
async function load() {
 working.value='load';error.value=''
 try {
  const result=await request('open')
  info.value=result;autoDraft.value=result.auto_draft??!!props.line.auto_draft;rules.value=result.rules;limit.value=result.limit;sample.value=result.sample||'custom'
 }catch(e){error.value=e.message}finally{working.value=''}
}
async function inspect() {
 working.value='preview';error.value=''
 try{preview.value=await request('preview');showPreview.value=true}
 catch(e){error.value=e.message}finally{working.value=''}
}
async function trial() {
 working.value='trial';error.value=''
 const key=sample.value==='custom'?'custom:'+text.value:sample.value
 try {
  const {results}=await request('trial')
  previous.value=current.value?.key===key?current.value:null
  current.value={key,results}
 }catch(e){error.value=e.message}finally{working.value=''}
}
async function save() {
 working.value='save';error.value=''
 try {
  await api('/api/topics/automation',{action:'settings',line_id:props.line.id,expected_revision:info.value.revision,
   settings:{draft_rules:rules.value.trim(),draft_limit:limit.value,...(autoDraft.value!==(info.value.auto_draft??!!props.line.auto_draft)?{auto_draft:autoDraft.value}:{})}})
  emit('saved');panel.value=false;ElMessage.success('选稿设置已保存')
 }catch(e){error.value=e.message}finally{working.value=''}
}
onMounted(load)
</script>
<template>
 <el-drawer v-model="panel" :title="line.name+' · AI 选稿设置'" size="660px" class="draft-prompt-drawer" append-to-body :close-on-click-modal="false" @closed="emit('close')">
  <el-alert v-if="error" type="error" :title="error" :closable="false"/>
  <div v-if="!info" role="status">{{working?'正在读取选稿设置…':''}}<el-button v-if="!working" @click="load">重新读取</el-button></div>
  <template v-else>
   <div class="auto-draft-row"><span>自动选稿并制作</span><el-switch v-model="autoDraft" :disabled="!!working||(line.enabled===false&&!autoDraft)" :aria-label="line.name+'自动选稿并制作'"/></div>
   <div class="rules-head"><label :for="'draft-rules-'+line.id">选稿提示词</label><el-button link :disabled="!!working||rules===info.default_rules" @click="rules=info.default_rules">恢复默认</el-button></div>
   <el-input :id="'draft-rules-'+line.id" v-model="rules" type="textarea" :rows="8" maxlength="10000" aria-label="选稿提示词" :disabled="!!working"/>
   <div class="limit-row"><span>每轮最多入队</span><el-input-number v-model="limit" :min="1" :max="10" :precision="0" controls-position="right" aria-label="每轮入队上限" :disabled="!!working"/><span>个题</span></div>
   <section class="trial-area">
    <div class="rules-head"><h3>试选</h3><el-radio-group v-if="info.sample" v-model="sample" size="small" :disabled="!!working">
     <el-radio-button :value="info.sample">三筛候选</el-radio-button><el-radio-button value="custom">粘贴样稿</el-radio-button>
    </el-radio-group></div>
    <p v-if="!info.sample" class="muted">暂无三筛通过的候选。可以粘贴正文试选。</p>
    <el-input v-if="sample==='custom'" v-model="text" type="textarea" :rows="3" :disabled="!!working" maxlength="15000" aria-label="试选样稿" placeholder="粘贴帖子正文。多篇用单独一行 --- 分隔。"/>
    <details v-else><summary>本批 {{info.titles.length}} 个候选</summary><ol><li v-for="(title,i) in info.titles" :key="i">{{title}}</li></ol></details>
    <div class="trial-actions">
     <el-button :loading="working==='trial'" :disabled="isPrototype||!!working||!canTest" @click="trial">试选一次</el-button>
     <el-button link type="primary" :disabled="isPrototype||!!working||!valid" @click="inspect">查看完整提示词</el-button>
    </div>
    <p class="muted">{{isPrototype?'请在本机工作台预览和试选。':'试选只返回判断，稿件不会入队。'}}</p>
    <div v-if="current" class="trial-results"><h4>试选结果</h4>
     <article v-for="(v,i) in current.results" :key="v.id"><strong>{{v.title}}</strong>
      <p v-if="previous" class="muted">上次 · {{previous.results[i]?.use}}　{{previous.results[i]?.reason}}</p>
      <p><b>{{v.use}}</b>　{{v.reason}}</p><blockquote v-if="v.quote">{{v.quote}}</blockquote>
     </article>
    </div>
   </section>
  </template>
  <template #footer><el-button @click="panel=false">关闭</el-button><el-button type="primary" :loading="working==='save'" :disabled="!info||!!working||!valid" @click="save">保存选稿设置</el-button></template>
 </el-drawer>
 <el-dialog v-model="showPreview" append-to-body title="完整提示词预览" width="680px" class="draft-preview">
  <template v-if="preview"><p class="muted">按当前规则、栏目设置和试选材料组装。</p>
   <details><summary>系统指令</summary><pre>{{preview.instructions}}</pre></details>
   <h4>发给 AI 的提示词</h4><pre>{{preview.prompt}}</pre>
   <details><summary>要求 AI 返回的格式</summary><pre>{{JSON.stringify(preview.schema,null,2)}}</pre></details>
  </template>
 </el-dialog>
</template>
<style scoped>
.auto-draft-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:16px;font-size:14px}
.rules-head,.limit-row,.trial-actions{display:flex;align-items:center;gap:10px}
.rules-head{justify-content:space-between;margin:0 0 10px}.rules-head label,.rules-head h3{font-weight:600;font-size:15px;margin:0}
.limit-row{margin:16px 0;font-size:14px}.limit-row .el-input-number{width:96px}
.trial-area{border-top:1px solid #dde6e1;padding-top:18px}.trial-area p{margin:8px 0}
.trial-actions{margin-top:12px}.trial-results{margin-top:20px}.trial-results article{padding:10px 0;border-top:1px solid #e7ece9;font-size:14px}
.trial-results p{line-height:1.6}.trial-results b{color:#207469}blockquote{margin:6px 0;color:#596b62;border-left:2px solid #b7cec3;padding-left:9px}
summary{cursor:pointer;color:#466357;font-size:14px}.draft-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:#f5f8f6;line-height:1.65;max-height:45vh;overflow:auto}
:global(.draft-prompt-drawer){max-width:100vw}.draft-prompt-drawer :deep(.el-alert){margin-bottom:12px}
</style>
