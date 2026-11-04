<script setup>
import { computed, ref, onBeforeUnmount, watch } from 'vue'
import { ElDrawer } from 'element-plus'
import { api } from './api'
import ModelResult from './ModelResult.vue'
const props=defineProps({modelValue:Boolean,embedded:Boolean,batchId:String,jobId:String,kind:{type:String,default:'screening'}})
const emit=defineEmits(['update:modelValue'])
const data=ref({events:[],total:0}),page=ref(1),loading=ref(false),error=ref(''),calls=ref({}),opening=ref({}),acting=ref(false)
const title=computed(()=>props.jobId?'二创过程':props.kind==='selection'?'选稿过程':'筛选过程')
const query=computed(()=>props.jobId?{job_id:props.jobId}:{batch_id:props.batchId,kind:props.kind})
let timer,sequence=0,generation=0,disposed=false
async function refresh(){const n=++sequence;try{const value=await api('/api/session-trace?'+new URLSearchParams({...query.value,page:page.value}));if(!disposed&&props.modelValue&&n===sequence){data.value=value;error.value=''}}catch(e){if(n===sequence)error.value=e.message}finally{if(n===sequence)loading.value=false}}
async function poll(epoch){await refresh();if(!disposed&&props.modelValue&&epoch===generation)timer=setTimeout(()=>poll(epoch),4000)}
async function full(event, open){
 if(!open||calls.value[event.id]||opening.value[event.id])return
 const requestQuery=query.value
 opening.value[event.id]=true
 try{const value=await api('/api/session-trace?'+new URLSearchParams({...requestQuery,call_id:event.id}));if(!disposed&&requestQuery===query.value)calls.value[event.id]=value}
 catch(e){if(!disposed&&requestQuery===query.value)error.value=e.message}
 finally{if(requestQuery===query.value)delete opening.value[event.id]}
}
async function control(action){acting.value=true;try{await api(action==='stop'?'/api/stop':'/api/screen',{batch_id:props.batchId,line_id:data.value.line_id});await refresh()}catch(e){error.value=e.message}finally{acting.value=false}}
function messageError(value){if(/原文读取失败|cooperate_not_ready/.test(value||''))return '原文读取失败，后续核实未完成。';if(/extension protocol|code=unsupported/.test(value||''))return '浏览器扩展版本不兼容。';if(/Codex 退出码|stderr.txt/.test(value||''))return '本次模型调用失败。';return value}
function pretty(value){return typeof value==='string'?value:JSON.stringify(value,null,2)}
function hasValue(value){return value!==null&&value!==undefined&&value!==''&&(typeof value!=='object'||Object.keys(value).length>0)}
function inputText(call){return call?.instructions?`固定指令\n${call.instructions}\n\n本轮输入\n${call.input||''}`:call?.input}
function sessionInfo(event){const meta=event.meta||{};return Object.fromEntries(Object.entries({model:meta.model,effort:meta.effort,session:meta.thread_id,resumed_from:meta.resumed_from,usage:meta.usage}).filter(([,value])=>hasValue(value)))}
function toolName(tool){return tool.type==='web_search'?'搜索资料':tool.tool||tool.name||'工具调用'}
watch(()=>[props.modelValue,props.batchId,props.jobId,props.kind],(current,previous)=>{
 sequence++;generation++;clearTimeout(timer)
 if(!props.embedded||!previous||current.slice(1).some((value,i)=>value!==previous[i+1])){
  calls.value={};opening.value={};page.value=1;data.value={events:[],total:0};error.value=''
 }
 if(props.modelValue){loading.value=!data.value.events.length;poll(generation)}
},{immediate:true})
onBeforeUnmount(()=>{disposed=true;sequence++;clearTimeout(timer)})
</script>
<template>
 <component :is="embedded?'section':ElDrawer" v-bind="embedded?{}:{modelValue,title,size:'480px',appendToBody:true}" :class="embedded?'session-panel surface':'session-drawer'" @update:model-value="emit('update:modelValue',$event)">
  <header v-if="embedded" class="session-panel-header"><h2>{{title}}</h2><button class="icon-button" title="关闭二创过程" aria-label="关闭二创过程" @click="emit('update:modelValue',false)">×</button></header>
  <div :class="{'session-panel-body':embedded}">
  <div class="session-heading"><span>{{data.title}}</span><div v-if="data.can_stop||data.can_resume" class="actions"><el-button v-if="data.can_stop" text :loading="acting" @click="control('stop')">停止</el-button><el-button v-if="data.can_resume" text :loading="acting" @click="control('resume')">继续筛选</el-button></div></div>
  <el-alert v-if="error||data.error" :title="messageError(error||data.error)" type="error" :closable="false"/>
  <p v-if="loading" class="muted">正在读取…</p>
  <p v-else-if="!data.events.length" class="muted">{{data.active?'等待处理记录…':'暂无调用记录'}}</p>
  <div v-else class="session-feed">
   <template v-for="event in data.events" :key="event.id">
    <details v-if="event.type==='tool'" class="tool-call"><summary>{{event.name}}{{event.status==='running'?'…':''}}</summary><div class="tool-body"><label>输入</label><pre>{{pretty(event.input)}}</pre><label>返回</label><pre>{{event.error||pretty(event.output)}}</pre></div></details>
    <section v-else class="session-turn">
     <div class="turn-input"><span class="message-label">{{event.label}}</span><p class="input-bubble">{{event.input}}</p></div>
     <template v-for="(entry,index) in event.activity" :key="index">
      <p v-if="entry.type==='message'&&entry.value.length<280&&!/^[\[{]/.test(entry.value.trim())" class="assistant-message">{{entry.value}}</p>
      <details v-else class="tool-call"><summary>{{entry.type==='message'?'处理说明':toolName(entry.value)}}</summary><div class="tool-body"><pre>{{pretty(entry.value)}}</pre></div></details>
     </template>
     <div v-if="event.output!==null" class="turn-output"><ModelResult :value="event.output" :titles="data.titles" :html="event.output_html" :stage="event.stage" :changes="event.changes"/></div>
     <p v-if="event.error" class="action-error">{{messageError(event.error)}}</p><p v-else-if="event.status==='running'" class="muted">{{data.active?'处理中…':'该次调用没有完成记录'}}</p>
     <div class="call-details">
      <details v-if="!calls[event.id]||hasValue(inputText(calls[event.id]))" @toggle="full(event,$event.target.open)"><summary>本轮输入</summary><p v-if="opening[event.id]" class="muted">读取中…</p><pre v-else-if="calls[event.id]" class="full-call">{{inputText(calls[event.id])}}</pre></details>
      <details v-if="hasValue(event.output)"><summary>完整输出</summary><pre class="full-call">{{pretty(event.output)}}</pre></details>
      <details v-if="hasValue(sessionInfo(event))"><summary>会话与用量</summary><pre class="full-call">{{pretty(sessionInfo(event))}}</pre></details>
     </div>
    </section>
   </template>
   <el-pagination v-if="data.total>data.page_size" v-model:current-page="page" :page-size="data.page_size" :total="data.total" layout="prev,pager,next" @current-change="refresh"/>
  </div>
  </div>
 </component>
</template>
<style scoped>
.session-panel{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}.session-panel-header{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid #edf0ee}.session-panel-header h2{margin:0}.session-panel-header .icon-button{font-size:22px}.session-panel-body{min-height:0;flex:1;overflow-y:auto;overscroll-behavior-y:contain;padding:20px 24px}
:global(.session-drawer){max-width:100vw}:global(.session-drawer .el-drawer__header){margin:0;padding:20px 24px;border-bottom:1px solid #eee;color:#25342f}:global(.session-drawer .el-drawer__body){padding:20px 24px;background:#fff}.session-heading{font-size:13px;color:#777;line-height:1.7;margin-bottom:24px}.session-heading .actions{justify-content:flex-end}.session-turn{margin-bottom:32px}.turn-input{display:flex;align-items:flex-end;flex-direction:column;margin-left:30px;margin-bottom:20px}.message-label{color:#999;font-size:12px;margin-bottom:6px}.input-bubble{margin:0;padding:12px 16px;border-radius:16px;background:#f4f4f4;font-size:14px;line-height:1.8}.assistant-message{font-size:14px;line-height:1.8;white-space:pre-wrap}.tool-call{margin:12px 0;color:#777;font-size:13px}.tool-call summary{cursor:pointer;padding:5px 0}.tool-body{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:12px;margin-top:8px}.tool-body label{font-size:11px;color:#999}.tool-body pre,.full-call{font:12px/1.8 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;max-height:55vh;overflow:auto}.turn-output{margin-top:18px}.call-details{margin-top:8px;color:#777;font-size:12px}.call-details summary:hover{color:#207469}.el-pagination{justify-content:center}summary{cursor:pointer}details{margin:12px 0}
</style>
