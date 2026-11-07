import { useEffect, useState } from 'react'
import type { MouseEvent } from 'react'
import { openApps, openChat, openLibrary, pinChat, useStore } from '../store'
import { t } from '../i18n'
import type { Chat } from '../types'
import BotAvatar from './BotAvatar'
import { GroupFace } from './Chat'

// 侧栏预览：去掉任务标记和 Markdown 符号
const plain = (s: string) => s === '[新话题]' ? t('— 新话题 —') : s.replace(/^\[[^\]]+\]\s*/, '').replace(/[#*`>_~|]+/g, '').replace(/\s+/g, ' ').trim()   // i18n-ignore：存在消息里的标记

export default function Sidebar({ onNewBot, onNewGroup, onSettings, onClose }: { onNewBot: () => void; onNewGroup: () => void; onSettings: () => void; onClose: () => void }) {
  const chats = useStore(s => s.chats)
  const bots = useStore(s => s.bots)
  const active = useStore(s => s.active)
  const unread = useStore(s => s.unread)
  const tasks = useStore(s => s.tasks)
  const approvals = useStore(s => s.approvals)
  const connected = useStore(s => s.connected)
  const view = useStore(s => s.view)
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

  // 一个对话在侧栏里要显示的东西；Bot 已经没了的私聊不显示
  const info = (c: Chat) => {
    const group = c.kind === 'group'
    const members = c.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is NonNullable<typeof b> => !!b)
    const bot = group ? undefined : members[0]
    if (!group && !bot) return null
    return {
      group, members, bot, name: group ? c.title : bot!.name,
      isActive: c.id === active && view === 'chat',
      busy: Object.values(tasks).some(t => t.chat_id === c.id && t.status === 'running'),
      needs: Object.values(approvals).some(a => a.chat_id === c.id && a.status === 'pending'),
      away: group && Object.values(approvals).some(a => a.origin_chat_id === c.id && a.status === 'pending'),
    }
  }
  const open = (c: Chat) => { openChat(c.id); onClose() }
  const onMenu = (c: Chat) => (e: MouseEvent) => { e.preventDefault(); setCtx({ chat: c, x: e.clientX, y: e.clientY }) }
  const badge = (c: Chat, needs: boolean) => needs ? <span className="badge warn">!</span> : unread[c.id] ? <span className="badge">{unread[c.id]}</span> : null
  const pins = chats.filter(c => c.pinned).sort((a, b) => a.pinned - b.pinned)

  return (
    <aside className="sidebar">
      <div className="side-top">
        <div className="brand"><span className="logo">◆</span> Super Agent</div>
        <div className="plus-wrap">
          <button className="icon-btn" title={t('新建')} onClick={() => setMenu(!menu)}>＋</button>
          {menu && (
            <div className="menu" onMouseLeave={() => setMenu(false)}>
              <button onClick={() => { setMenu(false); onNewBot() }}>{t('新建 Bot')}</button>
              <button onClick={() => { setMenu(false); onNewGroup() }}>{t('新建群聊')}</button>
            </div>
          )}
        </div>
      </div>
      <button className={'side-nav' + (view === 'library' ? ' active' : '')} onClick={() => { openLibrary(); onClose() }}>
        <span className="side-nav-icon">▤</span>{t('资料库')}
      </button>
      <button className={'side-nav' + (view === 'apps' ? ' active' : '')} onClick={() => { openApps(); onClose() }}>
        <span className="side-nav-icon">⊞</span>{t('应用')}
      </button>
      {pins.length > 0 && (
        <div className="pin-row">
          {pins.map(c => {
            const x = info(c)
            if (!x) return null
            return (
              <button key={c.id} className={'pin-item' + (x.isActive ? ' active' : '')} title={x.name} onClick={() => open(c)} onContextMenu={onMenu(c)}>
                <span className="pin-av">
                  {x.group ? <GroupFace image={c.image} bots={x.members} size={34} stack={24} /> : <BotAvatar avatar={x.bot!.avatar} size={34} busy={x.busy} />}
                  {badge(c, x.needs)}
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
              {group ? <span className="ci-group"><GroupFace image={c.image} bots={members} size={34} stack={24} /></span> : <BotAvatar avatar={bot!.avatar} size={34} busy={x.busy} />}
              <div className="ci-text">
                <div className="ci-name">{x.name}{bot?.is_primary && <span className="star">★</span>}{group && <span className="ci-count">{members.length}</span>}</div>
                <div className="ci-preview">{x.needs ? '⚠ ' + t('等你确认') : x.away ? '⚠ ' + t('有 Bot 等你确认（在私聊里）') : plain(c.preview) || (group ? t('新的群聊') : t('新的 Bot'))}</div>
              </div>
              {badge(c, x.needs)}
            </button>
          )
        })}
      </nav>
      {ctx && (
        <div className="menu ctx-menu" style={{ left: Math.min(ctx.x, innerWidth - 180), top: Math.min(ctx.y, innerHeight - 56) }}
             onPointerDown={e => e.stopPropagation()}>
          <button onClick={() => { pinChat(ctx.chat.id, !ctx.chat.pinned); setCtx(null) }}>{ctx.chat.pinned ? t('取消置顶') : t('置顶')}</button>
        </div>
      )}
      <div className="side-bottom">
        <button className="side-link" onClick={onSettings}>⚙ {t('设置与费用')}</button>
        <span className={'conn' + (connected ? ' on' : '')} title={connected ? t('已连接') : t('连接中断，正在重连')} />
      </div>
    </aside>
  )
}
