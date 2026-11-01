<script setup>
defineProps({ name: String, size: { type: Number, default: 18 } })
const paths = {
  columns: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  topics: 'M4 4h16v16H4z M8 8h8 M8 12h8 M8 16h5',
  drafts: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',
  sources: 'M8 12h8 M9 5H6a7 7 0 0 0 0 14h3 M15 5h3a7 7 0 0 1 0 14h-3',
  accounts: 'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2 M12 3a4 4 0 1 0 0 8a4 4 0 1 0 0-8',
  plus: 'M12 5v14 M5 12h14',
  arrow: 'M5 12h14 M13 6l6 6-6 6',
  back: 'M19 12H5 M11 6l-6 6 6 6',
  check: 'M5 12l4 4L19 6',
  edit: 'M16 3l5 5-12 12H4v-5z M14 5l5 5',
  image: 'M3 3h18v18H3z M3 16l6-6 5 5 3-3 4 4 M16 7h.01',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
  search: 'M21 21l-5-5 M10 3a7 7 0 1 0 0 14a7 7 0 1 0 0-14',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.drafts" /></svg></template>
