import { useState } from 'react'
import { openChat, useStore } from '../store'
import BotAvatar from './BotAvatar'

// 侧栏预览：去掉任务标记和 Markdown 符号
const plain = (t: string) => t.replace(/^\[[^\]]+\]\s*/, '').replace(/[#*`>_~|]+/g, '').replace(/\s+/g, ' ').trim()

export default function Sidebar({ onNewBot, onSettings, onClose }: { onNewBot: () => void; onSettings: () => void; onClose: () => void }) {
  const chats = useStore(s => s.chats)
  const bots = useStore(s => s.bots)
  const active = useStore(s => s.active)
  const unread = useStore(s => s.unread)
  const tasks = useStore(s => s.tasks)
  const approvals = useStore(s => s.approvals)
  const connected = useStore(s => s.connected)
  const [menu, setMenu] = useState(false)

  return (
    <aside className="sidebar">
      <div className="side-top">
        <div className="brand"><span className="logo">◆</span> Super Agent</div>
        <div className="plus-wrap">
          <button className="icon-btn" title="新建" onClick={() => setMenu(!menu)}>＋</button>
          {menu && (
            <div className="menu" onMouseLeave={() => setMenu(false)}>
              <button onClick={() => { setMenu(false); onNewBot() }}>新建 Bot</button>
              <button disabled title="后续版本">新建群聊<span className="soon">即将推出</span></button>
            </div>
          )}
        </div>
      </div>
      <nav className="chat-list">
        {chats.map(c => {
          const bot = bots.find(b => b.id === c.bot_ids[0])
          if (!bot) return null
          const busy = Object.values(tasks).some(t => t.chat_id === c.id && t.status === 'running')
          const needs = Object.values(approvals).some(a => a.chat_id === c.id && a.status === 'pending')
          return (
            <button key={c.id} className={'chat-item' + (c.id === active ? ' active' : '')} onClick={() => { openChat(c.id); onClose() }}>
              <BotAvatar avatar={bot.avatar} size={34} busy={busy} />
              <div className="ci-text">
                <div className="ci-name">{bot.name}{bot.is_primary && <span className="star">★</span>}</div>
                <div className="ci-preview">{needs ? '⚠ 等你确认' : plain(c.preview) || '新的 Bot'}</div>
              </div>
              {needs ? <span className="badge warn">!</span> : unread[c.id] ? <span className="badge">{unread[c.id]}</span> : null}
            </button>
          )
        })}
      </nav>
      <div className="side-bottom">
        <button className="side-link" onClick={onSettings}>⚙ 设置与费用</button>
        <span className={'conn' + (connected ? ' on' : '')} title={connected ? '已连接' : '连接中断，正在重连'} />
      </div>
    </aside>
  )
}
