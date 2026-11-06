import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, approve, chatById, loadOlder, openChat, openLibrary, showTask, useStore } from '../store'
import type { Approval, Bot, Message, Task } from '../types'
import BotAvatar from './BotAvatar'
import Composer from './Composer'
import Markdown from './Markdown'

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
const VIA: Record<string, string> = {
  telegram: '来自 Telegram', feishu: '来自飞书', dingtalk: '来自钉钉', wecom: '来自企业微信', discord: '来自 Discord',
}
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

// 群聊里每条消息上面标出是哪个 Bot 说的
const Speaker = ({ bot, show }: { bot?: Bot; show?: boolean }) => show ? <div className="speaker">{bot?.name || '已删除的 Bot'}</div> : null

function BotMessage({ bot, m, children, showName }: { bot?: Bot; m: Message; children?: React.ReactNode; showName?: boolean }) {
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Speaker bot={bot} show={showName} />
        <Thinking text={m.meta.thinking} seconds={m.meta.thought_s} />
        <Markdown text={m.content} />
        {children}
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={m.content} /></div>
      </div>
    </div>
  )
}

function TaskCard({ m, task, owner, handoffTo, replyTo }: { m: Message; task?: Task; owner?: Bot; handoffTo?: Bot; replyTo?: Bot }) {
  const status = task?.status || m.meta.status || 'done'
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (status !== 'running') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [status])
  const goal = m.content.replace(/^\[任务已派发\]\s*/, '')
  const label: Record<string, string> = { running: task?.waiting_approval ? '等你确认' : '进行中', done: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
  return (
    <div className={`task-card ${status}${task?.waiting_approval ? ' waiting' : ''}`}>
      <div className="task-top">
        <span className="dot" />
        <b>{owner ? `${owner.name} 的` : ''}{m.meta.mode === 'heavy' ? 'Heavy 任务' : '后台任务'} #{m.id}</b>
        <span className="muted">{label[status] || status}{status === 'running' && task ? ` · ${secs((now / 1000) - task.started)}` : ''}{task?.steps ? ` · ${task.steps} 步` : ''}</span>
        <button className="ghost-btn" title="看这个任务每一步做了什么" onClick={() => showTask(m.id)}>过程</button>
        {status === 'running' && <button className="ghost-btn" onClick={() => api(`/api/tasks/${m.id}/cancel`, { method: 'POST' })}>停止</button>}
      </div>
      <div className="task-goal">{goal}</div>
      {handoffTo && <div className="task-action">做完交给 {handoffTo.name}</div>}
      {replyTo && <div className="task-action">做完把结果回给 {replyTo.name}</div>}
      {status === 'running' && task?.last_action && <div className="task-action">→ {task.last_action}</div>}
    </div>
  )
}

// 任务结果存进资料库，标题取任务描述的第一行
function SavePageBtn({ bot, text, goal, taskId }: { bot?: Bot; text: string; goal: string; taskId?: number }) {
  const [saved, setSaved] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  if (saved) return <button className="ghost-btn" onClick={() => openLibrary(saved)}>已存为页面 · 打开</button>
  const save = async () => {
    setBusy(true)
    try {
      const h1 = text.match(/^#\s+(.+)$/m)?.[1]
      const title = (h1 || goal.split('\n').find(l => l.trim()) || `任务 #${taskId} 的结果`).replace(/^\[定时任务 [^\]]+\]\s*/, '').slice(0, 60)
      const p = await api('/api/pages', { method: 'POST', body: { bot_id: bot?.id, title, content: text, task_id: taskId } })
      setSaved(p.id)
    } finally { setBusy(false) }
  }
  return <button className="ghost-btn" disabled={busy} onClick={save}>存为页面</button>
}

function ResultMessage({ bot, m, goal, showName }: { bot?: Bot; m: Message; goal: string; showName?: boolean }) {
  const text = m.content.replace(/^\[任务 #\d+ 结果\]\n?/, '')
  const [open, setOpen] = useState(text.length < 1800)
  const status = m.meta.status || 'done'
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Speaker bot={bot} show={showName} />
        <div className={`result-head ${status}`}>
          {status === 'failed' ? '任务失败' : '任务完成'} · #{taskKind(m).taskId}{m.meta.elapsed ? ` · 用时 ${secs(m.meta.elapsed)}` : ''}
        </div>
        <div className={open ? '' : 'clamp'}><Markdown text={text} /></div>
        {!open && <button className="expand" onClick={() => setOpen(true)}>展开全文</button>}
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={text} />
          {status === 'done' && <SavePageBtn bot={bot} text={text} goal={goal} taskId={taskKind(m).taskId} />}
          {taskKind(m).taskId && <button className="ghost-btn" onClick={() => showTask(taskKind(m).taskId!)}>查看过程</button>}</div>
      </div>
    </div>
  )
}

// 群里派的任务：卡片在私聊里，注明来自哪个群
function Origin({ a }: { a: Approval }) {
  const g = useStore(s => (a.origin_chat_id && a.origin_chat_id !== a.chat_id ? chatById(s, a.origin_chat_id) : undefined))
  return g ? <> · 在群聊「<button className="link-btn" onClick={() => openChat(g.id)}>{g.title}</button>」里派的</> : null
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
      <div className="muted small">任务 #{a.task_id} 已暂停<Origin a={a} />。接手电脑处理完后点“交还”，Bot 会接着做。</div>
      <div className="approval-actions">
        <button className="btn" onClick={() => approve(a.id, false, '用户表示处理不了')}>我处理不了</button>
        <span className="grow" />
        <button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('osa-open-computer', { detail: { botId: a.bot_id, takeover: true, taskId: a.task_id } }))}>接手电脑</button>
        <button className="btn primary" onClick={() => api(`/api/bots/${a.bot_id}/computer/handback`, { method: 'POST' })}>处理完了，交还</button>
      </div>
    </div>
  )
}

function ApprovalCard({ a }: { a: Approval }) {
  if (a.kind === 'help') return <HelpCard a={a} />
  const [note, setNote] = useState('')
  const done = a.status && a.status !== 'pending'
  const label: Record<string, string> = { approved: a.always ? '已允许，以后自动允许' : '已允许', rejected: '已拒绝', expired: '超时未确认，按拒绝处理', cancelled: '任务已取消' }
  if (done) return <div className="sys-line">{a.status === 'approved' ? '✓' : '✕'} {label[a.status!] || a.status} <code>{a.tool}</code>（任务 #{a.task_id}）</div>
  const [app, tool] = a.tool.includes('__') ? a.tool.split('__', 2) : ['', a.tool]
  return (
    <div className="approval-card">
      <div className="approval-title">需要你确认{app && <> · 应用 <b>@{app}</b></>} <code>{tool}</code></div>
      <div className="muted small">来自任务 #{a.task_id}<Origin a={a} />：{a.goal}
        {a.expires ? ` · 后台任务，${new Date(a.expires * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 前没确认按拒绝处理` : ''}</div>
      <pre className="approval-args">{a.args}</pre>
      <div className="approval-actions">
        <input value={note} onChange={e => setNote(e.target.value)} placeholder="附一句意见（可选）" />
        <button className="btn" onClick={() => approve(a.id, false, note)}>拒绝</button>
        <button className="btn" onClick={() => approve(a.id, true, note)}>允许一次</button>
        <button className="btn primary" title={`以后 ${a.tool} 不再询问，可在设置 → 确认规则里撤销`} onClick={() => approve(a.id, true, note, true)}>总是允许</button>
      </div>
    </div>
  )
}

function HandoffLine({ from, to, text }: { from?: Bot; to?: Bot; text: string }) {
  return (
    <div className="handoff">
      {from && <BotAvatar avatar={from.avatar} size={18} />}<b>{from?.name || '已删除的 Bot'}</b>
      <span className="muted">交给</span>
      {to && <BotAvatar avatar={to.avatar} size={18} />}<b>{to?.name || '已删除的 Bot'}</b>
      <span className="handoff-text">{text}</span>
    </div>
  )
}

// Bot 之间的私信：谁私信了谁、说了什么；点名字去看对方那边
function DmLine({ m, from, to, here }: { m: Message; from?: Bot; to?: Bot; here: number }) {
  const [open, setOpen] = useState(m.content.length < 240)
  // 在收信的私聊里，链接回发信的那个对话；在发信的对话里，链接到收信 Bot 的私聊
  const there = m.meta.chat && m.meta.chat !== here ? m.meta.chat : to && to.chat_id !== here ? to.chat_id : null
  return (
    <div className="handoff dm">
      <span className="dm-icon">✉</span>
      {from && <BotAvatar avatar={from.avatar} size={18} />}<b>{from?.name || '已删除的 Bot'}</b>
      <span className="muted">私信</span>
      {to && <BotAvatar avatar={to.avatar} size={18} />}<b>{to?.name || '已删除的 Bot'}</b>
      {there && <button className="link-btn small" onClick={() => openChat(there)}>{m.meta.chat && m.meta.chat !== here ? '去发信的对话' : `去看 ${to?.name}`}</button>}
      <span className={'handoff-text' + (open ? '' : ' clamp3')}>{m.content}</span>
      {!open && <button className="expand" onClick={() => setOpen(true)}>展开</button>}
    </div>
  )
}

// 私信的回信：显示成发信 Bot 的一条消息，长的折起来
function DmReply({ m, bot, to }: { m: Message; bot?: Bot; to?: Bot }) {
  const [open, setOpen] = useState(m.content.length < 1800)
  const bad = m.meta.status && m.meta.status !== 'done'
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <div className="speaker">{bot?.name || '已删除的 Bot'}</div>
        <div className={'result-head' + (bad ? ' failed' : '')}>↩ 回了 {to?.name || '已删除的 Bot'} 的私信{m.meta.task_id ? ` · 任务 #${m.meta.task_id}` : ''}</div>
        <div className={open ? '' : 'clamp'}><Markdown text={m.content} /></div>
        {!open && <button className="expand" onClick={() => setOpen(true)}>展开全文</button>}
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={m.content} />
          {bot && <button className="ghost-btn" onClick={() => openChat(bot.chat_id)}>去 {bot.name} 的私聊</button>}
          {m.meta.task_id && <button className="ghost-btn" onClick={() => showTask(m.meta.task_id)}>查看过程</button>}</div>
      </div>
    </div>
  )
}

const STARTERS = [
  '每天早上 9 点给我整理 AI 领域的重要新闻',
  '帮我调研一下自部署 SearxNG 的最佳实践',
  '你能做哪些事？先介绍一下你的能力',
]

export function GroupAvatars({ bots, size = 26 }: { bots: Bot[]; size?: number }) {
  return (
    <span className="group-avatars" style={{ width: size + (Math.min(bots.length, 3) - 1) * size * 0.45 }}>
      {bots.slice(0, 3).map((b, i) => <span key={b.id} style={{ left: i * size * 0.45, zIndex: 3 - i }}><BotAvatar avatar={b.avatar} size={size} /></span>)}
    </span>
  )
}

export default function Chat({ onToggleDetails }: { onToggleDetails: () => void }) {
  const active = useStore(s => s.active)
  const chat = useStore(s => chatById(s, s.active))
  const allBots = useStore(s => s.bots)
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

  if (!active || !chat) return <main className="chat empty-main" />
  const group = chat.kind === 'group'
  const botOf = (id: number | null | undefined) => allBots.find(b => b.id === id)
  const members = chat.bot_ids.map(botOf).filter((b): b is Bot => !!b)
  const bot = group ? undefined : members[0]
  if (!group && !bot) return <main className="chat empty-main" />
  const speaker = (m: Message) => (group ? botOf(m.bot_id) : bot)
  const lead = botOf(chat.lead)
  // 回复里记录了本轮派发的任务：任务卡片挂到回复下面，不再单独显示
  const byId = new Map((messages || []).map(m => [m.id, m]))
  const attached = new Set((messages || []).flatMap(m => (m.role === 'assistant' ? m.meta.tasks || [] : [])))
  const pending = Object.values(approvals).filter(a => a.chat_id === active && (a.status === 'pending' || Date.now() / 1000 - a.ts < 600))
  // 群里派的任务要确认时，卡片在 Bot 的私聊里；这里只放一条提示
  const elsewhere = Object.values(approvals).filter(a => a.origin_chat_id === active && a.chat_id !== active && a.status === 'pending')
  const empty = messages && messages.length === 0 && !stream
  const title = group ? chat.title : bot!.name
  const task = (tid: number) => byId.get(tid) && <TaskCard key={tid} m={byId.get(tid)!} task={tasks[tid]}
    owner={group ? botOf(byId.get(tid)!.bot_id) : undefined} handoffTo={botOf(byId.get(tid)!.meta.handoff_to)}
    replyTo={botOf(byId.get(tid)!.meta.reply_to)} />
  const starters = group && members.length >= 2
    ? ['@所有人 先各自介绍一下你们能做什么', `@${members[0].name} 查一下今天的 AI 新闻，查完交给 @${members[1].name} 写一段 100 字的总结`]
    : STARTERS

  return (
    <main className="chat">
      <header className="chat-head">
        <button className="head-bot" onClick={onToggleDetails} title={group ? '群聊设置' : 'Bot 详情'}>
          {group ? <GroupAvatars bots={members} /> : <BotAvatar avatar={bot!.avatar} size={26} />}
          <span className="head-name">{title}</span>
          {group ? <span className="muted small">{members.length} 个 Bot</span> : bot!.is_primary && <span className="star" title="主 Bot">★</span>}
        </button>
        <div className="head-actions">
          <button className="ghost-btn" title="之前的对话不再带进上下文，省 token；长期记忆还在" onClick={() => {
            if (confirm(`开始新话题？${group ? '群里的 Bot' : bot!.name} 不再参考之前的对话（记录还在，长期记忆也还在）。`)) api(`/api/chats/${active}/reset`, { method: 'POST' })
          }}>新话题</button>
          <button className="icon-btn" onClick={onToggleDetails} title="详情">☰</button>
        </div>
      </header>
      <div className="scroller" ref={scroller} onScroll={onScroll}>
        <div className="thread">
          {empty && (group ? (
            <div className="welcome">
              <GroupAvatars bots={members} size={56} />
              <h2>{chat.title}</h2>
              <p className="muted">@ 某个 Bot 让它来做，@所有人 让大家都说说；不 @ 的消息由协调者{lead ? ` ${lead.name} ` : ''}接，它会分给合适的 Bot。
                Bot 之间可以交接工作；要你确认的操作会出现在各自的私聊里。</p>
              <div className="starters">{starters.map(s => <button key={s} onClick={() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: s }))}>{s}</button>)}</div>
            </div>
          ) : (
            <div className="welcome">
              <BotAvatar avatar={bot!.avatar} size={72} />
              <h2>{bot!.name}</h2>
              <p className="muted">给 {bot!.name} 布置第一份工作。它会记住你交代的职责，在后台替你完成任务。</p>
              <div className="starters">{starters.map(s => <button key={s} onClick={() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: s }))}>{s}</button>)}</div>
            </div>
          ))}
          {messages?.map(m => {
            if (m.role === 'user') return <div key={m.id} className="msg user">
              {m.meta.via && <span className="via">{VIA[m.meta.via] || m.meta.via}</span>}<div className="bubble">{m.content}</div></div>
            if (m.role === 'assistant') return m.content || m.meta.tasks?.length ? (
              <BotMessage key={m.id} bot={speaker(m)} m={m} showName={group}>
                {(m.meta.tasks || []).map(task)}
              </BotMessage>) : null
            const k = taskKind(m)
            if (k.kind === 'dispatch') return attached.has(m.id) ? null : task(m.id)
            if (k.kind === 'result') return <ResultMessage key={m.id} bot={speaker(m)} m={m} showName={group}
              goal={(k.taskId && byId.get(k.taskId)?.content.replace(/^\[任务已派发\]\s*/, '')) || ''} />
            if (k.kind === 'handoff') return <HandoffLine key={m.id} from={botOf(m.bot_id)} to={botOf(m.meta.to)} text={m.meta.note || m.content} />
            if (k.kind === 'dm') return <DmLine key={m.id} m={m} from={botOf(m.bot_id)} to={botOf(m.meta.to)} here={chat.id} />
            if (k.kind === 'dm_reply' && m.meta.sent) return <div key={m.id} className="sys-line">↩ 已把回信发给 {botOf(m.meta.to)?.name || '已删除的 Bot'}
              {m.meta.chat && m.meta.chat !== chat.id && <> · <button className="link-btn" onClick={() => openChat(m.meta.chat)}>去看</button></>}</div>
            if (k.kind === 'dm_reply') return <DmReply key={m.id} m={m} bot={botOf(m.bot_id)} to={botOf(m.meta.to)} />
            if (k.kind === 'reset') return <div key={m.id} className="topic-line"><span>新话题 · 之前的对话不再带进上下文，长期记忆还在</span></div>
            if (k.kind === 'trigger') return <div key={m.id} className="sys-line">⚡ {m.content.replace(/^\[触发器 ([^\]]+)\]/, '触发器 $1：').slice(0, 160)}</div>
            return <div key={m.id} className="sys-line">{m.content.replace(/^\[|\]$/g, '').slice(0, 160)}</div>
          })}
          {stream && (() => {
            const who = group ? botOf(stream.bot_id) : bot
            return (
              <div className="msg bot">
                {who && <BotAvatar avatar={who.avatar} size={28} busy />}
                <div className="msg-body">
                  <Speaker bot={who} show={group} />
                  <Thinking text={stream.thinking} live={!stream.text} seconds={(Date.now() - stream.started) / 1000} />
                  {stream.text ? <Markdown text={stream.text + ' ▍'} /> : !stream.thinking && <div className="typing"><i /><i /><i /></div>}
                </div>
              </div>
            )
          })()}
          {pending.map(a => <ApprovalCard key={a.id} a={a} />)}
          {elsewhere.map(a => {
            const b = botOf(a.bot_id)
            return (
              <div key={a.id} className="elsewhere">
                <span>⚠ {b?.name} 的任务 #{a.task_id} {a.kind === 'help' ? '需要你帮忙' : '需要你确认'}，卡片在它的私聊里</span>
                {b && <button className="btn" onClick={() => openChat(b.chat_id)}>去处理</button>}
              </div>
            )
          })}
          {error && <div className="error-line">{error}</div>}
        </div>
      </div>
      <SetupHint />
      <Composer chatId={active} bot={bot} members={group ? members : []}
        busy={stream ? (group ? botOf(stream.bot_id)?.name || '有人' : bot!.name) : ''} />
    </main>
  )
}

// 对话或后台任务的模型还没法用（比如没填 API Key）：提示去设置里配好
function SetupHint() {
  const problem = useStore(s => s.settings?.problem)
  if (!problem) return null
  return (
    <div className="setup-hint">
      <span>⚠ {problem}，Bot 现在没法回复。</span>
      <button className="btn small" onClick={() => window.dispatchEvent(new CustomEvent('osa-open-settings', { detail: 'models' }))}>去配置模型</button>
    </div>
  )
}
