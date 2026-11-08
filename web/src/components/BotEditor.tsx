import { useRef, useState } from 'react'
import { api, openChat, refreshLists, showTask, useStore } from '../store'
import type { Avatar, Bot } from '../types'
import { t, tc } from '../i18n'
import BotAvatar, { SHAPE_NAMES } from './BotAvatar'
import { ComputerPanel } from './Computer'
import { RoutinesPanel, SkillsPanel } from './Routines'
import { EventsPanel } from './Events'
import { MemoryPanel } from './Memories'
import { ShareTemplate, TemplateInstall, TemplatePicker, type TemplatePreview } from './Templates'
import { TasksPanel } from './Tasks'
import { BotLibraryPanel } from './BotLibrary'
import { ask } from './Dialog'
import { AvatarImageTools, botPrompt } from './AvatarTools'
import { useBack } from '../swipe'
import { isEnter } from '../keys'

// 新建 Bot：从模板装（带好技能和定时任务），或者空白的：只问名字和头像，职责在第一条消息里交代
export function NewBotDialog({ onClose }: { onClose: () => void }) {
  const shapes = useStore(s => s.shapes)
  const colors = useStore(s => s.colors)
  const count = useStore(s => s.bots.length)
  const [mode, setMode] = useState<'pick' | 'blank'>('pick')
  const [preview, setPreview] = useState<TemplatePreview | null>(null)
  const [name, setName] = useState('')
  const [avatar, setAvatar] = useState<Avatar>({ shape: shapes[count % shapes.length], color: colors[(count + 2) % colors.length] })
  const [err, setErr] = useState('')
  const done = async (b: Bot) => { await refreshLists(); openChat(b.chat_id); onClose() }
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  useBack(() => preview ? setPreview(null) : setMode('pick'), undefined, !!preview || mode === 'blank')   // 上一步

  const create = async () => {
    try { await done(await api('/api/bots', { method: 'POST', body: { name: name.trim() || t('新 Bot'), avatar } })) }
    catch (e: any) { setErr(e.message) }
  }

  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className={'modal ' + (mode === 'pick' ? 'medium' : 'small')} onMouseDown={e => e.stopPropagation()}>
        <h3>{t('新建 Bot')}</h3>
        {preview ? <TemplateInstall p={preview} onBack={() => setPreview(null)} onInstalled={done} />
          : mode === 'pick' ? <>
            <p className="muted small">{t('从现成的模板开始（带好技能和定时任务），或者建一个空白的。')}</p>
            <TemplatePicker onPreview={setPreview} onBlank={() => setMode('blank')} />
            <div className="modal-actions"><button className="btn" onClick={onClose}>{t('取消')}</button></div>
          </> : <>
            <div className="new-bot-preview"><BotAvatar avatar={avatar} size={88} /></div>
            <AvatarPicker avatar={avatar} onChange={setAvatar} prompt={botPrompt(name)} />
            <label className="field">{t('名字')}<input autoFocus value={name} maxLength={40} placeholder={t('例如：Scout、研究员、邮件管家')}
              onChange={e => setName(e.target.value)} onKeyDown={e => isEnter(e) && create()} /></label>
            <p className="muted small">{t('建好之后，在第一条消息里告诉它要做什么工作。')}</p>
            {err && <div className="error-line">{err}</div>}
            <div className="modal-actions"><button className="btn" onClick={() => setMode('pick')}>← {t('从模板选')}</button>
              <span className="grow" /><button className="btn" onClick={onClose}>{t('取消')}</button><button className="btn primary" onClick={create}>{t('创建')}</button></div>
          </>}
      </div>
    </div>
  )
}

// 头像：选形状和颜色，或者用一张图片（上传、AI 生成）。点形状或颜色就不用图片了。prompt：AI 生成时默认的描述
export function AvatarPicker({ avatar, onChange, prompt }: { avatar: Avatar; onChange: (a: Avatar) => void; prompt: string }) {
  const shapes = useStore(s => s.shapes)
  const colors = useStore(s => s.colors)
  const plain = { shape: avatar.shape, color: avatar.color }
  return (
    <div className="avatar-picker">
      <div className="shape-row">
        {shapes.map(sh => (
          <button key={sh} title={SHAPE_NAMES[sh]} className={sh === avatar.shape && !avatar.image ? 'on' : ''} onClick={() => onChange({ ...plain, shape: sh })}>
            <BotAvatar avatar={{ shape: sh, color: avatar.color }} size={30} />
          </button>
        ))}
      </div>
      <div className="color-row">
        {colors.map(c => <button key={c} className={c === avatar.color && !avatar.image ? 'on' : ''} style={{ background: c }} onClick={() => onChange({ ...plain, color: c })} aria-label={c} />)}
      </div>
      <AvatarImageTools prompt={prompt} onImage={image => onChange({ ...plain, image })} />
      {avatar.image && <button className="ghost-btn" onClick={() => onChange(plain)}>{t('不用图片，改回形状')}</button>}
    </div>
  )
}

export function BotPanel({ onClose, tab, setTab }: { onClose: () => void; tab: string; setTab: (tab: string) => void }) {
  const bot = useStore(s => s.bots.find(b => b.chat_id === s.active))
  const tasks = useStore(s => s.tasks)
  const root = useRef<HTMLElement>(null)
  useBack(onClose, root)
  if (!bot) return null
  return (
    <aside className="panel" ref={root}>
      <div className="panel-head">
        <div className="panel-tabs">
          <button className={tab === 'computer' ? 'on' : ''} onClick={() => setTab('computer')}>{t('电脑')}</button>
          <button className={tab === 'tasks' ? 'on' : ''} onClick={() => setTab('tasks')}>{tc('tab', '任务')}</button>
          <button className={tab === 'library' ? 'on' : ''} onClick={() => setTab('library')}>{t('资料库')}</button>
          <button className={tab === 'skills' ? 'on' : ''} onClick={() => setTab('skills')}>{t('技能')}</button>
          <button className={tab === 'routines' ? 'on' : ''} onClick={() => setTab('routines')}>{t('定时')}</button>
          <button className={tab === 'events' ? 'on' : ''} onClick={() => setTab('events')}>{t('事件')}</button>
          <button className={tab === 'memory' ? 'on' : ''} onClick={() => setTab('memory')}>{t('记忆')}</button>
          <button className={tab === 'details' ? 'on' : ''} onClick={() => setTab('details')}>{tc('tab', '详情')}</button>
        </div>
        <button className="icon-btn" onClick={onClose}>✕</button>
      </div>
      {tab === 'computer'
        ? <div className="panel-body"><ComputerPanel key={bot.id} botId={bot.id} /></div>
        : tab === 'tasks' ? <TasksPanel key={bot.id} botId={bot.id} />
        : tab === 'library' ? <div className="panel-body"><BotLibraryPanel key={bot.id} bot={bot} /></div>
        : tab === 'skills' ? <div className="panel-body"><SkillsPanel key={bot.id} bot={bot} /></div>
        : tab === 'routines' ? <div className="panel-body"><RoutinesPanel key={bot.id} bot={bot} /></div>
        : tab === 'events' ? <div className="panel-body"><EventsPanel key={bot.id} bot={bot} /></div>
        : tab === 'memory' ? <div className="panel-body"><MemoryPanel key={bot.id} bot={bot} /></div>
        : <BotPanelInner key={bot.id} bot={bot} tasks={Object.values(tasks).filter(x => x.bot_id === bot.id)} onClose={onClose} />}
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
  const running = tasks.filter(x => x.status === 'running')

  const save = async () => {
    await api(`/api/bots/${bot.id}`, { method: 'PATCH', body: { name, instructions: instr, avatar } })
    setMsg(t('已保存')); setEditAvatar(false); setTimeout(() => setMsg(''), 1500)
  }
  const del = async () => {
    if (!(await ask(t('删除「{name}」？它的对话记录也会一起删除，无法恢复。', { name: bot.name }), { ok: t('删除'), danger: true }))) return
    try { await api(`/api/bots/${bot.id}`, { method: 'DELETE' }); onClose() } catch (e: any) { setMsg(e.message) }
  }

  return (
      <div className="panel-body">
        <button className="panel-avatar" onClick={() => setEditAvatar(!editAvatar)} title={t('修改头像')}><BotAvatar avatar={avatar} size={72} /></button>
        {editAvatar && <AvatarPicker avatar={avatar} onChange={setAvatar} prompt={botPrompt(name, instr)} />}
        <label className="field">{t('名字')}<input value={name} maxLength={40} onChange={e => setName(e.target.value)} /></label>
        <label className="field">{t('职责与要求')}
          <textarea rows={5} value={instr} placeholder={t('可以留空，直接在对话里交代。这里写的内容每次都会告诉 Bot。')} onChange={e => setInstr(e.target.value)} />
        </label>
        <div className="row-actions">
          <button className="btn primary" disabled={!dirty} onClick={save}>{t('保存')}</button>
          {msg && <span className="muted small">{msg}</span>}
        </div>

        <h4>{t('正在进行')}</h4>
        {running.length ? running.map(x => (
          <button key={x.id} className="mini-task" onClick={() => showTask(x.id)}><span className="dot" />#{x.id} {x.goal.slice(0, 60)}</button>
        )) : <div className="muted small">{t('没有正在进行的任务')}</div>}

        <h4>{t('管理')}</h4>
        {!bot.is_primary
          ? <button className="btn" onClick={() => api(`/api/bots/${bot.id}/primary`, { method: 'POST' })}>★ {t('设为主 Bot')}</button>
          : <div className="muted small">★ {t('这是主 Bot。')}</div>}
        {!bot.is_primary && <button className="btn danger" onClick={del}>{t('删除这个 Bot')}</button>}
        <ShareTemplate bot={bot} />
      </div>
  )
}
