import { useState } from 'react'
import { api, openChat, refreshLists, useStore } from '../store'
import type { Bot, Chat } from '../types'
import BotAvatar from './BotAvatar'
import { ComputerPanel } from './Computer'

const MIN = 2, MAX = 6

function BotPicker({ bots, picked, onChange }: { bots: Bot[]; picked: number[]; onChange: (ids: number[]) => void }) {
  return (
    <div className="bot-picker">
      {bots.map(b => {
        const on = picked.includes(b.id)
        return (
          <button key={b.id} className={on ? 'on' : ''} disabled={!on && picked.length >= MAX}
            onClick={() => onChange(on ? picked.filter(x => x !== b.id) : [...picked, b.id])}>
            <BotAvatar avatar={b.avatar} size={26} /><span>{b.name}</span>{on && <span className="check-mark">✓</span>}
          </button>
        )
      })}
    </div>
  )
}

export function NewGroupDialog({ onClose }: { onClose: () => void }) {
  const bots = useStore(s => s.bots)
  const [picked, setPicked] = useState<number[]>([])
  const [title, setTitle] = useState('')
  const [lead, setLead] = useState<number | null>(null)
  const [err, setErr] = useState('')
  const leadId = lead && picked.includes(lead) ? lead : (picked.find(i => bots.find(b => b.id === i)?.is_primary) ?? picked[0])
  const create = async () => {
    try {
      const g: Chat = await api('/api/groups', { method: 'POST', body: { bot_ids: picked, title, lead: leadId } })
      await refreshLists(); openChat(g.id); onClose()
    } catch (e: any) { setErr(e.message) }
  }
  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal small" onMouseDown={e => e.stopPropagation()}>
        <h3>新建群聊</h3>
        {bots.length < MIN ? <p className="muted">至少要有 {MIN} 个 Bot 才能建群聊，先去新建一个 Bot。</p> : <>
          <p className="muted small">选 {MIN}–{MAX} 个 Bot。群里 @ 谁谁回答；不 @ 的消息由协调者接，它会分给合适的 Bot。</p>
          <BotPicker bots={bots} picked={picked} onChange={setPicked} />
          <label className="field">名字（可以不填）<input value={title} maxLength={60}
            placeholder={picked.map(i => bots.find(b => b.id === i)?.name).filter(Boolean).slice(0, 3).join('、') || '自动用成员的名字'}
            onChange={e => setTitle(e.target.value)} /></label>
          {picked.length >= MIN && <label className="field">协调者
            <select value={leadId} onChange={e => setLead(Number(e.target.value))}>
              {picked.map(i => <option key={i} value={i}>{bots.find(b => b.id === i)?.name}</option>)}
            </select>
          </label>}
        </>}
        {err && <div className="error-line">{err}</div>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn primary" disabled={picked.length < MIN} onClick={create}>创建</button>
        </div>
      </div>
    </div>
  )
}

export function GroupPanel({ chat, onClose, tab, setTab }: { chat: Chat; onClose: () => void; tab: string; setTab: (t: string) => void }) {
  const t = tab === 'computer' ? 'computer' : 'group'
  return (
    <aside className="panel">
      <div className="panel-head">
        <div className="panel-tabs">
          <button className={t === 'group' ? 'on' : ''} onClick={() => setTab('group')}>群聊</button>
          <button className={t === 'computer' ? 'on' : ''} onClick={() => setTab('computer')}>电脑</button>
        </div>
        <button className="icon-btn" onClick={onClose}>✕</button>
      </div>
      <div className="panel-body">
        {t === 'computer' ? <ComputerPanel key={chat.id} botId={chat.lead ?? chat.bot_ids[0]} /> : <GroupSettings key={chat.id} chat={chat} onClose={onClose} />}
      </div>
    </aside>
  )
}

function GroupSettings({ chat, onClose }: { chat: Chat; onClose: () => void }) {
  const bots = useStore(s => s.bots)
  const tasks = useStore(s => s.tasks)
  const [title, setTitle] = useState(chat.title)
  const [adding, setAdding] = useState(false)
  const [err, setErr] = useState('')
  const members = chat.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is Bot => !!b)
  const others = bots.filter(b => !chat.bot_ids.includes(b.id))
  const patch = async (body: any) => {
    setErr('')
    try { await api(`/api/chats/${chat.id}`, { method: 'PATCH', body }); await refreshLists() } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!confirm(`删除群聊「${chat.title}」？群里的消息会一起删除，Bot 和它们的私聊不受影响。`)) return
    await api(`/api/chats/${chat.id}`, { method: 'DELETE' }); await refreshLists(); onClose()
    const first = bots[0]; if (first) openChat(first.chat_id)
  }
  const running = Object.values(tasks).filter(x => x.chat_id === chat.id && x.status === 'running')
  return (
    <>
      <label className="field">群名<input value={title} maxLength={60} onChange={e => setTitle(e.target.value)}
        onBlur={() => title !== chat.title && patch({ title })} onKeyDown={e => e.key === 'Enter' && (e.target as HTMLInputElement).blur()} /></label>
      <div className="section-head"><h4>成员（{members.length}/{MAX}）</h4>
        {members.length < MAX && others.length > 0 && <button className="btn" onClick={() => setAdding(!adding)}>{adding ? '收起' : '＋ 添加'}</button>}
      </div>
      {adding && <div className="bot-picker compact">
        {others.map(b => <button key={b.id} onClick={() => { patch({ bot_ids: [...chat.bot_ids, b.id] }); setAdding(false) }}>
          <BotAvatar avatar={b.avatar} size={22} /><span>{b.name}</span></button>)}
      </div>}
      {members.map(b => (
        <div key={b.id} className="member">
          <BotAvatar avatar={b.avatar} size={28} />
          <div className="member-main">
            <b>{b.name}</b>{chat.lead === b.id && <span className="tag">协调者</span>}
            <div className="muted small">{(b.instructions || '职责没写明').slice(0, 40)}</div>
          </div>
          <div className="member-actions">
            <button className="ghost-btn" onClick={() => openChat(b.chat_id)}>私聊</button>
            {chat.lead !== b.id && <button className="ghost-btn" onClick={() => patch({ lead: b.id })}>设为协调者</button>}
            {members.length > MIN && <button className="ghost-btn" onClick={() => patch({ bot_ids: chat.bot_ids.filter(x => x !== b.id) })}>移出</button>}
          </div>
        </div>
      ))}
      {members.length < MIN && <div className="error-line">成员少于 {MIN} 个，再添加一个 Bot 吧。</div>}
      <p className="muted small">没 @ 任何人的消息由协调者接。要确认的操作和“请你帮忙”会出现在对应 Bot 的私聊里。</p>
      {err && <div className="error-line">{err}</div>}
      <h4>正在进行</h4>
      {running.length ? running.map(x => (
        <div key={x.id} className="mini-task"><span className="dot" />{bots.find(b => b.id === x.bot_id)?.name} #{x.id} {x.goal.slice(0, 50)}</div>
      )) : <div className="muted small">没有正在进行的任务</div>}
      <h4>管理</h4>
      <button className="btn danger" onClick={del}>删除这个群聊</button>
    </>
  )
}
