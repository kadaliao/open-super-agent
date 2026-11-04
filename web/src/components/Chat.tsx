import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, approve, botForChat, loadOlder, useStore } from '../store'
import type { Approval, Bot, Message, Task } from '../types'
import BotAvatar from './BotAvatar'
import Composer from './Composer'
import Markdown from './Markdown'

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
const secs = (s: number) => (s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`)

// 旧版本的任务记录没有 kind，从文字里推断
function taskKind(m: Message): { kind: string; taskId?: number } {
  if (m.meta.kind) return { kind: m.meta.kind, taskId: m.meta.task_id ?? (m.meta.kind === 'dispatch' ? m.id : undefined) }
  if (m.content.startsWith('[任务已派发]')) return { kind: 'dispatch', taskId: m.id }
  const r = m.content.match(/^\[任务 #(\d+) 结果\]/)
  if (r) return { kind: 'result', taskId: Number(r[1]) }
  if (/^\[任务 #\d+ 已取消/.test(m.content)) return { kind: 'cancel' }
  if (m.content.startsWith('[触发器')) return { kind: 'trigger' }
  return { kind: 'note' }
}

function Thinking({ text, seconds, live }: { text: string; seconds?: number; live?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  return (
    <div className={'thinking' + (open ? ' open' : '')}>
      <button onClick={() => setOpen(!open)}>
        <span className={live ? 'pulse' : ''}>{live ? '正在思考' : `思考了 ${secs(seconds || 0)}`}</span>
        <span className="chev">›</span>
      </button>
      {open && <div className="thinking-body">{text}</div>}
    </div>
  )
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false)
  return <button className="ghost-btn" onClick={() => { navigator.clipboard?.writeText(text); setOk(true); setTimeout(() => setOk(false), 1200) }}>{ok ? '已复制' : '复制'}</button>
}

function BotMessage({ bot, m, children }: { bot?: Bot; m: Message; children?: React.ReactNode }) {
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Thinking text={m.meta.thinking} seconds={m.meta.thought_s} />
        <Markdown text={m.content} />
        {children}
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={m.content} /></div>
      </div>
    </div>
  )
}

function TaskCard({ m, task }: { m: Message; task?: Task }) {
  const status = task?.status || m.meta.status || 'done'
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (status !== 'running') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [status])
  const goal = m.content.replace(/^\[任务已派发\]\s*/, '')
  const label: Record<string, string> = { running: task?.waiting_approval ? '等你确认' : '进行中', done: '已完成', failed: '失败', cancelled: '已取消' }
  return (
    <div className={`task-card ${status}${task?.waiting_approval ? ' waiting' : ''}`}>
      <div className="task-top">
        <span className="dot" />
        <b>{m.meta.mode === 'heavy' ? 'Heavy 任务' : '后台任务'} #{m.id}</b>
        <span className="muted">{label[status] || status}{status === 'running' && task ? ` · ${secs((now / 1000) - task.started)}` : ''}{task?.steps ? ` · ${task.steps} 步` : ''}</span>
        {status === 'running' && <button className="ghost-btn" onClick={() => api(`/api/tasks/${m.id}/cancel`, { method: 'POST' })}>停止</button>}
      </div>
      <div className="task-goal">{goal}</div>
      {status === 'running' && task?.last_action && <div className="task-action">→ {task.last_action}</div>}
    </div>
  )
}

function ResultMessage({ bot, m }: { bot?: Bot; m: Message }) {
  const text = m.content.replace(/^\[任务 #\d+ 结果\]\n?/, '')
  const [open, setOpen] = useState(text.length < 1800)
  const status = m.meta.status || 'done'
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <div className={`result-head ${status}`}>
          {status === 'failed' ? '任务失败' : '任务完成'} · #{taskKind(m).taskId}{m.meta.elapsed ? ` · 用时 ${secs(m.meta.elapsed)}` : ''}
        </div>
        <div className={open ? '' : 'clamp'}><Markdown text={text} /></div>
        {!open && <button className="expand" onClick={() => setOpen(true)}>展开全文</button>}
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={text} /></div>
      </div>
    </div>
  )
}

function HelpCard({ a }: { a: Approval }) {
  const done = a.status && a.status !== 'pending'
  let reason = ''
  try { reason = JSON.parse(a.args)['需要你做的事'] } catch { reason = a.args }
  if (done) return <div className="sys-line">{a.status === 'approved' ? '✓ 你已处理完，Bot 继续' : '✕ 没有处理'}（任务 #{a.task_id}）</div>
  return (
    <div className="approval-card help">
      <div className="approval-title">🙋 需要你帮忙</div>
      <div className="help-reason">{reason}</div>
      <div className="muted small">任务 #{a.task_id} 已暂停。接手电脑处理完后点“交还”，Bot 会接着做。</div>
      <div className="approval-actions">
        <button className="btn" onClick={() => approve(a.id, false, '用户表示处理不了')}>我处理不了</button>
        <span className="grow" />
        <button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('osa-open-computer', { detail: { botId: a.bot_id, takeover: true } }))}>接手电脑</button>
        <button className="btn primary" onClick={() => api(`/api/bots/${a.bot_id}/computer/handback`, { method: 'POST' })}>处理完了，交还</button>
      </div>
    </div>
  )
}

function ApprovalCard({ a }: { a: Approval }) {
  if (a.kind === 'help') return <HelpCard a={a} />
  const [note, setNote] = useState('')
  const done = a.status && a.status !== 'pending'
  const label: Record<string, string> = { approved: '已允许', rejected: '已拒绝', expired: '超时未确认，按拒绝处理', cancelled: '任务已取消' }
  if (done) return <div className="sys-line">{a.status === 'approved' ? '✓' : '✕'} {label[a.status!] || a.status} <code>{a.tool}</code>（任务 #{a.task_id}）</div>
  return (
    <div className={'approval-card' + (done ? ' done' : '')}>
      <div className="approval-title">需要你确认 <code>{a.tool}</code></div>
      <div className="muted small">来自任务 #{a.task_id}：{a.goal}</div>
      <pre className="approval-args">{a.args}</pre>
      {done ? <div className="muted">{label[a.status!] || a.status}</div> : (
        <div className="approval-actions">
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="附一句意见（可选）" />
          <button className="btn" onClick={() => approve(a.id, false, note)}>拒绝</button>
          <button className="btn primary" onClick={() => approve(a.id, true, note)}>允许一次</button>
        </div>
      )}
    </div>
  )
}

const STARTERS = [
  '每天早上 9 点给我整理 AI 领域的重要新闻',
  '帮我调研一下自部署 SearxNG 的最佳实践',
  '你能做哪些事？先介绍一下你的能力',
]

export default function Chat({ onToggleDetails }: { onToggleDetails: () => void }) {
  const active = useStore(s => s.active)
  const bot = useStore(s => botForChat(s, s.active))
  const messages = useStore(s => (s.active ? s.messages[s.active] : undefined))
  const stream = useStore(s => (s.active ? s.streams[s.active] : undefined))
  const tasks = useStore(s => s.tasks)
  const approvals = useStore(s => s.approvals)
  const hasMore = useStore(s => (s.active ? s.hasMore[s.active] : false))
  const error = useStore(s => (s.active ? s.errors[s.active] : undefined))
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const prevHeight = useRef(0)

  // 新消息到达时，只有用户本来就在底部才自动滚到底
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (stick.current) el.scrollTop = el.scrollHeight
    else if (prevHeight.current && el.scrollTop < 40) el.scrollTop = el.scrollHeight - prevHeight.current
    prevHeight.current = el.scrollHeight
  }, [messages, stream?.text, stream?.thinking])
  useEffect(() => { stick.current = true }, [active])

  const onScroll = () => {
    const el = scroller.current!
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (el.scrollTop < 40 && hasMore && active) { prevHeight.current = el.scrollHeight; loadOlder(active) }
  }

  if (!active || !bot) return <main className="chat empty-main" />
  // 回复里记录了本轮派发的任务：任务卡片挂到回复下面，不再单独显示
  const byId = new Map((messages || []).map(m => [m.id, m]))
  const attached = new Set((messages || []).flatMap(m => (m.role === 'assistant' ? m.meta.tasks || [] : [])))
  const pending = Object.values(approvals).filter(a => a.chat_id === active && (a.status === 'pending' || Date.now() / 1000 - a.ts < 600))
  const empty = messages && messages.length === 0 && !stream

  return (
    <main className="chat">
      <header className="chat-head">
        <button className="head-bot" onClick={onToggleDetails} title="Bot 详情">
          <BotAvatar avatar={bot.avatar} size={26} />
          <span className="head-name">{bot.name}</span>
          {bot.is_primary && <span className="star" title="主 Bot">★</span>}
        </button>
        <button className="icon-btn" onClick={onToggleDetails} title="详情">☰</button>
      </header>
      <div className="scroller" ref={scroller} onScroll={onScroll}>
        <div className="thread">
          {empty && (
            <div className="welcome">
              <BotAvatar avatar={bot.avatar} size={72} />
              <h2>{bot.name}</h2>
              <p className="muted">给 {bot.name} 布置第一份工作。它会记住你交代的职责，在后台替你完成任务。</p>
              <div className="starters">{STARTERS.map(s => <button key={s} onClick={() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: s }))}>{s}</button>)}</div>
            </div>
          )}
          {messages?.map(m => {
            if (m.role === 'user') return <div key={m.id} className="msg user"><div className="bubble">{m.content}</div></div>
            if (m.role === 'assistant') return m.content || m.meta.tasks?.length ? (
              <BotMessage key={m.id} bot={bot} m={m}>
                {(m.meta.tasks || []).map((tid: number) => byId.get(tid) && <TaskCard key={tid} m={byId.get(tid)!} task={tasks[tid]} />)}
              </BotMessage>) : null
            const k = taskKind(m)
            if (k.kind === 'dispatch') return attached.has(m.id) ? null : <TaskCard key={m.id} m={m} task={tasks[m.id]} />
            if (k.kind === 'result') return <ResultMessage key={m.id} bot={bot} m={m} />
            if (k.kind === 'trigger') return <div key={m.id} className="sys-line">⚡ {m.content.replace(/^\[触发器 ([^\]]+)\]/, '触发器 $1：').slice(0, 160)}</div>
            return <div key={m.id} className="sys-line">{m.content.replace(/^\[|\]$/g, '').slice(0, 160)}</div>
          })}
          {stream && (
            <div className="msg bot">
              <BotAvatar avatar={bot.avatar} size={28} busy />
              <div className="msg-body">
                <Thinking text={stream.thinking} live={!stream.text} seconds={(Date.now() - stream.started) / 1000} />
                {stream.text ? <Markdown text={stream.text + ' ▍'} /> : !stream.thinking && <div className="typing"><i /><i /><i /></div>}
              </div>
            </div>
          )}
          {pending.map(a => <ApprovalCard key={a.id} a={a} />)}
          {error && <div className="error-line">{error}</div>}
        </div>
      </div>
      <Composer chatId={active} bot={bot} busy={!!stream} />
    </main>
  )
}
