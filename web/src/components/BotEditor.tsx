import { useState } from 'react'
import { api, openChat, refreshLists, useStore } from '../store'
import type { Avatar, Bot } from '../types'
import BotAvatar, { SHAPE_NAMES } from './BotAvatar'
import { ComputerPanel } from './Computer'
import { RoutinesPanel, SkillsPanel } from './Routines'
import { MemoryPanel } from './Memories'

// 新建 Bot：和 Grok Bot 一样只问名字和头像，职责在第一条消息里交代
export function NewBotDialog({ onClose }: { onClose: () => void }) {
  const shapes = useStore(s => s.shapes)
  const colors = useStore(s => s.colors)
  const count = useStore(s => s.bots.length)
  const [name, setName] = useState('')
  const [avatar, setAvatar] = useState<Avatar>({ shape: shapes[count % shapes.length], color: colors[(count + 2) % colors.length] })
  const [err, setErr] = useState('')

  const create = async () => {
    try {
      const b: Bot = await api('/api/bots', { method: 'POST', body: { name: name.trim() || '新 Bot', avatar } })
      await refreshLists()
      openChat(b.chat_id)
      onClose()
    } catch (e: any) { setErr(e.message) }
  }

  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal small" onMouseDown={e => e.stopPropagation()}>
        <h3>新建 Bot</h3>
        <div className="new-bot-preview"><BotAvatar avatar={avatar} size={88} /></div>
        <AvatarPicker avatar={avatar} onChange={setAvatar} />
        <label className="field">名字<input autoFocus value={name} maxLength={40} placeholder="例如：Scout、研究员、邮件管家"
          onChange={e => setName(e.target.value)} onKeyDown={e => e.key === 'Enter' && create()} /></label>
        <p className="muted small">建好之后，在第一条消息里告诉它要做什么工作。</p>
        {err && <div className="error-line">{err}</div>}
        <div className="modal-actions"><button className="btn" onClick={onClose}>取消</button><button className="btn primary" onClick={create}>创建</button></div>
      </div>
    </div>
  )
}

export function AvatarPicker({ avatar, onChange }: { avatar: Avatar; onChange: (a: Avatar) => void }) {
  const shapes = useStore(s => s.shapes)
  const colors = useStore(s => s.colors)
  return (
    <div className="avatar-picker">
      <div className="shape-row">
        {shapes.map(sh => (
          <button key={sh} title={SHAPE_NAMES[sh]} className={sh === avatar.shape ? 'on' : ''} onClick={() => onChange({ ...avatar, shape: sh })}>
            <BotAvatar avatar={{ shape: sh, color: avatar.color }} size={30} />
          </button>
        ))}
      </div>
      <div className="color-row">
        {colors.map(c => <button key={c} className={c === avatar.color ? 'on' : ''} style={{ background: c }} onClick={() => onChange({ ...avatar, color: c })} aria-label={c} />)}
      </div>
    </div>
  )
}

export function BotPanel({ onClose, tab, setTab }: { onClose: () => void; tab: string; setTab: (t: string) => void }) {
  const bot = useStore(s => s.bots.find(b => b.chat_id === s.active))
  const tasks = useStore(s => s.tasks)
  if (!bot) return null
  return (
    <aside className="panel">
      <div className="panel-head">
        <div className="panel-tabs">
          <button className={tab === 'computer' ? 'on' : ''} onClick={() => setTab('computer')}>电脑</button>
          <button className={tab === 'skills' ? 'on' : ''} onClick={() => setTab('skills')}>技能</button>
          <button className={tab === 'routines' ? 'on' : ''} onClick={() => setTab('routines')}>定时</button>
          <button className={tab === 'memory' ? 'on' : ''} onClick={() => setTab('memory')}>记忆</button>
          <button className={tab === 'details' ? 'on' : ''} onClick={() => setTab('details')}>详情</button>
        </div>
        <button className="icon-btn" onClick={onClose}>✕</button>
      </div>
      {tab === 'computer'
        ? <div className="panel-body"><ComputerPanel key={bot.id} botId={bot.id} /></div>
        : tab === 'skills' ? <div className="panel-body"><SkillsPanel key={bot.id} bot={bot} /></div>
        : tab === 'routines' ? <div className="panel-body"><RoutinesPanel key={bot.id} bot={bot} /></div>
        : tab === 'memory' ? <div className="panel-body"><MemoryPanel key={bot.id} bot={bot} /></div>
        : <BotPanelInner key={bot.id} bot={bot} tasks={Object.values(tasks).filter(t => t.bot_id === bot.id)} onClose={onClose} />}
    </aside>
  )
}

function BotPanelInner({ bot, tasks, onClose }: { bot: Bot; tasks: any[]; onClose: () => void }) {
  const [name, setName] = useState(bot.name)
  const [instr, setInstr] = useState(bot.instructions)
  const [avatar, setAvatar] = useState(bot.avatar)
  const [editAvatar, setEditAvatar] = useState(false)
  const [msg, setMsg] = useState('')
  const dirty = name !== bot.name || instr !== bot.instructions || JSON.stringify(avatar) !== JSON.stringify(bot.avatar)
  const running = tasks.filter(t => t.status === 'running')

  const save = async () => {
    await api(`/api/bots/${bot.id}`, { method: 'PATCH', body: { name, instructions: instr, avatar } })
    setMsg('已保存'); setEditAvatar(false); setTimeout(() => setMsg(''), 1500)
  }
  const del = async () => {
    if (!confirm(`删除「${bot.name}」？它的对话记录也会一起删除，无法恢复。`)) return
    try { await api(`/api/bots/${bot.id}`, { method: 'DELETE' }); onClose() } catch (e: any) { setMsg(e.message) }
  }

  return (
      <div className="panel-body">
        <button className="panel-avatar" onClick={() => setEditAvatar(!editAvatar)} title="修改头像"><BotAvatar avatar={avatar} size={72} /></button>
        {editAvatar && <AvatarPicker avatar={avatar} onChange={setAvatar} />}
        <label className="field">名字<input value={name} maxLength={40} onChange={e => setName(e.target.value)} /></label>
        <label className="field">职责与要求
          <textarea rows={5} value={instr} placeholder="可以留空，直接在对话里交代。这里写的内容每次都会告诉 Bot。" onChange={e => setInstr(e.target.value)} />
        </label>
        <div className="row-actions">
          <button className="btn primary" disabled={!dirty} onClick={save}>保存</button>
          {msg && <span className="muted small">{msg}</span>}
        </div>

        <h4>正在进行</h4>
        {running.length ? running.map(t => (
          <div key={t.id} className="mini-task"><span className="dot" />#{t.id} {t.goal.slice(0, 60)}</div>
        )) : <div className="muted small">没有正在进行的任务</div>}

        <h4>管理</h4>
        {!bot.is_primary
          ? <button className="btn" onClick={() => api(`/api/bots/${bot.id}/primary`, { method: 'POST' })}>★ 设为主 Bot</button>
          : <div className="muted small">★ 这是主 Bot。</div>}
        {!bot.is_primary && <button className="btn danger" onClick={del}>删除这个 Bot</button>}
      </div>
  )
}
