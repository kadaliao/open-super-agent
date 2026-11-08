import { useRef, useState } from 'react'
import { api, openChat, refreshLists, useStore } from '../store'
import type { Bot, Chat } from '../types'
import { lang, t, tc } from '../i18n'
import BotAvatar from './BotAvatar'
import { ComputerPanel } from './Computer'
import { TasksPanel } from './Tasks'
import { ask } from './Dialog'
import { GroupFace } from './Chat'
import { AvatarImageTools, groupPrompt } from './AvatarTools'
import { useBack } from '../swipe'
import { isEnter } from '../keys'

const MIN = 2, MAX = 6
const SEP = lang === 'en' ? ', ' : '、'

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
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
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
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal small" onMouseDown={e => e.stopPropagation()}>
        <h3>{t('新建群聊')}</h3>
        {bots.length < MIN ? <p className="muted">{t('至少要有 {n} 个 Bot 才能建群聊，先去新建一个 Bot。', { n: MIN })}</p> : <>
          <p className="muted small">{t('选 {min}–{max} 个 Bot。群里 @ 谁谁回答；不 @ 的消息由协调者接，它会分给合适的 Bot。', { min: MIN, max: MAX })}</p>
          <BotPicker bots={bots} picked={picked} onChange={setPicked} />
          <label className="field">{t('名字（可以不填）')}<input value={title} maxLength={60}
            placeholder={picked.map(i => bots.find(b => b.id === i)?.name).filter(Boolean).slice(0, 3).join(SEP) || t('自动用成员的名字')}
            onChange={e => setTitle(e.target.value)} /></label>
          {picked.length >= MIN && <label className="field">{t('协调者')}
            <select value={leadId} onChange={e => setLead(Number(e.target.value))}>
              {picked.map(i => <option key={i} value={i}>{bots.find(b => b.id === i)?.name}</option>)}
            </select>
          </label>}
        </>}
        {err && <div className="error-line">{err}</div>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>{t('取消')}</button>
          <button className="btn primary" disabled={picked.length < MIN} onClick={create}>{t('创建')}</button>
        </div>
      </div>
    </div>
  )
}

export function GroupPanel({ chat, onClose, tab, setTab }: { chat: Chat; onClose: () => void; tab: string; setTab: (tab: string) => void }) {
  const cur = tab === 'computer' || tab === 'tasks' ? tab : 'group'
  const root = useRef<HTMLElement>(null)
  useBack(onClose, root)
  return (
    <aside className="panel" ref={root}>
      <div className="panel-head">
        <div className="panel-tabs">
          <button className={cur === 'group' ? 'on' : ''} onClick={() => setTab('group')}>{tc('tab', '群聊')}</button>
          <button className={cur === 'tasks' ? 'on' : ''} onClick={() => setTab('tasks')}>{tc('tab', '任务')}</button>
          <button className={cur === 'computer' ? 'on' : ''} onClick={() => setTab('computer')}>{t('电脑')}</button>
        </div>
        <button className="icon-btn" onClick={onClose}>✕</button>
      </div>
      {cur === 'tasks' ? <TasksPanel key={chat.id} chatId={chat.id} /> : <div className="panel-body">
        {cur === 'computer' ? <ComputerPanel key={chat.id} botId={chat.lead ?? chat.bot_ids[0]} /> : <GroupSettings key={chat.id} chat={chat} onClose={onClose} />}
      </div>}
    </aside>
  )
}

function GroupSettings({ chat, onClose }: { chat: Chat; onClose: () => void }) {
  const bots = useStore(s => s.bots)
  const tasks = useStore(s => s.tasks)
  const [title, setTitle] = useState(chat.title)
  const [adding, setAdding] = useState(false)
  const [editAvatar, setEditAvatar] = useState(false)
  const [err, setErr] = useState('')
  const members = chat.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is Bot => !!b)
  const others = bots.filter(b => !chat.bot_ids.includes(b.id))
  const patch = async (body: any) => {
    setErr('')
    try { await api(`/api/chats/${chat.id}`, { method: 'PATCH', body }); await refreshLists() } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!(await ask(t('删除群聊「{name}」？群里的消息会一起删除，Bot 和它们的私聊不受影响。', { name: chat.title }), { ok: t('删除'), danger: true }))) return
    await api(`/api/chats/${chat.id}`, { method: 'DELETE' }); await refreshLists(); onClose()
    const first = bots[0]; if (first) openChat(first.chat_id)
  }
  const running = Object.values(tasks).filter(x => x.chat_id === chat.id && x.status === 'running')
  return (
    <>
      <button className="panel-avatar" onClick={() => setEditAvatar(!editAvatar)} title={t('修改群头像')}>
        <GroupFace image={chat.image} bots={members} size={72} stack={48} /></button>
      {editAvatar && <div className="avatar-picker">
        <AvatarImageTools prompt={groupPrompt(chat.title, members.map(b => b.name))} onImage={image => { patch({ image }); setEditAvatar(false) }} />
        {chat.image && <button className="ghost-btn" onClick={() => { patch({ image: '' }); setEditAvatar(false) }}>{t('不用图片，用成员的头像拼')}</button>}
      </div>}
      <label className="field">{t('群名')}<input value={title} maxLength={60} onChange={e => setTitle(e.target.value)}
        onBlur={() => title !== chat.title && patch({ title })} onKeyDown={e => isEnter(e) && (e.target as HTMLInputElement).blur()} /></label>
      <div className="section-head"><h4>{t('成员（{n}/{max}）', { n: members.length, max: MAX })}</h4>
        {members.length < MAX && others.length > 0 && <button className="btn" onClick={() => setAdding(!adding)}>{adding ? tc('add', '收起') : '＋ ' + t('添加')}</button>}
      </div>
      {adding && <div className="bot-picker compact">
        {others.map(b => <button key={b.id} onClick={() => { patch({ bot_ids: [...chat.bot_ids, b.id] }); setAdding(false) }}>
          <BotAvatar avatar={b.avatar} size={22} /><span>{b.name}</span></button>)}
      </div>}
      {members.map(b => (
        <div key={b.id} className="member">
          <BotAvatar avatar={b.avatar} size={28} />
          <div className="member-main">
            <b>{b.name}</b>{chat.lead === b.id && <span className="tag">{t('协调者')}</span>}
            <div className="muted small">{(b.instructions || t('职责没写明')).slice(0, 40)}</div>
          </div>
          <div className="member-actions">
            <button className="ghost-btn" onClick={() => openChat(b.chat_id)}>{t('私聊')}</button>
            {chat.lead !== b.id && <button className="ghost-btn" onClick={() => patch({ lead: b.id })}>{t('设为协调者')}</button>}
            {members.length > MIN && <button className="ghost-btn" onClick={() => patch({ bot_ids: chat.bot_ids.filter(x => x !== b.id) })}>{t('移出')}</button>}
          </div>
        </div>
      ))}
      {members.length < MIN && <div className="error-line">{t('成员少于 {n} 个，再添加一个 Bot 吧。', { n: MIN })}</div>}
      <p className="muted small">{t('没 @ 任何人的消息由协调者接。要确认的操作和“请你帮忙”会出现在对应 Bot 的私聊里。')}</p>
      {err && <div className="error-line">{err}</div>}
      <h4>{t('正在进行')}</h4>
      {running.length ? running.map(x => (
        <div key={x.id} className="mini-task"><span className="dot" />{bots.find(b => b.id === x.bot_id)?.name} #{x.id} {x.goal.slice(0, 50)}</div>
      )) : <div className="muted small">{t('没有正在进行的任务')}</div>}
      <h4>{t('管理')}</h4>
      <button className="btn danger" onClick={del}>{t('删除这个群聊')}</button>
    </>
  )
}
