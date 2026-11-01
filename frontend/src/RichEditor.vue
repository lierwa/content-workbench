<script setup>
import { watch, ref, onMounted, onBeforeUnmount } from 'vue'
import Icon from './Icon.vue'
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
  extensions: [StarterKit.configure({ link: { openOnClick: false, HTMLAttributes: { rel: 'noopener noreferrer' } } }), ArticleImage.configure({ allowBase64: false }), TableKit, TextStyle, FontSize, Color, Figure, Figcaption, BlockStyle],
  onUpdate: ({ editor: instance }) => emit('update:modelValue', instance.getHTML()),
})
watch(() => props.disabled, value => editor.value?.setEditable(!value))
watch(() => props.modelValue, value => { if (editor.value && value !== editor.value.getHTML()) editor.value.commands.setContent(value || '', { emitUpdate: false }) })
const linkOpen=ref(false),linkUrl=ref(''),linkError=ref('')
function openLink(){linkUrl.value=editor.value.getAttributes('link').href||'';linkError.value='';linkOpen.value=true}
function setLink(){try{const parsed=new URL(linkUrl.value);if(!['http:','https:'].includes(parsed.protocol)||parsed.username||parsed.password)throw new Error();editor.value.chain().focus().extendMarkRange('link').setLink({href:parsed.href}).run();linkOpen.value=false}catch{linkError.value='请输入有效的 http 或 https 链接'}}
function removeLink(){editor.value.chain().focus().extendMarkRange('link').unsetLink().run();linkOpen.value=false}
function setParagraphStyle(property, value) {
  const instance = editor.value
  const type = instance.isActive('heading') ? 'heading' : 'paragraph'
  const existing = instance.getAttributes(type).style || ''
  const styles = existing.split(';').map(x => x.trim()).filter(Boolean).filter(x => x.split(':')[0].trim().toLowerCase() !== property)
  if (value) styles.push(`${property}: ${value}`)
  instance.chain().focus().updateAttributes(type, { style: styles.join('; ') }).run()
}
function insertFigure(url) { editor.value?.chain().focus().insertContent({ type: 'figure', content: [{ type: 'image', attrs: { src: url, alt: '' } }, { type: 'figcaption', content: [{ type: 'text', text: '点击编辑图片说明' }] }] }).run() }
defineExpose({ insertFigure })
</script>

<template>
  <div class="rich-editor" :class="{ 'toutiao-native': presentationMode === 'toutiao_native' }">
    <div v-if="editor" class="editor-toolbar" :style="{top:toolbarTop+'px'}" role="toolbar" aria-label="正文格式工具">
      <div class="toolbar-group">
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('paragraph') }" @click="editor.chain().focus().setParagraph().run()">正文</button>
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('heading', { level: 2 }) }" @click="editor.chain().focus().toggleHeading({ level: 2 }).run()">小标题</button>
      </div><div class="toolbar-group">
      <button type="button" :disabled="disabled" :aria-pressed="editor.isActive('bold')" :class="{ active: editor.isActive('bold') }" @click="editor.chain().focus().toggleBold().run()"><strong>加粗</strong></button>
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('italic') }" @click="editor.chain().focus().toggleItalic().run()"><em>斜体</em></button>
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('blockquote') }" @click="editor.chain().focus().toggleBlockquote().run()">引用</button>
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('bulletList') }" @click="editor.chain().focus().toggleBulletList().run()">列表</button>
      <button type="button" :disabled="disabled" :class="{ active: editor.isActive('orderedList') }" @click="editor.chain().focus().toggleOrderedList().run()">编号</button>
      </div><div class="toolbar-group">
      <button type="button" :disabled="disabled" @click="openLink">链接</button>
      <label class="editor-image" :class="{ disabled }"><Icon name="image" :size="16"/>配图<input type="file" aria-label="插入配图" accept="image/png,image/jpeg" :disabled="disabled" @change="emit('image', $event)" /></label>
      </div>
      <template v-if="platform === 'wechat'"><select aria-label="字号" :disabled="disabled" @change="editor.chain().focus().setFontSize($event.target.value).run()"><option value="">字号</option><option value="14px">14</option><option value="16px">16</option><option value="18px">18</option><option value="20px">20</option><option value="24px">24</option></select><select aria-label="行距" :disabled="disabled" @change="setParagraphStyle('line-height', $event.target.value)"><option value="">行距</option><option value="1.5">1.5</option><option value="1.8">1.8</option><option value="2">2</option></select><select aria-label="段后留白" :disabled="disabled" @change="setParagraphStyle('margin-bottom', $event.target.value)"><option value="">段距</option><option value="8px">8px</option><option value="16px">16px</option><option value="24px">24px</option><option value="32px">32px</option></select></template>
      <div class="toolbar-group toolbar-history"><button type="button" :disabled="disabled || !editor.can().undo()" @click="editor.chain().focus().undo().run()">撤销</button><button type="button" :disabled="disabled || !editor.can().redo()" @click="editor.chain().focus().redo().run()">重做</button></div>
    </div>
    <EditorContent :editor="editor" class="editor-content" />
    <el-dialog v-model="linkOpen" title="编辑链接" width="440px" append-to-body><el-form label-position="top" @submit.prevent="setLink"><el-form-item label="链接地址"><el-input v-model="linkUrl" aria-label="链接地址" placeholder="https://"/></el-form-item></el-form><p v-if="linkError" class="action-error">{{linkError}}</p><template #footer><el-button @click="removeLink">移除链接</el-button><el-button @click="linkOpen=false">取消</el-button><el-button type="primary" :disabled="!linkUrl.trim()" @click="setLink">确定</el-button></template></el-dialog>
  </div>
</template>
