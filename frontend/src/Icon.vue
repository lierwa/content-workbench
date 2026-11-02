<script setup>
defineProps({ name: String, size: { type: Number, default: 18 } })
const paths = {
  undo: 'M3 10h10a7 7 0 0 1 0 14 M3 10l5-5 M3 10l5 5',
  redo: 'M21 10H11a7 7 0 0 0 0 14 M21 10l-5-5 M21 10l-5 5',
  bold: 'M6 3h7a4.5 4.5 0 0 1 0 9H6z M6 12h8a4.5 4.5 0 0 1 0 9H6z',
  italic: 'M10 3h10 M4 21h10 M15 3L9 21',
  underline: 'M6 3v7a6 6 0 0 0 12 0V3 M4 21h16',
  strike: 'M17 5c-3-4-10-2-10 2 0 2 1 3 5 4 M4 12h16 M7 19c4 4 11 1 10-3',
  quote: 'M3 5h7v7H3z M10 12c0 4-2 6-5 7 M14 5h7v7h-7z M21 12c0 4-2 6-5 7',
  list: 'M9 5h12 M9 12h12 M9 19h12 M3 5h.01 M3 12h.01 M3 19h.01',
  align: 'M4 4h16 M4 9h10 M4 14h16 M4 19h10',
  eraser: 'M16 3l5 5-12 12H5l-4-4z M8 9l7 7 M9 20h12',
  chevron: 'M6 9l6 6 6-6',
  trash: 'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  table: 'M3 3h18v18H3z M3 9h18 M3 15h18 M9 3v18 M15 3v18',
  columns: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  topics: 'M4 4h16v16H4z M8 8h8 M8 12h8 M8 16h5',
  process: 'M3 5l2 2 3-3 M11 5h10 M3 12l2 2 3-3 M11 12h10 M5 19h.01 M11 19h10',
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
  filter: 'M3 4h18l-7 8v6l-4 2v-8z',
}
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name] || paths.drafts" /></svg></template>
