<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import { api } from './api'
import Icon from './Icon.vue'
import { creation, switchSelection } from './workflow'

const route=useRoute(), router=useRouter()
const data=ref({revision:0,lines:[],sources:[]}), accounts=ref([])
const loading=ref(true), pending=ref(false), error=ref(''), fieldError=ref('')
const editing=ref(false), editingId=ref(null), revision=ref(0), form=ref({screening_rules:{initial:'',selection:'',verification:''}}), formTab=ref('content')
const sourceOpen=ref(false), source=ref(null), sourceRevision=ref(0)
const sourcesPage=computed(()=>route.path==='/sources')
const names=computed(()=>Object.fromEntries(data.value.sources.map(s=>[s.id,s.name])))
const accountIds=computed({get:()=>form.value.targets?.map(t=>t.account_id)||[],set:ids=>{form.value.targets=accounts.value.filter(a=>ids.includes(a.id)).map(a=>({platform:a.platform,account_id:a.id}))}})
const automatic=computed({get:()=>form.value.interval_minutes!==null,set:v=>{form.value.interval_minutes=v?60:null}})
const collecting=computed({get:()=>source.value?.interval_minutes!==null,set:v=>{source.value.interval_minutes=v?30:null}})
async function refresh(){
  loading.value=true;error.value=''
  try{const [c,p]=await Promise.all([api('/api/columns'),api('/api/production')]);data.value=c;accounts.value=p.accounts}
  catch(e){error.value=e.message}finally{loading.value=false}
}
function open(line){
  editingId.value=line?.id||null;revision.value=data.value.revision;fieldError.value='';formTab.value='content'
  form.value=line?{enabled:true,writing_rules:'',...JSON.parse(JSON.stringify(line))}:{name:'',sources:[],rules:'',screening_rules:JSON.parse(JSON.stringify(data.value.screening_templates?.hotspot||{initial:'',selection:'',verification:''})),max_candidates:2,writing_rules:'',mode:'hotspot',format:'single',valid_hours:48,edition_hours:24,reference_domains:[],interval_minutes:null,targets:[],auto_draft:false,enabled:true}
  editing.value=true
}
async function save(){
  fieldError.value=''
  if(!form.value.name.trim()){fieldError.value='请填写栏目名称';return}
  if(!form.value.sources.length){fieldError.value='请选择渠道';return}
  if(!form.value.rules.trim()){fieldError.value='请填写栏目定位与范围';formTab.value='content';return}
  for(const [key,label] of [['initial','初筛'],['selection','二筛'],['verification','三筛']])if(!form.value.screening_rules?.[key]?.trim()){fieldError.value=`请填写${label}规则`;formTab.value='screening';return}
  pending.value=true
  const fields=['name','sources','rules','screening_rules','max_candidates','writing_rules','mode','format','valid_hours','edition_hours','reference_domains','interval_minutes','targets','auto_draft','enabled']
  try{await api('/api/columns'+(editingId.value?'/'+editingId.value:''),{expected_revision:revision.value,column:Object.fromEntries(fields.map(k=>[k,form.value[k]??(k==='enabled'?true:k==='writing_rules'?'':form.value[k])]))},editingId.value?'PATCH':'POST');editing.value=false;await refresh();ElMessage.success('栏目已保存')}
  catch(e){fieldError.value=e.message}finally{pending.value=false}
}
async function useTemplate(){
  const value=data.value.screening_templates?.[form.value.mode];if(!value)return
  try{await ElMessageBox.confirm('替换当前三轮规则？','套用规则模板',{confirmButtonText:'替换',cancelButtonText:'取消'})}catch{return}
  form.value.screening_rules=JSON.parse(JSON.stringify(value))
}
async function toggle(line){
  pending.value=true
  try{await api('/api/columns/'+line.id,{expected_revision:data.value.revision,column:{enabled:!(line.enabled!==false)}},'PATCH');await refresh()}
  catch(e){error.value=e.message}finally{pending.value=false}
}
async function remove(line){
  try{await ElMessageBox.confirm(`删除“${line.name}”？`,'删除栏目',{confirmButtonText:'删除',cancelButtonText:'取消',type:'warning'})}catch{return}
  pending.value=true
  try{await api('/api/columns/'+line.id,{expected_revision:data.value.revision},'DELETE');await refresh()}
  catch(e){error.value=e.message}finally{pending.value=false}
}
function enter(line){switchSelection(line.id);creation.format=line.format;router.push({path:'/topics',query:{column:line.id}})}
function editSource(value){source.value={...value};sourceRevision.value=data.value.revision;fieldError.value='';sourceOpen.value=true}
async function saveSource(){pending.value=true;fieldError.value='';try{await api('/api/sources/'+source.value.id,{expected_revision:sourceRevision.value,source:{enabled:source.value.enabled,interval_minutes:source.value.interval_minutes}},'PATCH');sourceOpen.value=false;await refresh();ElMessage.success('来源设置已保存')}catch(e){fieldError.value=e.message}finally{pending.value=false}}
watch(()=>route.path,()=>{editing.value=false;sourceOpen.value=false;refresh()})
watch(accountIds,ids=>{if(!ids.length)form.value.auto_draft=false})
watch(()=>form.value.mode,(next,previous)=>{
  if(!editingId.value&&data.value.screening_templates?.[next]&&JSON.stringify(form.value.screening_rules)===JSON.stringify(data.value.screening_templates?.[previous])){
    form.value.screening_rules=JSON.parse(JSON.stringify(data.value.screening_templates[next]))
    form.value.max_candidates=next==='digest'?5:2
  }
})
watch(form,()=>{fieldError.value=''}, {deep:true})
onMounted(refresh)
</script>

<template>
  <header class="page-heading"><h1>{{sourcesPage?'渠道设置':'栏目'}}</h1><el-button v-if="!sourcesPage" type="primary" @click="open()"><Icon name="plus" />新建栏目</el-button></header>
  <div v-if="error" class="error-retry"><el-alert :title="error" type="error" :closable="false" /><el-button :disabled="loading" @click="refresh">重试</el-button></div>
  <div v-if="loading" class="loading-state" role="status">正在加载…</div>
  <template v-else-if="sourcesPage">
    <div class="surface"><div class="source-table-head"><span>来源</span><span>采集安排</span><span>状态</span><span></span></div>
      <div v-for="row in data.sources" :key="row.id" class="source-row"><div class="source-name"><span class="resource-icon"><Icon name="sources" /></span>{{row.name}}</div><span class="muted">{{row.interval_minutes?`每 ${row.interval_minutes} 分钟`:'手动'}}</span><span class="status-pill" :class="row.enabled?'success':'neutral'">{{row.enabled?'已启用':'已停用'}}</span><el-button text @click="editSource(row)">设置</el-button></div>
    </div>
  </template>
  <template v-else>
    <div v-if="!data.lines.length" class="empty-state"><Icon name="columns" :size="34" /><h2>还没有栏目</h2><el-button type="primary" @click="open()">新建栏目</el-button></div>
    <div v-else class="column-grid">
      <article v-for="line in data.lines" :key="line.id" class="column-card">
        <div class="card-top"><span class="resource-icon"><Icon name="columns" /></span><span class="status-pill" :class="line.running?'progress':line.enabled===false?'neutral':'success'">{{line.running?'筛选中':line.enabled===false?'已停用':'已启用'}}</span><el-dropdown trigger="click" @command="command=>command==='edit'?open(line):command==='toggle'?toggle(line):remove(line)"><button class="icon-button" :aria-label="`${line.name}的操作`"><Icon name="more" /></button><template #dropdown><el-dropdown-menu><el-dropdown-item command="edit" :disabled="line.running">编辑栏目</el-dropdown-item><el-dropdown-item command="toggle" :disabled="line.running||pending">{{line.enabled===false?'启用':'停用'}}</el-dropdown-item><el-dropdown-item v-if="!line.has_history" command="delete" divided>删除</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div>
        <button class="column-title" @click="enter(line)">{{line.name}}</button>
        <div class="channel-tags"><span v-for="id in line.sources" :key="id">{{names[id]||id}}</span></div>
        <div class="column-card-bottom"><span class="muted">{{line.format==='digest'?'集合稿':'单篇'}}<span class="dot">·</span>{{line.interval_minutes?`每 ${line.interval_minutes} 分钟筛选`:'手动筛选'}}</span><button class="text-action" @click="enter(line)">选题<Icon name="arrow" :size="16" /></button></div>
      </article>
    </div>
  </template>

  <el-drawer v-model="editing" :title="editingId?'编辑栏目':'新建栏目'" size="640px" :close-on-click-modal="false" class="column-form-drawer">
    <el-tabs v-model="formTab"><el-tab-pane label="内容设置" name="content"/><el-tab-pane label="三轮规则" name="screening"/><el-tab-pane label="运行安排" name="schedule"/></el-tabs>
    <el-alert v-if="fieldError" :title="fieldError" type="error" :closable="false" />
    <el-form label-position="top" @submit.prevent="save">
      <div v-show="formTab==='content'">
        <el-form-item label="栏目名称" required><el-input v-model="form.name" aria-label="栏目名称" maxlength="40" /></el-form-item>
        <el-form-item label="渠道" required><el-checkbox-group v-model="form.sources" class="channel-picker"><el-checkbox v-for="s in data.sources" :key="s.id" :value="s.id">{{s.name}}<span v-if="!s.enabled" class="muted">（已停用）</span></el-checkbox></el-checkbox-group></el-form-item>
        <el-form-item label="栏目定位与范围" required><el-input v-model="form.rules" aria-label="栏目定位与范围" type="textarea" :rows="3" maxlength="10000" /></el-form-item>
        <div class="form-grid"><el-form-item label="筛选方式"><el-select v-model="form.mode" aria-label="筛选方式"><el-option label="递进筛选" value="hotspot"/><el-option label="消息选编" value="digest"/></el-select></el-form-item><el-form-item label="成稿形式"><el-select v-model="form.format" aria-label="成稿形式"><el-option label="单篇" value="single"/><el-option label="集合稿" value="digest"/></el-select></el-form-item></div>
        <el-form-item label="创作要求"><el-input v-model="form.writing_rules" aria-label="创作要求" type="textarea" :rows="3" maxlength="10000" /></el-form-item>
        <div class="form-grid"><el-form-item label="内容有效期"><div class="number-field"><el-input-number v-model="form.valid_hours" :min="1" :max="8760" aria-label="内容有效期"/><span>小时</span></div></el-form-item><el-form-item v-if="form.format==='digest'" label="集合周期"><div class="number-field"><el-input-number v-model="form.edition_hours" :min="1" :max="8760" aria-label="集合周期"/><span>小时</span></div></el-form-item></div>
      </div>
      <div v-show="formTab==='screening'">
        <el-form-item label="初筛规则" required><el-input v-model="form.screening_rules.initial" aria-label="初筛规则" type="textarea" :rows="4" maxlength="10000" /></el-form-item>
        <el-form-item label="二筛规则" required><el-input v-model="form.screening_rules.selection" aria-label="二筛规则" type="textarea" :rows="4" maxlength="10000" /></el-form-item>
        <el-form-item label="三筛规则" required><el-input v-model="form.screening_rules.verification" aria-label="三筛规则" type="textarea" :rows="4" maxlength="10000" /></el-form-item>
        <el-form-item label="单轮最多取材"><div class="number-field"><el-input-number v-model="form.max_candidates" :min="1" :max="10" :precision="0" aria-label="单轮最多取材"/><span>条</span></div></el-form-item>
        <el-button text @click="useTemplate">套用{{form.mode==='digest'?'简报':'单篇'}}规则模板</el-button>
      </div>
      <div v-show="formTab==='schedule'">
        <div class="setting-row"><label>启用栏目</label><el-switch v-model="form.enabled" aria-label="启用栏目" /></div>
        <div class="setting-row"><label>自动筛选</label><el-switch v-model="automatic" aria-label="自动筛选" /></div>
        <el-form-item v-if="automatic" label="筛选间隔"><div class="number-field"><el-input-number v-model="form.interval_minutes" :min="1" :max="10080" aria-label="筛选间隔"/><span>分钟</span></div></el-form-item>
        <el-form-item label="目标账号"><el-checkbox-group v-model="accountIds" class="account-picker"><el-checkbox v-for="a in accounts" :key="a.id" :value="a.id">{{a.platform==='wechat'?'公众号':'今日头条'}} · {{a.identity?.name||a.display_name}}</el-checkbox></el-checkbox-group><RouterLink v-if="!accounts.length" to="/accounts">添加账号</RouterLink></el-form-item>
        <div class="setting-row"><label>自动做稿并存草稿</label><el-switch v-model="form.auto_draft" aria-label="自动做稿并存草稿" :disabled="!accountIds.length" /></div>
      </div>
    </el-form>
    <template #footer><el-button :disabled="pending" @click="editing=false">取消</el-button><el-button type="primary" :loading="pending" @click="save">保存栏目</el-button></template>
  </el-drawer>

  <el-dialog v-model="sourceOpen" :title="source?.name" width="440px" :close-on-click-modal="false"><template v-if="source"><div class="setting-row"><label>启用来源</label><el-switch v-model="source.enabled" aria-label="启用来源" /></div><div class="setting-row"><label>自动采集</label><el-switch v-model="collecting" aria-label="自动采集" /></div><el-form v-if="collecting" label-position="top"><el-form-item label="采集间隔"><div class="number-field"><el-input-number v-model="source.interval_minutes" :min="1" :max="10080" aria-label="采集间隔"/><span>分钟</span></div></el-form-item></el-form><el-alert v-if="fieldError" :title="fieldError" type="error" :closable="false"/></template><template #footer><el-button @click="sourceOpen=false">取消</el-button><el-button type="primary" :loading="pending" @click="saveSource">保存</el-button></template></el-dialog>
</template>
