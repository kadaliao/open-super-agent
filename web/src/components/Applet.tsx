import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api, openLibrary, useStore } from '../store'
import type { Page } from '../types'
import { t } from '../i18n'
import { attachApplet } from '../applet'
import { attachMcpApp, type AppView } from '../mcpapp'

// 小应用的画面：沙箱 iframe + 宿主桥（applet.ts）。inline 时高度跟着内容走（页面里的 SDK 会报高度），
// 资料库里打开时撑满（fill）。
export function AppletFrame({ id, rawUrl, fill, title }: { id: number; rawUrl: string; fill?: boolean; title?: string }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const [h, setH] = useState(0)
  useEffect(() => {
    if (!ref.current) return
    return attachApplet(ref.current, id, fill ? undefined : setH)
  }, [id, fill])
  return <iframe ref={ref} className={fill ? 'page-frame' : 'applet-frame'} src={rawUrl} sandbox="allow-scripts allow-popups allow-forms"
    title={title || t('小应用')} style={fill ? undefined : { height: Math.min(Math.max(h, 80), 900) }} />
}

const pages = new Map<number, Promise<Page | null>>()
const fetchPage = (id: number) => {
  if (!pages.has(id)) pages.set(id, api(`/api/pages/${id}`).then(p => p as Page).catch(() => null))
  return pages.get(id)!
}
export const forgetPage = (id: number) => pages.delete(id)

// 对话里单独一行的站内页面链接（/library/pages/12）：小应用原地显示出来能直接操作，普通页面显示成卡片，点了在资料库里打开
export function PageEmbed({ href, fallback }: { href: string; fallback: ReactNode }) {
  const id = Number(href.match(/^\/library\/pages\/(\d+)$/)?.[1])
  const [p, setP] = useState<Page | null | undefined>(undefined)
  const bot = useStore(s => s.bots.find(b => b.id === p?.bot_id))
  useEffect(() => {
    let live = true
    const load = () => { forgetPage(id); fetchPage(id).then(x => { if (live) setP(x) }) }
    fetchPage(id).then(x => { if (live) setP(x) })
    window.addEventListener('osa-pages', load)   // Bot 更新了页面：重新取（iframe 的地址不变，用 key 让它重新加载）
    return () => { live = false; window.removeEventListener('osa-pages', load) }
  }, [id])
  if (p === undefined) return <>{fallback}</>
  if (!p) return <>{fallback}</>
  const open = () => openLibrary(id)
  if (p.app) return (
    <div className="applet-card">
      <div className="applet-head">
        <span className="applet-title">▣ {p.title}</span>
        <span className="grow" />
        {bot && <span className="muted small">{bot.name}</span>}
        <button className="ghost-btn" onClick={open}>{t('打开')}</button>
      </div>
      <AppletFrame key={p.updated} id={id} rawUrl={`/api/pages/${id}/raw`} title={p.title} />
    </div>
  )
  return (
    <button className="page-card" onClick={open}>
      <span className="lib-icon">{p.format === 'html' ? '◧' : '▤'}</span>
      <span className="lib-main"><b>{p.title}</b><span className="lib-meta">{t('资料库里的页面')}{bot ? ` · ${bot.name}` : ''}</span></span>
    </button>
  )
}

// 应用自带的界面（MCP Apps）：任务用到带界面的工具时挂在结果下面。iframe 是不透明源，宿主桥见 mcpapp.ts
export function McpAppView({ view, botId }: { view: AppView; botId: number | null }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const [h, setH] = useState(0)
  const src = `/api/mcp-apps/${encodeURIComponent(view.app)}/view?uri=${encodeURIComponent(view.uri)}`
  useEffect(() => {
    if (!ref.current) return
    return attachMcpApp(ref.current, view, botId, setH)
  }, [view, botId])
  return (
    <div className="applet-card">
      <div className="applet-head">
        <span className="applet-title">▣ @{view.app}</span>
        <span className="muted small">{view.tool}</span>
      </div>
      <iframe ref={ref} className="applet-frame" src={src} title={`@${view.app}`}
        sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms"
        style={{ height: Math.min(Math.max(h, 80), 900) }} />
    </div>
  )
}
