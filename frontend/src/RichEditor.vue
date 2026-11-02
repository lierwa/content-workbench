<script setup>
import { watch, ref, onMounted, onBeforeUnmount } from 'vue'
import Icon from './Icon.vue'
import EditorTool from './EditorTool.vue'
import { useEditor, EditorContent } from '@tiptap/vue-3'
import { Node, Extension, mergeAttributes } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import Image from '@tiptap/extension-image'
import { TableKit } from '@tiptap/extension-table'
import { TextStyle, FontSize, Color } from '@tiptap/extension-text-style'

const props = defineProps({ modelValue: { type: String, default: '' }, disabled: Boolean, platform: String, presentationMode: String })
const emit = defineEmits(['update:modelValue', 'image'])
const toolbarTop=ref(70)
let headingObserver
onMounted(()=>{const heading=document.querySelector('.editor-heading');if(heading){headingObserver=new ResizeObserver(()=>{toolbarTop.value=heading.getBoundingClientRect().height});headingObserver.observe(heading)}})
onBeforeUnmount(()=>headingObserver?.disconnect())

// Preserve safe layout attributes through the editor; the server is the authority
// that filters values and renders platform HTML before preview or publishing.
const BlockStyle = Extension.create({
  name: 'blockStyle',
  addGlobalAttributes() {
    return [{ types: ['paragraph', 'heading', 'blockquote', 'bulletList', 'orderedList', 'listItem', 'table', 'tableRow', 'tableCell', 'tableHeader', 'image', 'figure', 'figcaption', 'textStyle'], attributes: {
      style: { default: null, parseHTML: element => element.getAttribute('style'), renderHTML: attributes => attributes.style ? { style: attributes.style } : {} },
    } }]
  },
})
const Figure = Node.create({ name: 'figure', group: 'block', content: 'image figcaption?', defining: true, parseHTML: () => [{ tag: 'figure' }], renderHTML: ({ HTMLAttributes }) => ['figure', mergeAttributes(HTMLAttributes), 0] })
const Figcaption = Node.create({ name: 'figcaption', content: 'inline*', defining: true, parseHTML: () => [{ tag: 'figcaption' }], renderHTML: ({ HTMLAttributes }) => ['figcaption', mergeAttributes(HTMLAttributes), 0] })
const ArticleImage = Image.extend({ addAttributes() { return { ...this.parent?.(), width: { default: null }, height: { default: null } } } })
const editor = useEditor({
  content: props.modelValue,
  editable: !props.disabled,
  editorProps:{attributes:{role:'textbox','aria-label':'文章正文','aria-multiline':'true'}},
  extensions: [StarterKit.configure({ link: { openOnClick: false, HTMLAttributes: { rel: 'noopener noreferrer' } } }), ArticleImage.configure({ allowBase64: false }), TableKit, TextStyle, FontSize, Color, Figure, Figcaption, BlockStyle],
  onUpdate: ({ editor: instance }) => emit('update:modelValue', instance.getHTML()),
})
watch(() => props.disabled, value => editor.value?.setEditable(!value))
watch(() => props.modelValue, value => { if (editor.value && value !== editor.value.getHTML()) editor.value.commands.setContent(value || '', { emitUpdate: false }) })
const linkOpen=ref(false),linkUrl=ref(''),linkError=ref('')
function openLink(){linkUrl.value=editor.value.getAttributes('link').href||'';linkError.value='';linkOpen.value=true}
function setLink(){try{const parsed=new URL(linkUrl.value);if(!['http:','https:'].includes(parsed.protocol)||parsed.username||parsed.password)throw new Error();if(editor.value.state.selection.empty&&!editor.value.isActive('link'))editor.value.chain().focus().insertContent({type:'text',text:parsed.href,marks:[{type:'link',attrs:{href:parsed.href}}]}).run();else editor.value.chain().focus().extendMarkRange('link').setLink({href:parsed.href}).run();linkOpen.value=false}catch{linkError.value='请输入有效的 http 或 https 链接'}}
function removeLink(){editor.value.chain().focus().extendMarkRange('link').unsetLink().run();linkOpen.value=false}
function setParagraphStyle(property, value) {
  const instance = editor.value
  const type = instance.isActive('heading') ? 'heading' : 'paragraph'
  const existing = instance.getAttributes(type).style || ''
  const styles = existing.split(';').map(x => x.trim()).filter(Boolean).filter(x => x.split(':')[0].trim().toLowerCase() !== property)
  if (value) styles.push(`${property}: ${value}`)
  instance.chain().focus().updateAttributes(type, { style: styles.join('; ') }).run()
}
function insertFigure(url) {
 const instance=editor.value
 const selection=instance.state.selection
 if(instance.isActive('image')){
  const parent=selection.$from.parent
  if(parent.type.name==='figure')instance.chain().focus().setNodeSelection(selection.$from.before()).deleteSelection().run()
  else instance.chain().focus().deleteSelection().run()
 }
 instance.chain().focus().insertContent({type:'figure',content:[{type:'image',attrs:{src:url,alt:''}},{type:'figcaption',content:[]}]}).run()
}
function removeImage(){const selection=editor.value.state.selection;if(selection.$from.parent.type.name==='figure')editor.value.chain().focus().setNodeSelection(selection.$from.before()).deleteSelection().run();else editor.value.chain().focus().deleteSelection().run()}
function focusText(text){
 let range=null
 editor.value?.state.doc.descendants((node,pos)=>{if(range||!node.isTextblock)return;const index=node.textContent.indexOf(text);if(index>=0)range={from:pos+1+index,to:pos+1+index+text.length}})
 if(range)editor.value.chain().focus().setTextSelection(range).scrollIntoView().run()
}
function formatBlock(level){const chain=editor.value.chain().focus();level?chain.setHeading({level}).run():chain.setParagraph().run()}
function listStyle(kind){const chain=editor.value.chain().focus();kind==='bullet'?chain.toggleBulletList().run():chain.toggleOrderedList().run()}
function tableAction(action){const chain=editor.value.chain().focus();if(action==='insert')chain.insertTable({rows:3,cols:3,withHeaderRow:true}).run();else chain[action]().run()}
function navigateTools(event){
 if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return
 const buttons=[...event.currentTarget.querySelectorAll('button:not(:disabled),input:not(:disabled)')]
 const index=buttons.indexOf(document.activeElement);if(index<0)return
 event.preventDefault();buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length]?.focus()
}
defineExpose({insertFigure,focusText})
</script>

<template>
 <div class="rich-editor" :class="{'toutiao-native':presentationMode==='toutiao_native'}">
  <div v-if="editor" class="editor-toolbar" :style="{top:toolbarTop+'px'}" role="toolbar" aria-label="正文格式工具" @keydown="navigateTools">
   <div class="toolbar-group" role="group" aria-label="历史操作"><EditorTool label="撤销" icon="undo" shortcut="Ctrl / ⌘ Z" :disabled="disabled||!editor.can().undo()" @activate="editor.chain().focus().undo().run()"/><EditorTool label="重做" icon="redo" shortcut="Ctrl / ⌘ Shift Z" :disabled="disabled||!editor.can().redo()" @activate="editor.chain().focus().redo().run()"/></div>
   <div class="toolbar-group" role="group" aria-label="段落样式">
    <el-dropdown trigger="click" :disabled="disabled" @command="formatBlock"><button type="button" class="editor-tool style-tool" :disabled="disabled" aria-label="段落样式">{{editor.isActive('heading',{level:2})?'标题 2':editor.isActive('heading',{level:3})?'标题 3':'正文'}}<Icon name="chevron" :size="12"/></button><template #dropdown><el-dropdown-menu><el-dropdown-item :command="0">正文</el-dropdown-item><el-dropdown-item :command="2">标题 2</el-dropdown-item><el-dropdown-item :command="3">标题 3</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
    <el-dropdown trigger="click" :disabled="disabled" @command="size=>editor.chain().focus().setFontSize(size).run()"><button class="editor-tool style-tool font-tool" type="button" :disabled="disabled" aria-label="字号">{{editor.getAttributes('textStyle').fontSize?.replace('px','')||'字号'}}<Icon name="chevron" :size="12"/></button><template #dropdown><el-dropdown-menu><el-dropdown-item v-for="size in [14,16,18,20,24]" :key="size" :command="size+'px'">{{size}}</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
   </div>
   <div class="toolbar-group" role="group" aria-label="文字格式"><EditorTool v-for="tool in [{name:'bold',label:'加粗',key:'B'},{name:'italic',label:'斜体',key:'I'},{name:'underline',label:'下划线',key:'U'},{name:'strike',label:'删除线'}]" :key="tool.name" :label="tool.label" :icon="tool.name" :shortcut="tool.key?`Ctrl / ⌘ ${tool.key}`:''" :active="editor.isActive(tool.name)" :disabled="disabled" @activate="editor.chain().focus().toggleMark(tool.name).run()"/></div>
   <div class="toolbar-group" role="group" aria-label="段落排版">
    <el-dropdown trigger="click" :disabled="disabled" @command="listStyle"><button class="editor-tool" :class="{active:editor.isActive('bulletList')||editor.isActive('orderedList')}" type="button" :disabled="disabled" aria-label="列表样式"><Icon name="list" :size="18"/><Icon name="chevron" :size="10"/></button><template #dropdown><el-dropdown-menu><el-dropdown-item command="bullet">无序列表</el-dropdown-item><el-dropdown-item command="ordered">有序列表</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
    <EditorTool label="引用" icon="quote" :active="editor.isActive('blockquote')" :disabled="disabled" @activate="editor.chain().focus().toggleBlockquote().run()"/>
    <el-dropdown trigger="click" :disabled="disabled" @command="value=>setParagraphStyle('text-align',value)"><button type="button" class="editor-tool" :disabled="disabled" aria-label="段落对齐"><Icon name="align" :size="18"/><Icon name="chevron" :size="10"/></button><template #dropdown><el-dropdown-menu><el-dropdown-item command="left">左对齐</el-dropdown-item><el-dropdown-item command="center">居中</el-dropdown-item><el-dropdown-item command="right">右对齐</el-dropdown-item><el-dropdown-item command="justify">两端对齐</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
   </div>
   <div class="toolbar-group" role="group" aria-label="插入内容"><EditorTool label="编辑链接" icon="sources" :active="editor.isActive('link')" :disabled="disabled" @activate="openLink"/>
    <el-tooltip content="插入或替换图片" :show-after="400"><label class="editor-tool editor-image" :class="{disabled}"><Icon name="image" :size="18"/><input type="file" aria-label="插入或替换图片" accept="image/png,image/jpeg" :disabled="disabled" @change="emit('image',$event)"/></label></el-tooltip>
    <el-dropdown trigger="click" :disabled="disabled" @command="tableAction"><button class="editor-tool" type="button" :disabled="disabled" aria-label="表格"><Icon name="table" :size="18"/></button><template #dropdown><el-dropdown-menu><el-dropdown-item v-if="!editor.isActive('table')" command="insert">插入表格</el-dropdown-item><template v-else><el-dropdown-item command="addRowAfter">增加一行</el-dropdown-item><el-dropdown-item command="addColumnAfter">增加一列</el-dropdown-item><el-dropdown-item command="deleteRow">删除当前行</el-dropdown-item><el-dropdown-item command="deleteColumn">删除当前列</el-dropdown-item><el-dropdown-item command="deleteTable" divided>删除表格</el-dropdown-item></template></el-dropdown-menu></template></el-dropdown>
   </div>
   <EditorTool v-if="editor.isActive('image')" label="删除所选图片" icon="trash" :disabled="disabled" @activate="removeImage"/>
   <EditorTool label="清除格式" icon="eraser" :disabled="disabled" @activate="editor.chain().focus().unsetAllMarks().clearNodes().updateAttributes('paragraph',{style:null}).run()"/>
  </div>
  <slot name="title"/>
  <EditorContent :editor="editor" class="editor-content"/>
  <el-dialog v-model="linkOpen" title="编辑链接" width="440px" append-to-body><el-form label-position="top" @submit.prevent="setLink"><el-form-item label="链接地址"><el-input v-model="linkUrl" aria-label="链接地址" placeholder="https://"/></el-form-item></el-form><p v-if="linkError" class="action-error">{{linkError}}</p><template #footer><div class="dialog-actions"><el-button v-if="editor?.isActive('link')" text @click="removeLink">移除链接</el-button><div class="actions"><el-button @click="linkOpen=false">取消</el-button><el-button type="primary" :disabled="!linkUrl.trim()" @click="setLink">应用</el-button></div></div></template></el-dialog>
 </div>
</template>
