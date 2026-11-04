<script setup>
import { computed, ref, onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from './api'
const data=ref(null), draft=ref({}), original=ref(''), error=ref(''), saving=ref(false)
const dirty=computed(()=>JSON.stringify(draft.value)!==original.value)
const rows=computed(()=>data.value?.catalog.flatMap(group=>group.steps.map((step,i)=>({group:group.id,groupName:group.name,stage:step.id,name:group.steps.length===1?group.name:group.name.includes(step.name)?step.name:`${group.name} · ${step.name}`,span:i===0?group.steps.length:0})))||[])
function span({row,columnIndex}){return columnIndex===1?[row.span,1]:[1,1]}
function modelChanged(group){for(const step of Object.values(draft.value[group].steps))if(!data.value.model_efforts[draft.value[group].model].includes(step.effort))step.effort='high'}
async function load(){try{data.value=await api('/api/ai-settings');draft.value=JSON.parse(JSON.stringify(data.value.groups));original.value=JSON.stringify(draft.value);error.value=''}catch(e){error.value=e.message}}
async function save(){saving.value=true;error.value='';try{data.value=await api('/api/ai-settings',{expected_revision:data.value.revision,groups:draft.value});original.value=JSON.stringify(draft.value);ElMessage.success('已保存，新任务生效')}catch(e){error.value=e.message}finally{saving.value=false}}
onBeforeRouteLeave(async()=>{if(!data.value||!dirty.value)return true;try{await ElMessageBox.confirm('模型设置尚未保存。','未保存',{confirmButtonText:'放弃修改',cancelButtonText:'继续编辑'});return true}catch{return false}})
onMounted(load)
</script>
<template>
 <section>
  <header class="page-heading"><h1>模型设置</h1><el-button type="primary" :disabled="!data||!dirty" :loading="saving" @click="save">保存</el-button></header>
  <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  <el-table v-if="data" :data="rows" :span-method="span" class="model-settings">
   <el-table-column prop="name" label="环节" min-width="240"/>
   <el-table-column label="模型" min-width="240"><template #default="{row}"><el-select v-model="draft[row.group].model" :aria-label="`${row.groupName}模型`" :disabled="saving" @change="modelChanged(row.group)"><el-option v-for="model in data.models" :key="model" :value="model" :label="model"/></el-select></template></el-table-column>
   <el-table-column label="思考深度" min-width="160"><template #default="{row}"><el-select v-model="draft[row.group].steps[row.stage].effort" :aria-label="`${row.name}思考深度`" :disabled="saving"><el-option v-for="effort in data.model_efforts[draft[row.group].model]" :key="effort" :value="effort" :label="effort"/></el-select></template></el-table-column>
  </el-table>
 </section>
</template>
<style scoped>
.model-settings :deep(.el-select){max-width:250px}.model-settings :deep(.cell){padding-top:6px;padding-bottom:6px}
</style>
