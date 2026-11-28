import { useEffect, useState } from 'react'
import type { MouseEvent } from 'react'
import { openApps, openBoard, openChat, openLibrary, openObserve, pinChat, useStore } from '../store'
import { t } from '../i18n'
import type { Chat } from '../types'
import BotAvatar from './BotAvatar'
import { GroupFace } from './Chat'
import { Icon, Logo } from '../icons'
import { Preview, plain } from '../plain'
import { BoardIcon } from './Board'
import { useWaiting } from '../board'
import { StatusDot, chatStatus, statusLine, useReplying } from './BotStatus'


export default function Sidebar({ onNewBot, onNewGroup, onSettings, onClose }: { onNewBot: () => void; onNewGroup: () => void; onSettings: () => void; onClose: () => void }) {
  const chats = useStore(s => s.chats)
  const bots = useStore(s => s.bots)
  const active = useStore(s => s.active)
  const unread = useStore(s => s.unread)
  const threads = useStore(s => s.threads)
  const tasks = useStore(s => s.tasks)
  const approvals = useStore(s => s.approvals)
  const connected = useStore(s => s.connected)
  const view = useStore(s => s.view)
  const waiting = useWaiting()   // 在“等你处理”的卡
  const [menu, setMenu] = useState(false)
  const [ctx, setCtx] = useState<{ chat: Chat; x: number; y: number } | null>(null)   // 右键菜单

  // 右键菜单：点别处、按 Esc、滚动、窗口变化都关掉
  useEffect(() => {
    if (!ctx) return
    const close = () => setCtx(null)
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('pointerdown', close)
    window.addEventListener('keydown', key)
    window.addEventListener('resize', close)
    window.addEventListener('blur', close)
    window.addEventListener('scroll', close, true)
    return () => {
      window.removeEventListener('pointerdown', close)
      window.removeEventListener('keydown', key)
      window.removeEventListener('resize', close)
      window.removeEventListener('blur', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [ctx])

  const replying = useReplying()   // 只在开始、结束回话时变；侧栏不显示秒数，不用每秒重画
  // 一个对话在侧栏里要显示的东西；Bot 已经没了的私聊不显示
  const info = (c: Chat) => {
    const group = c.kind === 'group'
    const members = c.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is NonNullable<typeof b> => !!b)
    const bot = group ? undefined : members[0]
    if (!group && !bot) return null
    const st = chatStatus(c, tasks, approvals, replying.has(c.id))
    return { group, members, bot, name: group ? c.title : bot!.name, isActive: c.id === active && view === 'chat', st }
  }
  const open = (c: Chat) => { openChat(c.id); onClose() }
  const onMenu = (c: Chat) => (e: MouseEvent) => { e.preventDefault(); setCtx({ chat: c, x: e.clientX, y: e.clientY }) }
  // 讨论串里的新回复也算在对话上：看过那个讨论串才去掉（只打开对话不算）
  const news = (c: Chat) => Object.values(threads).filter(x => x.parent === c.id && x.unread > 0)
  const count = (c: Chat) => (unread[c.id] || 0) + news(c).reduce((n, x) => n + x.unread, 0)
  // 未读数；等你处理的不再另挂一个“!”，头像角上的点和第二行已经说了
  const badge = (c: Chat) => count(c) ? <span className="badge">{count(c)}</span> : null
  // 预览显示最新的动静：讨论串里的新回复比主对话的最后一条还新时，显示那条回复
  const preview = (c: Chat) => {
    const th = news(c).sort((a, b) => (b.last || 0) - (a.last || 0))[0]
    return th?.preview && (th.last || 0) >= c.updated ? <><Icon name="threads" size={14} /><Preview text={th.preview} /></> : plain(c.preview) && <Preview text={c.preview} />
  }
  const pins = chats.filter(c => c.pinned).sort((a, b) => a.pinned - b.pinned)

  return (
    <aside className="sidebar">
      <div className="side-top">
        <span className="logo" title="Open Super Agent"><Logo size={18} /></span>
        <div className="plus-wrap">
          <button className="icon-btn round" title={t('新建')} onClick={() => setMenu(!menu)}><Icon name="plus" size={16} /></button>
          {menu && (
            <div className="menu" onMouseLeave={() => setMenu(false)}>
              <button onClick={() => { setMenu(false); onNewBot() }}><Icon name="bot" size={16} className="lead" />{t('新建 Bot')}</button>
              <button onClick={() => { setMenu(false); onNewGroup() }}><Icon name="users" size={16} className="lead" />{t('新建群聊')}</button>
            </div>
          )}
        </div>
      </div>
      <button className={'side-nav' + (view === 'board' ? ' active' : '')} onClick={() => { openBoard(); onClose() }}>
        <span className="side-nav-icon"><BoardIcon size={16} /></span>{t('需求')}
        {waiting > 0 && <span className="badge warn side-nav-badge" title={t('{n} 张卡在等你处理', { n: waiting })}>{waiting}</span>}
      </button>
      <button className={'side-nav' + (view === 'library' ? ' active' : '')} onClick={() => { openLibrary(); onClose() }}>
        <span className="side-nav-icon"><Icon name="library" size={16} /></span>{t('资料库')}
      </button>
      <button className={'side-nav' + (view === 'apps' ? ' active' : '')} onClick={() => { openApps(); onClose() }}>
        <span className="side-nav-icon"><Icon name="grid" size={16} /></span>{t('应用')}
      </button>
      <button className={'side-nav' + (view === 'observe' ? ' active' : '')} onClick={() => { openObserve(); onClose() }}>
        <span className="side-nav-icon"><Icon name="activity" size={16} /></span>{t('观测')}
      </button>
      {pins.length > 0 && (
        <div className="pin-row">
          {pins.map(c => {
            const x = info(c)
            if (!x) return null
            return (
              <button key={c.id} className={'pin-item' + (x.isActive ? ' active' : '')} title={x.name} onClick={() => open(c)} onContextMenu={onMenu(c)}>
                <span className="pin-av">
                  {x.group ? <GroupFace image={c.image} bots={x.members} size={32} stack={20} /> : <BotAvatar avatar={x.bot!.avatar} size={32} />}
                  <StatusDot s={x.st} />
                  {badge(c)}
                </span>
                <span className="pin-name">{x.name}</span>
              </button>
            )
          })}
        </div>
      )}
      <nav className="chat-list">
        {chats.map(c => {
          const x = info(c)
          if (!x) return null
          const { group, members, bot } = x
          return (
            <button key={c.id} className={'chat-item' + (x.isActive ? ' active' : '')} onClick={() => open(c)} onContextMenu={onMenu(c)}>
              <span className={group ? 'ci-av ci-group' : 'ci-av'}>
                {group ? <GroupFace image={c.image} bots={members} size={32} stack={20} /> : <BotAvatar avatar={bot!.avatar} size={32} />}
                <StatusDot s={x.st} />
              </span>
              <div className="ci-text">
                <div className="ci-name"><span className="ci-title">{x.name}</span>{bot?.is_primary && <span className="star" title={t('主 Bot')}><Icon name="star" size={14} className="solid" /></span>}{group && <span className="ci-count">{members.length}</span>}</div>
                <div className="ci-preview">{statusLine(x.st) || preview(c) || (group ? t('新的群聊') : plain(bot!.instructions) || t('新的 Bot'))}</div>
              </div>
              {badge(c)}
            </button>
          )
        })}
      </nav>
      {ctx && (
        <div className="menu ctx-menu" style={{ left: Math.min(ctx.x, innerWidth - 180), top: Math.min(ctx.y, innerHeight - 56) }}
             onPointerDown={e => e.stopPropagation()}>
          <button onClick={() => { pinChat(ctx.chat.id, !ctx.chat.pinned); setCtx(null) }}><Icon name="pin" size={16} className="lead" />{ctx.chat.pinned ? t('取消置顶') : t('置顶')}</button>
        </div>
      )}
      <div className="side-bottom">
        <button className="side-link" onClick={onSettings}><Icon name="settings" size={16} />{t('设置')}</button>
        <span className={'conn' + (connected ? ' on' : '')} title={connected ? t('已连接') : t('连接中断，正在重连')} />
      </div>
    </aside>
  )
}
