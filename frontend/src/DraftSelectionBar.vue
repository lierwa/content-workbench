<script setup>
import { computed } from 'vue'

const props = defineProps({ state: Object, saving: Boolean, readonly: Boolean })
const emit = defineEmits(['change'])
const line = computed(() => props.state.line)
const rounds = computed(() => (props.state.draft_rounds || []).filter(r=>r.method==='ai'))
function status(round) {
 switch (round.status) {
 case 'replenishing': return '消息不足，正在自动补筛，已选内容保留…'
 case 'judging': return 'AI 正在比较本轮已核实的帖子…'
 case 'ready': return '已选 ' + round.ids.length + ' 个，正在入队'
 case 'sent': return round.ids.length + ' 条选题 · ' + (round.production?.work_count || 0) + ' 篇稿件 · ' + (round.production?.label || '制作记录待核对')
 case 'insufficient': return round.error
 case 'empty': return round.choices.length ? 'AI 判断本轮没有值得制作的帖子' : '本轮没有可供 AI 选择的新帖子'
 case 'failed': return (round.retry_from==='judging'?'AI 选稿失败：':'入队未完成：') + round.error
 case 'cancelled': return '选稿已停止。保留当时的取舍，交付情况以稿件记录为准。'
 default: return ''
 }
}
</script>

<template>
 <div v-if="rounds.length" class="draft-automation">
  <div v-for="r in rounds" :key="r.batch_id" class="round-state">
   <span :class="{'action-error':r.status==='failed'}">{{status(r)}}</span>
   <el-button v-if="!readonly && line.auto_draft && r.status==='failed'"  link type="primary" :disabled="saving" @click="emit('change',{action:'retry',batch_id:r.batch_id,revision:r.revision})">重试</el-button>
  </div>
 </div>
</template>

<style scoped>
.draft-automation{margin-bottom:16px;color:#66766e;font-size:13px}.round-state{display:flex;align-items:center;gap:8px;flex-wrap:wrap;line-height:1.6}
</style>
