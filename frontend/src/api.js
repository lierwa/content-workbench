export const isPrototype = location.pathname.endsWith('/prototype.html')
let prototype
export async function api(path, body, method = 'POST') {
  if (isPrototype) {
    prototype ||= import('./prototype.js')
    return (await prototype).request(path, body, method)
  }
  const response = await fetch(path, body === undefined ? undefined : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(result.error || `请求失败（${response.status}）`)
  return result
}
