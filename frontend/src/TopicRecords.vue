<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from './api'
import ScreenRunDialog from './ScreenRunDialog.vue'
const router=useRouter()
const data=ref({records:[],columns:[],total:0,page_size:20}),page=ref(1),trigger=ref(''),search=ref('')
const loading=ref(true),error=ref(''),open=ref(false),runOnce=ref(false),productionRun=ref(null),starting=ref(false)
const activeStates=['collecting','screening','producing','collecting_more']
const runLabels={collecting:'采集中',collecting_more:'自动补采中',screening:'筛选与补筛中',producing:'制作与审稿中',completed:'已全部出稿',failed:'本轮未完成'}
let timer,disposed=false,sequence=0
const labels={screening:'筛选中',queued:'排队中',completed:'已完成',partial:'部分完成',failed:'失败',stopped:'已停止',ended:'已结束',ready:'待处理'}
const stamp=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'—'
async function refresh(){const token=++sequence;try{const value=await api('/api/topic-records?'+new URLSearchParams({page:page.value,trigger:trigger.value,q:search.value}));if(!disposed&&token===sequence){data.value=value;error.value=''}}catch(e){if(token===sequence)error.value=e.message}finally{if(token===sequence)loading.value=false}}
async function poll(){await refresh();try{const value=await api('/api/production-run');if(!disposed)productionRun.value=value.run}catch(e){if(!disposed)error.value=e.message}if(!disposed)timer=setTimeout(poll,5000)}
async function produce(){starting.value=true;error.value='';try{const config=await api('/api/columns');const ids=config.lines.filter(l=>l.enabled!==false&&l.auto_draft).map(l=>l.id);if(!ids.length)throw new Error('请先启用栏目的自动选稿');productionRun.value=await api('/api/production-run',{line_ids:ids,expected_revision:config.revision});await refresh()}catch(e){error.value=e.message}finally{starting.value=false}}
function filter(){page.value=1;refresh()}
function started(result){if(result?.record_id)router.push('/topics/'+result.record_id);else refresh()}
function show(run){runOnce.value=run;open.value=true}
watch(trigger,filter)
onMounted(poll);onBeforeUnmount(()=>{disposed=true;sequence++;clearTimeout(timer)})
</script>
<template>
 <section>
  <header class="page-heading"><h1>选题记录</h1><div class="actions"><el-button @click="show(false)">选题设置</el-button><el-button @click="show(true)">筛选一次</el-button><el-button type="primary" :loading="starting" :disabled="activeStates.includes(productionRun?.status)" @click="produce">采集并出稿</el-button></div></header>
  <el-alert v-if="productionRun" :title="runLabels[productionRun.status]+(productionRun.error?' · '+productionRun.error:'')" :type="productionRun.status==='failed'?'error':productionRun.status==='completed'?'success':'info'" :closable="false"/>
  <div class="record-filters"><el-select v-model="trigger" clearable placeholder="全部触发方式" aria-label="触发方式"><el-option label="手动" value="manual"/><el-option label="定时" value="automatic"/></el-select><el-input v-model="search" clearable placeholder="搜索日期" aria-label="搜索选题记录" @change="filter" @keyup.enter="filter"/></div>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <el-table :data="data.records" v-loading="loading" row-key="id" empty-text="暂无执行记录" class="records-table" @row-click="row=>router.push('/topics/'+row.id)">
   <el-table-column label="执行时间" min-width="200"><template #default="{row}"><RouterLink :to="'/topics/'+row.id" @click.stop>{{stamp(row.created_at)}}</RouterLink></template></el-table-column>
   <el-table-column label="触发方式" min-width="130"><template #default="{row}">{{{manual:'手动',automatic:'定时',legacy:'未记录'}[row.trigger]}}</template></el-table-column>
   <el-table-column label="状态" min-width="130"><template #default="{row}">{{labels[row.status]||row.status}}</template></el-table-column>
   <el-table-column label="操作" width="100"><template #default="{row}"><RouterLink :to="'/topics/'+row.id" @click.stop>查看</RouterLink></template></el-table-column>
  </el-table>
  <el-pagination v-if="data.total>data.page_size" v-model:current-page="page" :page-size="data.page_size" :total="data.total" layout="prev,pager,next" @current-change="refresh"/>
  <ScreenRunDialog v-model="open" :run-once="runOnce" :lines="data.columns" :initial-ids="data.columns.filter(c=>c.enabled!==false).map(c=>c.id)" @started="started"/>
 </section>
</template>
<style scoped>
.record-filters{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}.record-filters .el-select{width:180px}.record-filters .el-input{width:240px}.records-table :deep(.el-table__row){cursor:pointer}.el-pagination{justify-content:flex-end;margin-top:16px}
</style>
