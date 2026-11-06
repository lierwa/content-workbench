<script setup>
import { ref, onBeforeUnmount, nextTick } from 'vue'

const props=defineProps({trailer:{type:Object,required:true}})
const video=ref(null), opened=ref(false), failed=ref(false)
let player=null, disposed=false
async function play(){
  opened.value=true
  await nextTick()
  try {
    const stream=new URL(props.trailer.stream)
    if(stream.protocol!=='https:' || !stream.hostname.endsWith('.steamstatic.com') || !stream.pathname.endsWith('.m3u8'))throw Error()
    const el=video.value
    if(el.canPlayType('application/vnd.apple.mpegurl')){
      el.src=stream.href
      await el.play()
    }else{
      const {default:Hls}=await import('hls.js')
      if(disposed)return
      if(!Hls.isSupported())throw Error()
      player=new Hls({maxBufferLength:20})
      player.on(Hls.Events.ERROR,(_event,data)=>{if(data.fatal)unavailable()})
      player.on(Hls.Events.MANIFEST_PARSED,()=>{el.play().catch(()=>{})})
      player.loadSource(stream.href)
      player.attachMedia(el)
    }
  }catch{if(!disposed)unavailable()}
}
function unavailable(){player?.destroy();player=null;failed.value=true}
onBeforeUnmount(()=>{disposed=true;player?.destroy()})
</script>
<template>
  <span class="game-trailer">
    <button v-if="!opened" type="button" @click="play">播放官方演示 · {{trailer.title}}</button>
    <video v-if="opened && !failed" ref="video" controls playsinline preload="none" :aria-label="trailer.title" @error="unavailable"></video>
    <span v-if="failed">当前无法在本页播放。</span>
    <a :href="trailer.player" target="_blank" rel="noopener noreferrer">在 Steam 观看官方演示</a>
  </span>
</template>
<style scoped>
.game-trailer{display:block;margin:.9rem 0}.game-trailer button{display:block;border:1px solid #a7bcb3;background:#f4f8f6;color:#254b3c;padding:.6rem .8rem;cursor:pointer}.game-trailer video{display:block;width:100%;aspect-ratio:16/9;background:#111}.game-trailer a{display:inline-block;margin-top:.35rem}
</style>
