import { useState } from 'react'
import { openApps, openChat, openLibrary, useStore } from '../store'
import { t } from '../i18n'
import BotAvatar from './BotAvatar'
import { GroupAvatars } from './Chat'

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
      <nav className="chat-list">
        {chats.map(c => {
          const group = c.kind === 'group'
          const members = c.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is NonNullable<typeof b> => !!b)
          const bot = group ? undefined : members[0]
          if (!group && !bot) return null
          const busy = Object.values(tasks).some(t => t.chat_id === c.id && t.status === 'running')
          const needs = Object.values(approvals).some(a => a.chat_id === c.id && a.status === 'pending')
          const away = group && Object.values(approvals).some(a => a.origin_chat_id === c.id && a.status === 'pending')
          return (
            <button key={c.id} className={'chat-item' + (c.id === active && view === 'chat' ? ' active' : '')} onClick={() => { openChat(c.id); onClose() }}>
              {group ? <span className="ci-group"><GroupAvatars bots={members} size={24} /></span> : <BotAvatar avatar={bot!.avatar} size={34} busy={busy} />}
              <div className="ci-text">
                <div className="ci-name">{group ? c.title : bot!.name}{bot?.is_primary && <span className="star">★</span>}{group && <span className="ci-count">{members.length}</span>}</div>
                <div className="ci-preview">{needs ? '⚠ ' + t('等你确认') : away ? '⚠ ' + t('有 Bot 等你确认（在私聊里）') : plain(c.preview) || (group ? t('新的群聊') : t('新的 Bot'))}</div>
              </div>
              {needs ? <span className="badge warn">!</span> : unread[c.id] ? <span className="badge">{unread[c.id]}</span> : null}
            </button>
          )
        })}
      </nav>
      <div className="side-bottom">
        <button className="side-link" onClick={onSettings}>⚙ {t('设置与费用')}</button>
        <span className={'conn' + (connected ? ' on' : '')} title={connected ? t('已连接') : t('连接中断，正在重连')} />
      </div>
    </aside>
  )
}
