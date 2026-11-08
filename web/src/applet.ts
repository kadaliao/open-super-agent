import { api } from './store'

// 小应用的宿主：页面在沙箱 iframe 里（没有同源权限、连不了网），只能 postMessage 给我们；
// 我们只认来自那个 iframe 的消息，替它调服务器的接口（清单里允许的工具、它自己的数据、替用户发消息），再把结果发回去。
// 页面那边的 SDK（window.osa）由服务器注入，见 osa/applets.py。
// iframe 的 origin 是 null（sandbox 没给 allow-same-origin），所以回消息的 targetOrigin 只能是 '*'，但目标窗口是确定的。

type Req = { osa: 1; id?: number; method?: string; params?: any; event?: string; height?: number }

export function attachApplet(frame: HTMLIFrameElement, pageId: number, onHeight?: (h: number) => void) {
  const reply = (id: number, r: { result?: any; error?: string }) => frame.contentWindow?.postMessage({ osa: 1, id, ...r }, '*')
  const onMessage = async (e: MessageEvent) => {
    if (!frame.contentWindow || e.source !== frame.contentWindow) return
    const d = e.data as Req
    if (!d || d.osa !== 1) return
    if (d.event === 'resize') { if (typeof d.height === 'number' && d.height > 0) onHeight?.(d.height); return }
    if (d.event === 'ready') { api(`/api/pages/${pageId}/data`).then(data => push('data', data)).catch(() => {}); return }
    if (!d.id) return
    try {
      const p = d.params || {}
      let result: any
      switch (d.method) {
        case 'call': result = (await api(`/api/pages/${pageId}/call`, { method: 'POST', body: { tool: p.tool, args: p.args || {} } })).result; break
        case 'say': await api(`/api/pages/${pageId}/say`, { method: 'POST', body: { text: p.text } }); result = true; break
        case 'data.get': { const all = await api(`/api/pages/${pageId}/data`); result = p.key == null ? all : all[p.key]; break }
        case 'data.set': result = await api(`/api/pages/${pageId}/data`, { method: 'PUT', body: p.data || {} }); break
        default: throw new Error(`unknown method ${d.method}`)
      }
      reply(d.id, { result })
    } catch (err: any) {
      reply(d.id, { error: err?.message || String(err) })
    }
  }
  const push = (event: string, data: any) => frame.contentWindow?.postMessage({ osa: 1, event, data }, '*')
  // Bot 或别的窗口改了数据：转给页面（osa.on('data')）
  const onData = (e: Event) => {
    const { page_id, data } = (e as CustomEvent).detail || {}
    if (page_id === pageId) push('data', data)
  }
  window.addEventListener('message', onMessage)
  window.addEventListener('osa-page-data', onData)
  return () => {
    window.removeEventListener('message', onMessage)
    window.removeEventListener('osa-page-data', onData)
  }
}
