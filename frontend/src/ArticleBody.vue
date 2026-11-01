<script setup>
import { computed, h } from 'vue'
import { isPrototype } from './api'
const props=defineProps({html:{type:String,default:''}})
const tags=new Set('section div span p br h1 h2 h3 h4 h5 h6 blockquote strong b em i u s del sub sup mark a img figure figcaption ul ol li pre code hr table thead tbody tr th td'.split(' '))
const styles=new Set('font-size font-weight font-style line-height color background-color text-align text-decoration letter-spacing text-indent margin margin-top margin-bottom padding border-left border-color border-width border-style'.split(' '))
function webUrl(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password?u.href:null}catch{return null}}
function nodes(root){return [...root.childNodes].flatMap(node=>{
  if(node.nodeType===Node.TEXT_NODE)return [node.textContent]
  if(node.nodeType!==Node.ELEMENT_NODE)return []
  const tag=node.tagName.toLowerCase();if(!tags.has(tag))return []
  const attrs={}
  if(tag==='img'){const src=node.getAttribute('src')||'';if(!/^\/api\/production\/media\/[\w-]+\.(png|jpg|jpeg)$/.test(src)&&!(isPrototype&&/^\.\/prototype-media\/[\w.-]+$/.test(src)))return [];attrs.src=src;attrs.alt=node.getAttribute('alt')||'文章配图'}
  if(tag==='a'){attrs.href=webUrl(node.getAttribute('href'));attrs.target='_blank';attrs.rel='noopener noreferrer'}
  for(const key of ['colspan','rowspan','start']){const value=node.getAttribute(key);if(value&&/^[1-9]\d{0,3}$/.test(value))attrs[key]=value}
  attrs.style=Object.fromEntries([...node.style].filter(key=>styles.has(key)).map(key=>[key,node.style.getPropertyValue(key)]))
  return [h(tag,attrs,tag==='img'?undefined:nodes(node))]
})}
const content=computed(()=>nodes(new DOMParser().parseFromString(props.html,'text/html').body))
const render=()=>h('div',{class:'article-body'},content.value)
</script>
<template><component :is="render"/></template>
