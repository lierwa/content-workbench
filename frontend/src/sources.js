export function sourceLinks(materials = []) {
  const seen = new Set()
  return materials.flatMap(m => {
    try {
      const url = new URL(m.final_url || m.url)
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || seen.has(url.href)) return []
      seen.add(url.href)
      return [{ title: m.title || m.publisher || url.hostname, url: url.href, publisher: m.publisher || url.hostname }]
    } catch { return [] }
  })
}

export function hotlistLinks(items = []) {
  const seen = new Set()
  return items.flatMap(item => {
    const link = sourceLinks([item])[0]
    if (!link || seen.has(link.url)) return []
    seen.add(link.url)
    const channel = (item.source_name || item.source_id || '来源').replace(/[（(]TopHub[）)]/g, '').trim()
    const label = item.source_id === 'baidu' ? `${channel}入口`
      : item.source_id === 'toutiao' && new URL(link.url).pathname.startsWith('/trending/') ? `${channel}热点页`
      : item.original_status === 'resolved' ? `${channel}原帖` : `${channel}入口`
    return [{ ...link, channel, label }]
  })
}

export function originalLinks(items = []) {
  return items.filter(item => item.original_status === 'resolved' && item.original_url && item.resolution_evidence?.length)
    .flatMap(item => sourceLinks([{ ...item, url: item.original_url, final_url: null }])
      .map(link => ({ ...link, label: `${item.source_name || item.source_id}原帖` })))
}
