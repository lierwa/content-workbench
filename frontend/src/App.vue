<script setup>
import { RouterLink, RouterView } from 'vue-router'
import Icon from './Icon.vue'
import { isPrototype } from './api'
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/columns"><span class="brand-mark">文</span><span>内容工作台</span></RouterLink>
      <span v-if="isPrototype" class="prototype-badge">交互原型</span>
      <nav class="app-nav" aria-label="主导航">
        <RouterLink to="/columns"><Icon name="columns" />栏目</RouterLink>
        <RouterLink to="/pool"><Icon name="sources" />线索池</RouterLink>
        <RouterLink to="/topics"><Icon name="topics" />选题</RouterLink>
        <RouterLink to="/drafts"><Icon name="drafts" />稿件</RouterLink>
        <div class="nav-divider"></div>
        <RouterLink to="/sources"><Icon name="sources" />渠道设置</RouterLink>
        <RouterLink to="/accounts"><Icon name="accounts" />账号</RouterLink>
      </nav>
    </aside>
    <main class="workspace" id="main"><RouterView /></main>
  </div>
</template>
