<script setup>
import Icon from './Icon.vue'
defineProps({label:String,icon:String,active:Boolean,disabled:Boolean,shortcut:String})
const emit=defineEmits(['activate'])
</script>
<template><el-tooltip :content="shortcut?`${label} · ${shortcut}`:label" :show-after="400"><button type="button" class="editor-tool" :aria-label="label" :aria-pressed="active" :disabled="disabled" :class="{active}" @mousedown.prevent @click="emit('activate')"><Icon :name="icon" :size="18"/></button></el-tooltip></template>
