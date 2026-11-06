import { useEffect, useState } from 'react'
import { bootstrap, chatById, connect, getState, openApps, openChat, openLibrary, useStore } from './store'
import Chat from './components/Chat'
import Library from './components/Library'
import Market from './components/Market'
import Sidebar from './components/Sidebar'
import Settings from './components/Settings'
import Onboarding from './components/Onboarding'
import { BotPanel, NewBotDialog } from './components/BotEditor'
import { GroupPanel, NewGroupDialog } from './components/Groups'

export default function App() {
  const ready = useStore(s => s.ready)
  const view = useStore(s => s.view)
  const chat = useStore(s => chatById(s, s.active))
  const [newBot, setNewBot] = useState(false)
  const [newGroup, setNewGroup] = useState(false)
  const [settings, setSettings] = useState<string | false>(false)   // 打开时是哪一页（空字符串：上次看的那页）
  const [onboard, setOnboard] = useState(false)                     // 第一次打开时的引导
  const [details, setDetails] = useState(() => localStorage.getItem('osa.details') === '1')
  const [side, setSide] = useState(false)   // 手机上的侧栏抽屉
  const [tab, setTab] = useState(() => localStorage.getItem('osa.tab') || 'computer')
  useEffect(() => localStorage.setItem('osa.tab', tab), [tab])
  // 对话里点“接手电脑”：打开右栏的电脑标签并接手
  useEffect(() => {
    const h = (e: Event) => {
      const { botId, takeover, taskId } = (e as CustomEvent).detail
      setDetails(true); setTab('computer')
      if (takeover) setTimeout(() => window.dispatchEvent(new CustomEvent('osa-takeover', { detail: { botId, taskId } })), 300)
    }
    window.addEventListener('osa-open-computer', h)
    return () => window.removeEventListener('osa-open-computer', h)
  }, [])
  // 点了任务卡片上的“过程”：打开右栏的任务标签
  useEffect(() => {
    const h = () => { setDetails(true); setTab('tasks') }
    window.addEventListener('osa-open-task', h)
    return () => window.removeEventListener('osa-open-task', h)
  }, [])

  // 演示一遍：开始录制时打开这个 Bot 的电脑画面；录完有草稿时打开它的技能面板
  const teach = useStore(s => s.teach)
  const draft = useStore(s => s.teachDraft)
  const showBot = (botId: number, t: string) => {
    const b = getState().bots.find(x => x.id === botId)
    if (b && getState().active !== b.chat_id) openChat(b.chat_id)
    setDetails(true); setTab(t)
  }
  useEffect(() => { if (teach) showBot(teach.bot_id, 'computer') }, [teach?.started])
  useEffect(() => { if (draft) showBot(draft.bot_id, 'skills') }, [draft])

  // 点了推送通知：Service Worker 把通知里的地址（/?chat=12）发过来
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return
    const h = (e: MessageEvent) => {
      if (e.data?.type !== 'open') return
      const chat = Number(new URL(e.data.url, location.origin).searchParams.get('chat'))
      if (chat && getState().chats.some(c => c.id === chat)) openChat(chat)
    }
    navigator.serviceWorker.addEventListener('message', h)
    return () => navigator.serviceWorker.removeEventListener('message', h)
  }, [])

  // 别处要打开设置的某一页（比如对话里“去配置模型”）；设置里点“新手引导”再看一遍引导
  useEffect(() => {
    const h = (e: Event) => setSettings((e as CustomEvent).detail || '')
    const g = () => { setSettings(false); setOnboard(true) }
    window.addEventListener('osa-open-settings', h)
    window.addEventListener('osa-onboarding', g)
    return () => { window.removeEventListener('osa-open-settings', h); window.removeEventListener('osa-onboarding', g) }
  }, [])

  // 对话里的页面链接（/library/pages/12）：在资料库里打开，不刷新页面
  useEffect(() => {
    const h = (e: Event) => openLibrary((e as CustomEvent).detail)
    window.addEventListener('osa-open-page', h)
    return () => window.removeEventListener('osa-open-page', h)
  }, [])

  useEffect(() => {
    const deep = location.pathname.match(/^\/library(?:\/pages\/(\d+))?\/?$/)
    const apps = /^\/apps\/?$/.test(location.pathname)   // 登录授权在本窗口完成后回到这里
    bootstrap().then(() => {
      if (getState().onboarding && !deep && !apps) setOnboard(true)
      if (deep) { openLibrary(deep[1] ? Number(deep[1]) : null); history.replaceState(null, '', '/') }
      if (apps) { openApps(); history.replaceState(null, '', '/') }
      connect()
    })
    if ('Notification' in window && Notification.permission === 'default') {
      // 引导开着的时候先不问，免得一点服务商就弹出浏览器的权限框
      const ask = () => { if (document.querySelector('.onb')) return; Notification.requestPermission(); window.removeEventListener('click', ask) }
      window.addEventListener('click', ask)
    }
  }, [])
  useEffect(() => localStorage.setItem('osa.details', details ? '1' : '0'), [details])

  if (!ready) return <div className="loading">◆</div>
  return (
    <div className={'app' + (details && view === 'chat' ? ' with-panel' : '') + (details && tab === 'computer' ? ' wide-panel' : details && tab === 'tasks' ? ' mid-panel' : '') + (side ? ' side-open' : '')}>
      <button className="mobile-menu icon-btn" onClick={() => setSide(true)}>☰</button>
      <div className="side-mask" onClick={() => setSide(false)} />
      <Sidebar onNewBot={() => setNewBot(true)} onNewGroup={() => setNewGroup(true)} onSettings={() => setSettings('')} onClose={() => setSide(false)} />
      {view === 'library' ? <Library /> : view === 'apps' ? <Market /> : <Chat onToggleDetails={() => setDetails(!details)} />}
      {details && view === 'chat' && (chat?.kind === 'group'
        ? <GroupPanel chat={chat} onClose={() => setDetails(false)} tab={tab} setTab={setTab} />
        : <BotPanel onClose={() => setDetails(false)} tab={tab} setTab={setTab} />)}
      {newBot && <NewBotDialog onClose={() => setNewBot(false)} />}
      {newGroup && <NewGroupDialog onClose={() => setNewGroup(false)} />}
      {settings !== false && <Settings initial={settings || undefined} onClose={() => setSettings(false)} />}
      {onboard && <Onboarding onClose={() => setOnboard(false)} />}
    </div>
  )
}
