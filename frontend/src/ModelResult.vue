<script setup>
import { computed, ref } from 'vue'
import ArticleBody from './ArticleBody.vue'
const props=defineProps({value:null,titles:{type:Object,default:()=>({})},entryKey:String,html:String,stage:String,changes:Array})
const excerpt=value=>value.length>180?value.slice(0,180)+'…':value
const v=computed(()=>props.value), expanded=ref(false)
const text=computed(()=>v.value?.markdown||v.value?.body||'')
const title=computed(()=>v.value?.title||props.titles[v.value?.id]||props.titles[v.value?.event_id]||props.titles[props.entryKey]||props.titles[v.value?.canonical_ref]||'')
const rows=computed(()=>{
 const source=Array.isArray(v.value)?v.value:v.value?.items||v.value?.choices||v.value?.selected||v.value?.groups||(v.value&&Object.keys(v.value).length&&Object.keys(v.value).every(k=>k.includes(':'))?v.value:{})
 return Object.entries(source).map(([key,value])=>({key,value}))
})
const ordered=computed(()=>[...rows.value].sort((a,b)=>Number(['advance','keep'].includes(b.value?.decision))-Number(['advance','keep'].includes(a.value?.decision))))
const decisions={advance:'保留',drop:'淘汰',deferred:'暂缓',insufficient:'材料不足',keep:'保留',pass:'通过',revise:'需要修改'}
const summary=computed(()=>{
 if(!rows.value.length)return ''
 if(props.stage==='grouping')return `已归组 ${rows.value.length} 条线索。`
 const kept=rows.value.filter(r=>['advance','keep'].includes(r.value?.decision)).length
 const dropped=rows.value.filter(r=>r.value?.decision==='drop').length
 if(kept||dropped)return `本轮处理 ${rows.value.length} 条，保留 ${kept} 条${dropped?`，淘汰 ${dropped} 条`:''}。`
 if(props.stage==='draft-selection')return `本轮选出 ${rows.value.filter(r=>r.value?.use==='二创').length} 条用于二创。`
 return `选出 ${rows.value.length} 条候选。`
})
const note=computed(()=>v.value?.reason||v.value?.relation_reason||v.value?.contribution||v.value?.summary||v.value?.cover_reason||'')
const generic=computed(()=>!rows.value.length&&!text.value&&!v.value?.issues&&!note.value&&!v.value?.decision&&typeof v.value?.passed!=='boolean'&&!v.value?.blocks)
</script>
<template>
 <div class="model-result">
  <template v-if="typeof v==='string'"><p class="preserve">{{v.length>400&&!expanded?v.slice(0,400)+'…':v}}</p><button v-if="v.length>400" class="expand" @click="expanded=!expanded">{{expanded?'收起':'展开'}}</button></template>
  <template v-else-if="v&&typeof v==='object'">
   <h3 v-if="title">{{title}}</h3>
   <template v-if="text"><template v-if="changes"><p>{{changes.length?`已修改 ${changes.length} 处。`:'正文未改动。'}}</p><div v-for="(change,i) in changes.slice(0,2)" :key="i" class="revision"><p v-if="change.after">{{excerpt(change.after)}}</p><p v-else>已删除该段。</p><details><summary>修改对照</summary><del v-if="change.before">{{change.before}}</del><p v-if="change.after">{{change.after}}</p></details></div><details v-if="changes.length>2"><summary>其余修改</summary><div v-for="(change,i) in changes.slice(2)" :key="i" class="revision"><p v-if="change.after">{{excerpt(change.after)}}</p><p v-else>已删除该段。</p><details><summary>修改对照</summary><del v-if="change.before">{{change.before}}</del><p v-if="change.after">{{change.after}}</p></details></div></details></template><p v-else-if="v.summary">{{v.summary}}</p><p v-else>{{text.slice(0,180)}}{{text.length>180?'…':''}}</p><details><summary>查看稿件</summary><ArticleBody v-if="html" :html="html"/><p v-else class="preserve">{{text}}</p></details></template>
   <template v-else>
    <p v-if="typeof v.passed==='boolean'"><strong>{{v.passed?'审稿通过':v.decision==='insufficient'?'材料不足':'需要修改'}}</strong></p>
    <p v-else-if="v.decision||v.use" class="decision">{{v.use||decisions[v.decision]||v.decision}}</p>
    <p v-if="typeof v.duplicate==='boolean'">{{v.duplicate?'与已有内容重复':'未发现重复内容'}}</p>
    <p v-if="note&&!v.issues?.length">{{note}}</p>
    <div v-for="(issue,i) in v.issues||[]" :key="i" class="review-issue"><details v-if="issue.text"><summary>对应句子</summary><blockquote>{{issue.text}}</blockquote></details><p v-if="issue.reason">{{issue.reason}}</p><p v-if="issue.fix">{{issue.fix}}</p></div>
    <p v-if="v.blocks">已完成 {{v.blocks.length}} 段的排版。</p>
    <p v-if="rows.length">{{summary}}</p>
    <div v-for="row in expanded?ordered:ordered.slice(0,stage==='grouping'?0:2)" :key="row.key" class="result-entry"><ModelResult :value="row.value" :titles="titles" :entry-key="row.key"/></div>
    <button v-if="rows.length>(stage==='grouping'?0:2)" class="expand" @click="expanded=!expanded">{{expanded?'收起':`查看全部 ${rows.length} 条判断`}}</button>
    <p v-if="generic">本轮处理已完成，完整返回见调用详情。</p>
   </template>
  </template>
 </div>
</template>
<style scoped>
:deep(.article-body){font-size:14px;line-height:1.85}:deep(.article-body h2){font-size:16px}.revision{font-size:13px;margin:16px 0}.revision del{display:block;color:#999;margin-bottom:8px;white-space:pre-wrap}.revision p{white-space:pre-wrap}
.model-result{font-size:14px;line-height:1.85;overflow-wrap:anywhere}.model-result h3{font-size:14px;font-weight:600;margin:0 0 6px}.model-result p{margin:0 0 10px}.preserve{white-space:pre-wrap}.decision{font-size:12px;color:#64756d}.review-issue{margin:16px 0}.review-issue blockquote{margin:0 0 8px;padding-left:12px;border-left:2px solid #ddd;color:#666}details,.expand{font-size:12px;color:#777}.expand{border:0;background:none;padding:4px 0;font-family:inherit;cursor:pointer}summary{cursor:pointer}.result-entry{margin-top:16px}
</style>
