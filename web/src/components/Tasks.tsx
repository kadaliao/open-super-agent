import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, dropTask, fetchTask, loadTask, showTask, useStore } from '../store'
import type { Task, TaskInfo, TaskStep } from '../types'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'

// 右栏“任务”：Bot 做过的后台任务。正在做的实时更新，做完的点开回看每一步

const secs = (s: number) => {
  s = Math.max(0, Math.floor(s))
  return s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 3600)} 小时 ${Math.floor(s % 3600 / 60)} 分`
}
const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
function day(ts: number) {
  const d = new Date(ts * 1000), now = new Date()
  const n = Math.round((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000)
  if (n === 0) return '今天'
  if (n === 1) return '昨天'
  return d.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) })
}
const firstLine = (s: string) => s.split('\n').find(l => l.trim())?.trim() || s

const STATUS: Record<string, string> = { running: '进行中', done: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
export const taskLabel = (t: { status: string; waiting_approval?: number | null }) =>
  t.status === 'running' && t.waiting_approval ? '等你确认' : STATUS[t.status] || t.status
// Heavy 模式里是哪个研究员
const worker = (w: string) => (w === 'review' ? '汇总' : /^h\d+$/.test(w) ? `研究员 ${Number(w.slice(1)) + 1}` : '')

// 每秒刷新一次：正在跑的显示已经跑了多久
function useNow(on: boolean) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!on) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [on])
  return now / 1000
}

// 数据库里的记录盖上实时状态（store 里的 tasks 跟着 task 事件更新）
function withLive(t: TaskInfo, live?: Task): TaskInfo {
  if (!live) return t
  return { ...t, status: live.status, steps: live.steps, started: live.started, waiting_approval: live.waiting_approval,
    last_action: live.last_action, elapsed: live.status === 'running' ? t.elapsed : live.elapsed }
}

// botId：这个 Bot 做的任务（含在群里做的）；chatId：这个群里派的任务
export function TasksPanel({ botId, chatId }: { botId?: number; chatId?: number }) {
  const open = useStore(s => s.openTask)
  useEffect(() => () => showTask(null), [])   // 换了 Bot、切走标签：下次回到列表
  return open ? <TaskDetail key={open} id={open} group={!!chatId} /> : <TaskList botId={botId} chatId={chatId} />
}

function TaskList({ botId, chatId }: { botId?: number; chatId?: number }) {
  const q = botId ? `bot_id=${botId}` : `chat_id=${chatId}`
  const tasks = useStore(s => s.tasks)
  const bots = useStore(s => s.bots)
  const [list, setList] = useState<TaskInfo[] | null>(null)
  const [more, setMore] = useState(false)
  const [err, setErr] = useState('')
  const mine = Object.values(tasks).filter(t => (botId ? t.bot_id === botId : t.chat_id === chatId))
  const changed = mine.map(t => `${t.id}:${t.status}`).join(',')   // 有任务开始或结束：重新拉列表
  const now = useNow(mine.some(t => t.status === 'running'))
  useEffect(() => {
    api<TaskInfo[]>(`/api/tasks?${q}&limit=30`).then(r => { setList(r); setMore(r.length === 30) }).catch(e => setErr(e.message))
  }, [q, changed])
  const older = async () => {
    const r = await api<TaskInfo[]>(`/api/tasks?${q}&before=${list!.at(-1)!.id}&limit=30`)
    setList([...list!, ...r]); setMore(r.length === 30)
  }

  if (err) return <div className="panel-body"><div className="error-line">{err}</div></div>
  if (!list) return <div className="panel-body"><div className="muted small">加载中…</div></div>
  let lastDay = ''
  return (
    <div className="panel-body">
      <p className="muted small">后台任务每一步做了什么都记在这里。正在做的实时更新，做完的也能点开回看。</p>
      {!list.length && <div className="muted small">还没有派过任务</div>}
      {list.map(raw => {
        const t = withLive(raw, tasks[raw.id])
        const d = day(t.ts), head = d !== lastDay
        lastDay = d
        const owner = chatId ? bots.find(b => b.id === t.bot_id) : undefined
        const running = t.status === 'running'
        const dur = running && t.started ? `已 ${secs(now - t.started)}` : t.elapsed ? `用时 ${secs(t.elapsed)}` : ''
        return (
          <Fragment key={t.id}>
            {head && <h4>{d}</h4>}
            <button className={`task-row ${t.status}${t.waiting_approval ? ' waiting' : ''}`} onClick={() => showTask(t.id)}>
              <span className="dot" />
              <span className="task-row-main">
                <span className="task-row-goal">{owner && <BotAvatar avatar={owner.avatar} size={16} />}<span>{firstLine(t.goal)}</span></span>
                <span className="task-row-meta">
                  {[hm(t.ts), taskLabel(t), dur, t.steps ? `${t.steps} 步` : '', t.mode === 'heavy' ? 'Heavy' : ''].filter(Boolean).join(' · ')}
                </span>
                {running && t.last_action && <span className="task-action">→ {t.last_action}</span>}
              </span>
            </button>
          </Fragment>
        )
      })}
      {more && <button className="btn small-btn" onClick={older}>更早的任务</button>}
    </div>
  )
}

function TaskDetail({ id, group }: { id: number; group: boolean }) {
  const steps = useStore(s => s.taskSteps[id])
  const live = useStore(s => s.tasks[id])
  const [info, setInfo] = useState<TaskInfo | null>(null)
  const [only, setOnly] = useState('')   // Heavy：只看哪个研究员
  const owner = useStore(s => s.bots.find(b => b.id === (info?.bot_id ?? live?.bot_id)))
  const [err, setErr] = useState('')
  useEffect(() => {
    loadTask(id).then(t => t && setInfo(t)).catch(e => setErr(e.message))
    return () => dropTask(id)
  }, [id])
  // 刚做完：再拉一次详情，拿到用时和花费
  useEffect(() => {
    if (info?.status === 'running' && live && live.status !== 'running') fetchTask(id).then(setInfo).catch(() => {})
  }, [live?.status, info?.status])

  const t = info ? withLive(info, live) : null
  const running = t?.status === 'running'
  const now = useNow(running)

  // 正在跑的从最新一步看起，跟着往下滚；做完的从头看
  const box = useRef<HTMLDivElement>(null)
  const stick = useRef<boolean | null>(null)
  const loaded = !!(t && steps)
  useLayoutEffect(() => {
    const el = box.current
    if (!el || !loaded) return
    if (stick.current === null) stick.current = running
    if (stick.current) el.scrollTop = el.scrollHeight
  }, [steps, loaded])
  const onScroll = () => {
    const el = box.current
    if (el && stick.current !== null) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  const back = <button className="link-btn" onClick={() => showTask(null)}>← 所有任务</button>
  if (err) return <div className="panel-body">{back}<div className="error-line">{err}</div></div>
  if (!t || !steps) return <div className="panel-body">{back}<div className="muted small">加载中…</div></div>
  const heavy = t.mode === 'heavy'
  const dur = running && t.started ? `已 ${secs(now - t.started)}` : t.elapsed ? `用时 ${secs(t.elapsed)}` : ''
  const openTool = [...steps].reverse().find(s => s.kind === 'tool' && s.status === 'running')
  const busy = steps.some(s => s.status === 'running' || s.status === 'waiting')
  // Heavy：几个研究员的步骤交错在一起，可以只看其中一个
  const workers = heavy ? [...new Set(steps.map(s => s.worker).filter(Boolean))].sort() : []
  const shown = only ? steps.filter(s => !s.worker || s.worker === only) : steps
  return (
    <div className="panel-body task-detail" ref={box} onScroll={onScroll}>
      <div className="td-nav">{back}
        {running && <button className="btn small" onClick={() => api(`/api/tasks/${id}/cancel`, { method: 'POST' })}>停止</button>}</div>
      <div className={`td-head ${t.status}${t.waiting_approval ? ' waiting' : ''}`}>
        <span className="dot" /><b>{heavy ? 'Heavy 任务' : '后台任务'} #{t.id}</b><span className="muted">{taskLabel(t)}</span>
      </div>
      <div className="td-meta">
        {[group && owner?.name, `${day(t.ts)} ${hm(t.ts)}`, dur, t.steps ? `${t.steps} 步` : '',
          t.cost ? `花费 ${t.currency || ''}${t.cost.toFixed(t.cost < 1 ? 3 : 2)}` : ''].filter(Boolean).join(' · ')}
      </div>
      <Goal text={t.goal} />
      {!steps.length && !running ? <>
        <div className="muted small">这个任务是在更新之前做的，没有记下过程。</div>
        {t.result && <div className="step-result"><Markdown text={t.result} /></div>}
      </> : (
        <div className="steps">
          {workers.length > 1 && <div className="step-filter">
            {['', ...workers].map(w => <button key={w} className={only === w ? 'on' : ''} onClick={() => setOnly(w)}>{w ? worker(w) : '全部'}</button>)}
          </div>}
          {shown.map(s => <Step key={s.id} s={s} waiting={!!t.waiting_approval && s === openTool} />)}
          {running && !busy && <div className="step-wait"><span className="spin" />正在想下一步…</div>}
        </div>
      )}
    </div>
  )
}

function Goal({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 160 || text.split('\n').length > 3
  return (
    <div className="td-goal">
      <div className={long && !open ? 'td-goal-text clamp3' : 'td-goal-text'}>{text}</div>
      {long && <button className="expand" onClick={() => setOpen(!open)}>{open ? '收起' : '展开全文'}</button>}
    </div>
  )
}

const ICON: Record<string, string> = { done: '✓', error: '✕', rejected: '⊘', cancelled: '–', interrupted: '–' }

function pretty(raw: string) {
  if (!raw || raw === '{}') return ''
  try {
    const v = JSON.parse(raw)
    return v && typeof v === 'object' && !Object.keys(v).length ? '' : JSON.stringify(v, null, 2)
  } catch { return raw }
}

function Step({ s, waiting }: { s: TaskStep; waiting: boolean }) {
  const live = s.status === 'running' || s.status === 'waiting'
  const draft = s.kind === 'answer' && /^h\d+$/.test(s.worker)
  const [toggled, setOpen] = useState<boolean | null>(null)
  const open = toggled ?? (s.kind === 'answer' && !draft)   // 结果默认展开，草稿、思考、工具默认收起
  const now = useNow(live && s.kind === 'tool')   // 工具在跑：每秒更新用时
  const who = worker(s.worker)
  const tag = who ? <span className="step-who">{who}</span> : null
  if (s.kind === 'note') return <div className={`step-note s-${s.status}`}>{tag}{s.title}</div>
  if (s.kind === 'say') return <div className="step-say">{tag}{s.body}</div>
  if (s.kind === 'text') return (   // 正在写的文字：边收边显示
    <div className="step-text">{tag}<Markdown text={s.body + (live ? ' ▍' : '')} /></div>
  )
  const title = s.kind === 'think' ? <>{live ? '正在思考' : '思考'}<span className="step-peek">{firstLine(s.body).slice(0, 80)}</span></>
    : s.kind === 'answer' ? (draft ? '草稿' : '写出结果') : s.title
  const dur = s.kind === 'answer' ? '' : live ? (s.kind === 'tool' ? secs(now - s.ts) : '') : s.dur >= 1 ? secs(s.dur) : ''
  const args = s.kind === 'tool' ? pretty(s.detail) : ''
  return (
    <div className={`step k-${s.kind} s-${s.status}${open ? ' open' : ''}`}>
      <button className="step-head" onClick={() => setOpen(!open)}>
        <span className="step-icon">{live ? <span className="spin" /> : ICON[s.status] || '✓'}</span>
        <span className={'step-title' + (live && s.kind === 'think' ? ' pulse' : '')}>{title}</span>
        {tag}
        {(waiting || s.status === 'waiting') && <span className="step-state">等你确认</span>}
        {s.status === 'rejected' && <span className="step-state bad">被拒绝</span>}
        <span className="step-dur">{dur}</span>
        <span className="chev">›</span>
      </button>
      {open && (s.kind === 'think' ? <div className="step-body step-think">{s.body || '…'}</div>
        : s.kind === 'answer' ? <div className="step-body"><Markdown text={s.body} /></div>
        : <div className="step-body">
            {args && <><div className="step-label">参数</div><pre className="step-pre">{args}</pre></>}
            <div className="step-label">返回</div>
            {s.body ? <pre className="step-pre">{s.body}</pre> : <div className="muted small">{live ? '还在做…' : '（没有返回内容）'}</div>}
          </div>)}
    </div>
  )
}
