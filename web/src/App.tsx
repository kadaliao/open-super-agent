import { useEffect, useState } from 'react'
import { bootstrap, connect, openLibrary, useStore } from './store'
import Chat from './components/Chat'
import Library from './components/Library'
import Sidebar from './components/Sidebar'
import Settings from './components/Settings'
import { BotPanel, NewBotDialog } from './components/BotEditor'

export default function App() {
  const ready = useStore(s => s.ready)
  const view = useStore(s => s.view)
  const [newBot, setNewBot] = useState(false)
  const [settings, setSettings] = useState(false)
  const [details, setDetails] = useState(() => localStorage.getItem('osa.details') === '1')
  const [side, setSide] = useState(false)   // 手机上的侧栏抽屉
  const [tab, setTab] = useState(() => localStorage.getItem('osa.tab') || 'computer')
  useEffect(() => localStorage.setItem('osa.tab', tab), [tab])
  // 对话里点“接手电脑”：打开右栏的电脑标签并接手
  useEffect(() => {
    const h = (e: Event) => {
      const { botId, takeover } = (e as CustomEvent).detail
      setDetails(true); setTab('computer')
      if (takeover) setTimeout(() => window.dispatchEvent(new CustomEvent('osa-takeover', { detail: botId })), 300)
    }
    window.addEventListener('osa-open-computer', h)
    return () => window.removeEventListener('osa-open-computer', h)
  }, [])

  // 对话里的页面链接（/library/pages/12）：在资料库里打开，不刷新页面
  useEffect(() => {
    const h = (e: Event) => openLibrary((e as CustomEvent).detail)
    window.addEventListener('osa-open-page', h)
    return () => window.removeEventListener('osa-open-page', h)
  }, [])

  useEffect(() => {
    const deep = location.pathname.match(/^\/library(?:\/pages\/(\d+))?\/?$/)
    bootstrap().then(() => {
      if (deep) { openLibrary(deep[1] ? Number(deep[1]) : null); history.replaceState(null, '', '/') }
      connect()
    })
    if ('Notification' in window && Notification.permission === 'default') {
      const ask = () => { Notification.requestPermission(); window.removeEventListener('click', ask) }
      window.addEventListener('click', ask)
    }
  }, [])
  useEffect(() => localStorage.setItem('osa.details', details ? '1' : '0'), [details])

  if (!ready) return <div className="loading">◆</div>
  return (
    <div className={'app' + (details && view === 'chat' ? ' with-panel' : '') + (details && tab === 'computer' ? ' wide-panel' : '') + (side ? ' side-open' : '')}>
      <button className="mobile-menu icon-btn" onClick={() => setSide(true)}>☰</button>
      <div className="side-mask" onClick={() => setSide(false)} />
      <Sidebar onNewBot={() => setNewBot(true)} onSettings={() => setSettings(true)} onClose={() => setSide(false)} />
      {view === 'library' ? <Library /> : <Chat onToggleDetails={() => setDetails(!details)} />}
      {details && view === 'chat' && <BotPanel onClose={() => setDetails(false)} tab={tab} setTab={setTab} />}
      {newBot && <NewBotDialog onClose={() => setNewBot(false)} />}
      {settings && <Settings onClose={() => setSettings(false)} />}
    </div>
  )
}
