import { reactive, watch } from 'vue'
import { isPrototype } from './api'
const storageKey=isPrototype?'workbench-prototype-selection':'workbench-selection'

const defaults = { batchId: null, lineId: 'hotspots', format: 'single', events: [], targets: [], selections: {} }
let saved
try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null') } catch { saved = null }
export const creation = reactive({ ...defaults, ...saved })
creation.selections ||= {}
export function rememberSelection() {
  const { batchId, lineId, format, events, targets } = creation
  creation.selections[lineId] = { batchId, format, events:events.map(e => ({ ...e })), targets:[...targets] }
}
export function switchSelection(lineId) {
  rememberSelection()
  const saved=creation.selections[lineId]
  Object.assign(creation, { batchId:saved?.batchId || null, format:saved?.format || 'single',
    events:saved?.events?.map(e => ({ ...e })) || [], targets:saved?.targets ? [...saved.targets] : [], lineId })
}
watch(creation, value => {
  try { sessionStorage.setItem(storageKey, JSON.stringify(value)) } catch { /* Selection remains usable in memory. */ }
}, { deep: true, flush: 'sync' })
