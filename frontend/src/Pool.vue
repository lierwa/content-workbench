<script setup>
import { onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from './api'
const route=useRoute(), router=useRouter()
const filters=reactive(Object.fromEntries(['q','category','line_id','source_id','status','hours'].map(k=>[k,String(route.query[k]||'')])))
const page=ref(1), data=ref({items:[],total:0,pool_total:0,lines:[],sources:[],categories:[],statuses:{}})
const loading=ref(true), sending=ref(false), error=ref(''), detail=ref(null)
let sequence=0, timer, debounce, disposed=false
function params(){return new URLSearchParams({...filters,page:page.value,page_size:30}).toString()}
async function refresh(){
  const id=++sequence
  try{const next=await api('/api/pool?'+params());if(id===sequence&&!disposed){data.value=next;error.value=''}}
  catch(e){if(id===sequence&&!disposed)error.value=e.message}
  finally{if(id===sequence&&!disposed)loading.value=false}
}
async function poll(){await refresh();if(!disposed)timer=setTimeout(poll,6000)}
function clear(){for(const key in filters)filters[key]=''}
async function collect(){sending.value=true;try{await api('/api/collect',filters.source_id?{source_ids:[filters.source_id]}:filters.line_id?{line_id:filters.line_id}:{});await refresh()}catch(e){error.value=e.message}finally{sending.value=false}}
function stamp(value){if(!value)return '—';const d=new Date(value);return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}
function link(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password?u.href:null}catch{return null}}
function badge(status){return status==='selected'?'success':['insufficient','stale'].includes(status)?'warning':status==='running'?'progress':'neutral'}
watch(filters,()=>{page.value=1;clearTimeout(debounce);router.replace({query:Object.fromEntries(Object.entries(filters).filter(([,v])=>v))});debounce=setTimeout(refresh,200)})
watch(()=>route.query,query=>{for(const key in filters){const value=String(query[key]||'');if(filters[key]!==value)filters[key]=value}})
watch(page,refresh)
onMounted(poll)
onBeforeUnmount(()=>{disposed=true;sequence++;clearTimeout(timer);clearTimeout(debounce)})
</script>

<template>
  <header class="page-heading"><div class="actions"><h1>线索池</h1><span class="muted">{{data.pool_total}} 条</span></div><div class="actions"><RouterLink to="/sources">渠道设置</RouterLink><el-button :disabled="sending||data.collecting" @click="collect">{{data.collecting?'采集中':'采集'}}</el-button></div></header>
  <section class="pool-filters surface" aria-label="线索筛选">
    <el-input v-model="filters.q" placeholder="搜索标题、摘要" aria-label="搜索线索" clearable class="pool-search" />
    <el-select v-model="filters.category" placeholder="来源分类" aria-label="来源分类" clearable><el-option v-for="name in data.categories" :key="name" :label="name" :value="name" /></el-select>
    <el-select v-model="filters.source_id" placeholder="全部来源" aria-label="采集来源" clearable filterable><el-option v-for="s in data.sources" :key="s.id" :label="s.name" :value="s.id" /></el-select>
    <el-select v-model="filters.line_id" placeholder="全部栏目" aria-label="所属栏目" clearable><el-option v-for="l in data.lines" :key="l.id" :label="l.name" :value="l.id" /></el-select>
    <el-select v-model="filters.status" placeholder="处理状态" aria-label="处理状态" clearable><el-option v-for="(name,id) in data.statuses" :key="id" :label="name" :value="id" /></el-select>
    <el-select v-model="filters.hours" placeholder="入池时间" aria-label="入池时间" clearable><el-option label="最近 24 小时" value="24" /><el-option label="最近 48 小时" value="48" /><el-option label="最近 7 天" value="168" /></el-select>
    <el-button link @click="clear">重置</el-button>
  </section>
  <div v-if="error" class="error-retry"><el-alert :title="error" type="error" :closable="false" /><el-button @click="refresh">重试</el-button></div>
  <div class="pool-count muted">{{data.total}} 条符合条件</div>
  <el-table :data="data.items" row-key="ref" class="pool-table" :empty-text="loading?'加载中…':'没有符合条件的线索'" @row-click="row=>detail=row">
    <el-table-column label="线索" min-width="300"><template #default="{row}"><button class="candidate-title" @click.stop="detail=row">{{row.title}}</button><div class="muted pool-meta">{{row.source_name}}<span class="dot">·</span>{{row.category}}</div></template></el-table-column>
    <el-table-column label="栏目 / 处理状态" min-width="190"><template #default="{row}"><div v-for="d in row.decisions" :key="d.line_id" class="pool-decision"><span>{{d.name}}</span><span class="status-pill" :class="badge(d.status)">{{d.label}}</span></div><span v-if="!row.decisions.length" class="muted">未分配栏目</span></template></el-table-column>
    <el-table-column label="发布时间" width="116"><template #default="{row}">{{stamp(row.published_at)}}</template></el-table-column>
    <el-table-column label="入池时间" width="116"><template #default="{row}">{{stamp(row.first_seen_at)}}</template></el-table-column>
  </el-table>
  <div class="pool-mobile-list">
    <button v-for="row in data.items" :key="row.ref" class="pool-mobile-row" @click="detail=row">
      <strong>{{row.title}}</strong><span class="muted">{{row.source_name}} · {{row.category}}</span>
      <span v-for="d in row.decisions" :key="d.line_id" class="pool-decision"><span>{{d.name}}</span><span class="status-pill" :class="badge(d.status)">{{d.label}}</span></span>
      <span v-if="!row.decisions.length" class="muted">未分配栏目</span>
      <span class="muted">发布 {{stamp(row.published_at)}} · 入池 {{stamp(row.first_seen_at)}}</span>
    </button>
    <div v-if="!data.items.length" class="empty-state">{{loading?'加载中…':'没有符合条件的线索'}}</div>
  </div>
  <div v-if="data.total>30" class="pagination"><el-pagination v-model:current-page="page" :page-size="30" :total="data.total" layout="prev, pager, next" /></div>
  <el-drawer :model-value="!!detail" title="线索详情" size="560px" @close="detail=null">
    <template v-if="detail"><h2 class="detail-title">{{detail.title}}</h2><div class="muted">{{detail.source_name}} · {{detail.category}}</div><p class="pool-summary">{{detail.summary||'暂无摘要'}}</p><a v-if="link(detail.entry_url)" :href="link(detail.entry_url)" target="_blank" rel="noopener noreferrer">打开采集入口 ↗</a><div v-for="d in detail.decisions" :key="d.line_id" class="source-entry"><div class="pool-decision"><strong>{{d.name}}</strong><span class="status-pill" :class="badge(d.status)">{{d.label}}</span></div><p v-if="d.reason" class="pool-summary">{{d.reason}}</p><RouterLink :to="{path:'/topics',query:{column:d.line_id}}">查看栏目选题 →</RouterLink></div></template>
  </el-drawer>
</template>

<style scoped>
.pool-filters{display:flex;gap:10px;flex-wrap:wrap;padding:16px;margin-bottom:18px}.pool-filters .el-select{width:144px}.pool-search{width:240px}.pool-count{margin:0 0 12px}.pool-meta{margin-top:9px}.pool-decision{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:12px}.pool-summary{white-space:pre-wrap;font-size:14px;line-height:1.8;margin:20px 0}.pool-table{border:1px solid #e2e8e5;border-radius:10px}.pool-table :deep(.el-table__row){cursor:pointer}.pool-mobile-list{display:none}@media(max-width:580px){.pool-filters .el-select{width:calc(50% - 5px)}.pool-search{width:100%}.pool-filters{padding:12px}.pool-table{display:none}.pool-mobile-list{display:grid;gap:12px}.pool-mobile-row{display:flex;flex-direction:column;align-items:flex-start;gap:10px;text-align:left;border:1px solid #e2e8e5;border-radius:10px;background:white;padding:18px;color:inherit;min-width:0}.pool-mobile-row strong{font-size:15px;line-height:1.7;font-weight:550;overflow-wrap:anywhere}.pool-mobile-row .pool-decision{margin:0}}
</style>
