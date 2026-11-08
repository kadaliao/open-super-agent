import { api } from './store'
import { desktop, phone } from './desktop'
import { locale } from './i18n'

// 应用自带的界面（MCP Apps）的宿主：界面在 iframe 里，和我们之间是 postMessage 上的 JSON-RPC 2.0。
// 协议见 MCP Apps 扩展（io.modelcontextprotocol/ui，2026-01-26）；服务端在 osa/mcpapps.py。
// 界面是不透明源（iframe 不给 allow-same-origin，响应头 CSP 里也有 sandbox），只认来自那个 iframe 窗口的消息；
// 回消息的 targetOrigin 只能是 '*'（对方 origin 是 null），但目标窗口是确定的。
// 规范要网页宿主套一层“沙箱代理”，我们没有第二个源，直接放 iframe，原因见 osa/mcpapps.py 开头。

export type AppView = {
  app: string                 // 应用（MCP 服务器）名
  tool: string                // 工具原名
  uri: string                 // ui:// 地址
  input: Record<string, unknown>
  result: Record<string, unknown>   // CallToolResult：content、structuredContent、isError、_meta
}

const PROTOCOL = '2026-01-26'
const dark = () => matchMedia('(prefers-color-scheme: dark)').matches

type Msg = { jsonrpc: '2.0'; id?: number | string; method?: string; params?: any; result?: any; error?: any }

export function attachMcpApp(frame: HTMLIFrameElement, view: AppView, botId: number | null, onHeight: (h: number) => void) {
  let ready = false
  let seq = 0
  const base = `/api/mcp-apps/${encodeURIComponent(view.app)}`
  const send = (m: Omit<Msg, 'jsonrpc'>) => frame.contentWindow?.postMessage({ jsonrpc: '2.0', ...m }, '*')
  const notify = (method: string, params: any = {}) => ready && send({ method, params })
  const context = () => ({
    toolInfo: { tool: { name: view.tool, inputSchema: { type: 'object' } } },
    theme: dark() ? 'dark' : 'light',
    displayMode: 'inline', availableDisplayModes: ['inline'],
    containerDimensions: { width: frame.clientWidth || 600, maxHeight: 900 },
    locale, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    userAgent: 'open-super-agent', platform: phone ? 'mobile' : desktop ? 'desktop' : 'web',
    deviceCapabilities: { touch: matchMedia('(pointer: coarse)').matches, hover: matchMedia('(hover: hover)').matches },
  })

  // 界面发来的请求：只做这几件事，别的一律“没有这个方法”
  const handle = async (method: string, p: any): Promise<any> => {
    switch (method) {
      case 'ui/initialize':
        return {
          protocolVersion: PROTOCOL,
          hostInfo: { name: 'open-super-agent', version: '1' },
          hostCapabilities: { openLinks: {}, serverTools: {}, serverResources: {}, logging: {}, message: { text: {} } },
          hostContext: context(),
        }
      case 'tools/call':   // 同一个应用里给界面用的工具；要确认的会在 Bot 的私聊里弹卡片，这里一直等
        return api(`${base}/call`, { method: 'POST', body: { name: p?.name, arguments: p?.arguments || {}, bot_id: botId } })
      case 'resources/read':
        return api(`${base}/read`, { method: 'POST', body: { uri: p?.uri } })
      case 'ui/message': {   // 替用户给 Bot 发消息：只取文字
        const text = (Array.isArray(p?.content) ? p.content : [p?.content]).filter((c: any) => c?.type === 'text').map((c: any) => c.text).join('\n')
        await api(`${base}/say`, { method: 'POST', body: { text, bot_id: botId } })
        return {}
      }
      case 'ui/open-link': {
        const url = String(p?.url || '')
        if (!/^https?:\/\//i.test(url)) return { isError: true }
        window.open(url, '_blank', 'noopener,noreferrer')
        return {}
      }
      case 'ui/request-display-mode': return { mode: 'inline' }   // 现在只有对话里内嵌这一种
      case 'ui/update-model-context': return {}                   // 收下，暂时不带进模型的上下文
      case 'ping': return {}
    }
    throw Object.assign(new Error(`Method not found: ${method}`), { code: -32601 })
  }

  const onMessage = async (e: MessageEvent) => {
    if (!frame.contentWindow || e.source !== frame.contentWindow) return
    const d = e.data as Msg
    if (!d || d.jsonrpc !== '2.0' || typeof d.method !== 'string') return   // 我们只发 teardown 请求，回应不用等
    if (d.id === undefined) {   // 通知
      if (d.method === 'ui/notifications/initialized' && !ready) {
        ready = true   // 规范：收到 initialized 之前不能给界面发任何东西
        notify('ui/notifications/tool-input', { arguments: view.input || {} })
        notify('ui/notifications/tool-result', view.result || { content: [] })
      } else if (d.method === 'ui/notifications/size-changed' && typeof d.params?.height === 'number') {
        onHeight(d.params.height)
      } else if (d.method === 'notifications/message') {
        console.debug(`[${view.app}]`, d.params?.data ?? d.params)
      }
      return
    }
    try {
      send({ id: d.id, result: await handle(d.method, d.params) })
    } catch (err: any) {
      send({ id: d.id, error: { code: err?.code ?? -32000, message: err?.message || String(err) } })
    }
  }
  const scheme = matchMedia('(prefers-color-scheme: dark)')
  const onScheme = () => notify('ui/notifications/host-context-changed', { theme: dark() ? 'dark' : 'light' })
  window.addEventListener('message', onMessage)
  scheme.addEventListener('change', onScheme)
  return () => {
    if (ready) send({ id: `teardown-${++seq}`, method: 'ui/resource-teardown', params: {} })
    window.removeEventListener('message', onMessage)
    scheme.removeEventListener('change', onScheme)
  }
}
