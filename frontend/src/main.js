import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import Candidates from './Candidates.vue'
import ProductionWorkspace from './ProductionWorkspace.vue'
import Columns from './Columns.vue'
import Pool from './Pool.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/columns' },
    { path: '/columns', component: Columns },
    { path: '/sources', component: Columns },
    { path: '/pool', component: Pool },
    { path: '/topics', component: Candidates },
    { path: '/drafts', component: ProductionWorkspace },
    { path: '/drafts/new', component: ProductionWorkspace },
    { path: '/drafts/:id', component: ProductionWorkspace },
    { path: '/accounts', component: ProductionWorkspace },
    { path: '/:pathMatch(.*)*', redirect: '/topics' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
