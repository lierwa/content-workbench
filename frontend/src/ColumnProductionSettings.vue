<script setup>
defineProps({form:Object, section:String})
const stages = [['research','补证要求'],['editing','返修要求'],['review','独立审稿要求'],['images','配图要求'],['layout','排版要求']]
</script>

<template>
  <div v-if="section==='production'">
    <el-form-item label="筛选完成后自动选稿制作"><el-switch v-model="form.auto_draft" aria-label="筛选完成后自动选稿制作" /></el-form-item>
    <el-form-item v-if="form.format==='single'" label="每轮最多制作"><el-input-number v-model="form.draft_limit" :min="1" :max="50" :precision="0" aria-label="每轮最多制作篇数" /> 篇</el-form-item>
    <el-form-item label="选稿要求"><el-input v-model="form.draft_rules" type="textarea" :rows="4" maxlength="10000" aria-label="选稿要求" /></el-form-item>
    <el-form-item label="创作要求"><el-input v-model="form.writing_rules" type="textarea" :rows="4" maxlength="10000" aria-label="创作要求" /></el-form-item>
    <el-form-item v-for="[stage,label] in stages" :key="stage" :label="label"><el-input v-model="form.production_rules[stage]" type="textarea" :rows="3" maxlength="10000" :aria-label="label" placeholder="填写本栏目的具体要求；留空沿用通用要求" /></el-form-item>
  </div>
  <div v-if="section==='format'">
    <template v-if="form.format==='digest'">
      <el-form-item label="整期标题"><el-radio-group v-model="form.format_policy.title_style"><el-radio value="headlines">并列两至三条消息</el-radio><el-radio value="lead">以头条消息命名</el-radio></el-radio-group></el-form-item>
      <div class="policy-grid">
        <el-form-item label="最少消息数"><el-input-number v-model="form.format_policy.min_items" :min="2" :max="50" :precision="0" aria-label="最少消息数" /></el-form-item>
        <el-form-item label="目标消息数"><el-input-number v-model="form.format_policy.target_items" :min="2" :max="50" :precision="0" aria-label="目标消息数" /></el-form-item>
        <el-form-item label="最多消息数"><el-input-number v-model="form.format_policy.max_items" :min="2" :max="50" :precision="0" aria-label="最多消息数" /></el-form-item>
      </div>
      <el-form-item label="消息不足时"><el-radio-group v-model="form.format_policy.shortage"><el-radio value="hold">自动补筛；确实不足则保留候选并报告未完成</el-radio><el-radio value="allow">允许少于下限成稿（至少两条）</el-radio></el-radio-group></el-form-item>
    </template>
    <div class="policy-grid">
      <el-form-item :label="form.format==='digest'?'每条最少字数':'正文最少字数'"><el-input-number v-model="form.format_policy.item_min_chars" :min="0" :max="10000" :precision="0" aria-label="正文最少字数" /></el-form-item>
      <el-form-item :label="form.format==='digest'?'每条最多字数':'正文最多字数'"><el-input-number v-model="form.format_policy.item_max_chars" :min="0" :max="10000" :precision="0" aria-label="正文最多字数" /></el-form-item>
      <el-form-item :label="form.format==='digest'?'每条最多段落':'正文最多段落'"><el-input-number v-model="form.format_policy.max_paragraphs" :min="0" :max="50" :precision="0" aria-label="正文最多段落" /></el-form-item>
    </div>
    <p class="muted">篇幅默认不限；仅有明确篇幅要求时填写字数、段落限制。0 表示不限，短消息不为字数扩写。集合每条保留独立小标题。</p>
    <el-form-item label="正文结构"><el-input v-model="form.format_policy.structure" type="textarea" :rows="4" maxlength="10000" aria-label="正文结构" /></el-form-item>
    <el-form-item label="正文配图"><el-radio-group v-model="form.format_policy.body_images"><el-radio value="none">不配正文图</el-radio><el-radio value="essential">默认无图，仅有必要时配图</el-radio><el-radio value="optional">按内容需要选图</el-radio></el-radio-group></el-form-item>
    <el-form-item v-if="form.format_policy.body_images!=='none'" label="整篇最多正文图"><el-input-number v-model="form.format_policy.max_body_images" :min="0" :max="50" :precision="0" aria-label="整篇最多正文图" /></el-form-item>
    <el-form-item v-if="form.format==='digest'&&form.format_policy.body_images!=='none'" label="每条消息最多图片"><el-input-number v-model="form.format_policy.max_images_per_item" :min="0" :max="50" :precision="0" aria-label="每条消息最多图片" /></el-form-item>
    <el-form-item label="封面"><el-select v-model="form.format_policy.cover" aria-label="封面策略"><el-option value="source_or_default" label="优先报道配图，无合适图片用公共封面"/><el-option value="default" label="使用公共封面"/></el-select></el-form-item>
  </div>
  <div v-if="section==='schedule'">
    <el-form-item label="目标出稿时间（北京时间）"><el-select v-model="form.production_schedule.mode" aria-label="出稿频率"><el-option value="off" label="不定时"/><el-option value="daily" label="每天"/><el-option value="weekly" label="每周"/></el-select></el-form-item>
    <template v-if="form.production_schedule.mode!=='off'">
      <el-form-item v-if="form.production_schedule.mode==='weekly'" label="星期"><el-checkbox-group v-model="form.production_schedule.weekdays"><el-checkbox v-for="(day,i) in ['一','二','三','四','五','六','日']" :key="day" :value="i+1">{{day}}</el-checkbox></el-checkbox-group></el-form-item>
      <el-form-item label="时刻"><el-select v-model="form.production_schedule.times" multiple filterable allow-create default-first-option aria-label="出稿时刻" placeholder="输入 HH:MM 后回车，如 08:00"><el-option v-for="time in ['08:00','12:00','18:00']" :key="time" :value="time" :label="time"/></el-select></el-form-item>
      <el-form-item label="提前开始准备"><el-input-number v-model="form.production_schedule.prepare_minutes" :min="1" :max="1440" :precision="0" aria-label="提前准备分钟数" /> 分钟</el-form-item>
      <el-form-item label="交付到"><el-radio-group v-model="form.production_schedule.delivery"><el-radio value="local">本地稿件</el-radio><el-radio value="draft">目标账号草稿箱</el-radio></el-radio-group></el-form-item>
      <p class="muted">提前从共享池取材并自动选稿、制作、审稿。启用后使用此计划安排筛选。请先在素材池设置持续采集。</p>
      <p class="muted">截止前审稿通过才可同步；错过时刻保留本地稿件，不自动补投。</p>
    </template>
    <el-alert title="目前支持定时出稿与草稿同步；平台公开发布尚未接通。" type="info" :closable="false" />
  </div>
</template>

<style scoped>
.policy-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.muted{font-size:13px;line-height:1.6;margin:0 0 18px}.el-alert{margin-bottom:16px}
</style>
