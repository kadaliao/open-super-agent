import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, canRetry, dropTask, fetchTask, loadTask, retryTask, showTask, taskEnded, taskFocused, taskLabel, taskWaiting, tellTask, useStore, waitLabel } from '../store'
import { lang, locale, t, tc } from '../i18n'
import type { Task, TaskInfo, TaskStep } from '../types'
import { isEnter } from '../keys'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'
import { DoubtCard, QuestionCard } from './Chat'
import { notice } from './Dialog'

// 右栏“任务”：Bot 做过的后台任务。正在做的实时更新，做完的点开回看每一步

const secs = (s: number) => {
  s = Math.max(0, Math.floor(s))
  return s < 60 ? t('{s} 秒', { s }) : s < 3600 ? t('{m} 分 {s} 秒', { m: Math.floor(s / 60), s: s % 60 })
    : t('{h} 小时 {m} 分', { h: Math.floor(s / 3600), m: Math.floor(s % 3600 / 60) })
}
// 英文 12 小时制不补零（9:05 AM），中文保持 09:05
const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: lang === 'en' ? 'numeric' : '2-digit', minute: '2-digit' })
function day(ts: number) {
  const d = new Date(ts * 1000), now = new Date()
  const n = Math.round((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000)
  if (n === 0) return t('今天')
  if (n === 1) return t('昨天')
  return d.toLocaleDateString(locale, { month: 'long', day: 'numeric', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) })
}
const firstLine = (s: string) => s.split('\n').find(l => l.trim())?.trim() || s

// Heavy 模式里是哪个研究员
const worker = (w: string) => (w === 'review' ? t('汇总') : /^h\d+$/.test(w) ? t('研究员 {n}', { n: Number(w.slice(1)) + 1 }) : '')
// 跑了多久 / 用了多久
const duration = (tk: TaskInfo, now: number) =>
  tk.status === 'running' && tk.started ? t('已 {d}', { d: secs(now - tk.started) }) : tk.elapsed ? t('用时 {d}', { d: secs(tk.elapsed) }) : ''

// 每秒刷新一次：正在跑的显示已经跑了多久
function useNow(on: boolean) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!on) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [on])
  return now / 1000
}

// 数据库里的记录盖上实时状态（store 里的 tasks 跟着 task 事件更新）
function withLive(tk: TaskInfo, live?: Task): TaskInfo {
  if (!live) return tk
  return { ...tk, status: live.status, steps: live.steps, started: live.started, waiting_approval: live.waiting_approval,
    last_action: live.last_action, elapsed: live.status === 'running' ? tk.elapsed : live.elapsed,
    waiting_kind: live.waiting_kind !== undefined ? live.waiting_kind : tk.waiting_kind,
    can_continue: live.can_continue ?? tk.can_continue, round: live.round ?? tk.round }
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
  const mine = Object.values(tasks).filter(tk => (botId ? tk.bot_id === botId : tk.chat_id === chatId))
  const changed = mine.map(tk => `${tk.id}:${tk.status}`).join(',')   // 有任务开始或结束：重新拉列表
  const now = useNow(mine.some(tk => tk.status === 'running'))
  useEffect(() => {
    api<TaskInfo[]>(`/api/tasks?${q}&limit=30`).then(r => { setList(r); setMore(r.length === 30) }).catch(e => setErr(e.message))
  }, [q, changed])
  const older = async () => {
    const r = await api<TaskInfo[]>(`/api/tasks?${q}&before=${list!.at(-1)!.id}&limit=30`)
    setList([...list!, ...r]); setMore(r.length === 30)
  }

  if (err) return <div className="panel-body"><div className="error-line">{err}</div></div>
  if (!list) return <div className="panel-body"><div className="muted small">{t('加载中…')}</div></div>
  let lastDay = ''
  return (
    <div className="panel-body">
      <p className="muted small">{t('后台任务每一步做了什么都记在这里。正在做的实时更新，做完的也能点开回看。')}</p>
      {!list.length && <div className="muted small">{t('还没有派过任务')}</div>}
      {list.map(raw => {
        const tk = withLive(raw, tasks[raw.id])
        const d = day(tk.ts), head = d !== lastDay
        lastDay = d
        const owner = chatId ? bots.find(b => b.id === tk.bot_id) : undefined
        const running = tk.status === 'running'
        return (
          <Fragment key={tk.id}>
            {head && <h4>{d}</h4>}
            <button className={`task-row ${tk.status}${taskWaiting(tk) ? ' waiting' : ''}`} onClick={() => showTask(tk.id)}>
              <span className="dot" />
              <span className="task-row-main">
                <span className="task-row-goal">{owner && <BotAvatar avatar={owner.avatar} size={16} />}<span>{firstLine(tk.goal)}</span></span>
                <span className="task-row-meta">
                  {[hm(tk.ts), taskLabel(tk), (tk.round || 1) > 1 ? t('第 {n} 轮', { n: tk.round! }) : '', duration(tk, now), tk.steps ? t('{n} 步', { n: tk.steps }) : '', tk.mode === 'heavy' ? 'Heavy' : ''].filter(Boolean).join(' · ')}
                </span>
                {running && tk.last_action && <span className="task-action">→ {tk.last_action}</span>}
              </span>
            </button>
          </Fragment>
        )
      })}
      {more && <button className="btn small-btn" onClick={older}>{t('更早的任务')}</button>}
    </div>
  )
}

function TaskDetail({ id, group }: { id: number; group: boolean }) {
  const steps = useStore(s => s.taskSteps[id])
  const live = useStore(s => s.tasks[id])
  const approvals = useStore(s => s.approvals)
  const [info, setInfo] = useState<TaskInfo | null>(null)
  const [only, setOnly] = useState('')   // Heavy：只看哪个研究员
  const owner = useStore(s => s.bots.find(b => b.id === (info?.bot_id ?? live?.bot_id)))
  const [err, setErr] = useState('')
  const [retrying, setRetrying] = useState(false)
  useEffect(() => {
    loadTask(id).then(x => x && setInfo(x)).catch(e => setErr(e.message))
    return () => dropTask(id)
  }, [id])
  // 刚做完：再拉一次详情，拿到用时和花费
  useEffect(() => {
    if (info?.status === 'running' && live && live.status !== 'running') fetchTask(id).then(setInfo).catch(() => {})
  }, [live?.status, info?.status])

  const tk = info ? withLive(info, live) : null
  const running = tk?.status === 'running'
  const now = useNow(running)

  // 正在跑的从最新一步看起，跟着往下滚；做完的从头看
  const box = useRef<HTMLDivElement>(null)
  const stick = useRef<boolean | null>(null)
  const loaded = !!(tk && steps)
  // 任务问你的问题、重启后说不准做没做成的那一步：卡片放在过程下面，在这里也能答（刚答完的留一会儿，看得到选了什么）
  const asks = Object.values(approvals).filter(a => a.task_id === id && (a.kind === 'question' || a.doubt) && (a.status === 'pending' || Date.now() / 1000 - a.ts < 600))
  useLayoutEffect(() => {
    const el = box.current
    if (!el || !loaded) return
    if (stick.current === null) stick.current = running
    if (stick.current) el.scrollTop = el.scrollHeight
  }, [steps, loaded, asks.length])
  const onScroll = () => {
    const el = box.current
    if (el && stick.current !== null) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }
  // 发了话、点了重试：任务又跑起来了，跟着最新一步往下看；接口返回的状态先盖上（做完时再拉一次详情）
  const resumed = (x: TaskInfo) => {
    setInfo(i => (i ? { ...i, ...x } : x))
    stick.current = true
    const el = box.current
    if (el) el.scrollTop = el.scrollHeight
  }
  const retry = async () => {
    setRetrying(true)
    try { resumed(await retryTask(id)) } catch (e: any) { notice(e.message) } finally { setRetrying(false) }
  }

  const back = <button className="link-btn" onClick={() => showTask(null)}>← {t('所有任务')}</button>
  if (err) return <div className="panel-body">{back}<div className="error-line">{err}</div></div>
  if (!tk || !steps) return <div className="panel-body">{back}<div className="muted small">{t('加载中…')}</div></div>
  const heavy = tk.mode === 'heavy'
  const openTool = [...steps].reverse().find(s => s.kind === 'tool' && s.status === 'running')
  const busy = steps.some(s => s.status === 'running' || s.status === 'waiting')
  const waiting = waitLabel(tk)
  // Heavy：几个研究员的步骤交错在一起，可以只看其中一个
  const workers = heavy ? [...new Set(steps.map(s => s.worker).filter(Boolean))].sort() : []
  const shown = only ? steps.filter(s => !s.worker || s.worker === only) : steps
  const round = tk.round || 1
  return (
    <div className="td-wrap">
      <div className="panel-body task-detail" ref={box} onScroll={onScroll}>
        <div className="td-nav">{back}
          <span className="td-btns">
            {running && <button className="btn small" onClick={() => api(`/api/tasks/${id}/cancel`, { method: 'POST' }).catch((e: any) => notice(e.message))}>{t('停止')}</button>}
            {canRetry(tk.status) && <button className="btn small" disabled={retrying} onClick={retry}
              title={tk.can_continue ? t('从停下的地方接着做') : t('从头再做一遍')}>{t('重试')}</button>}
          </span></div>
        <div className={`td-head ${tk.status}${waiting ? ' waiting' : ''}`}>
          <span className="dot" /><b>{heavy ? t('Heavy 任务') : t('后台任务')} #{tk.id}</b>
          {round > 1 && <span className="td-round">{t('第 {n} 轮', { n: round })}</span>}
          <span className="muted">{taskLabel(tk)}</span>
        </div>
        <div className="td-meta">
          {[group && owner?.name, t('{day} {time}', { day: day(tk.ts), time: hm(tk.ts) }), duration(tk, now), tk.steps ? t('{n} 步', { n: tk.steps }) : '',
            tk.cost ? t('花费 {cost}', { cost: (tk.currency || '') + tk.cost.toFixed(tk.cost < 1 ? 3 : 2) }) : ''].filter(Boolean).join(' · ')}
        </div>
        <Goal text={tk.goal} />
        {!steps.length && !running ? <>
          <div className="muted small">{t('这个任务是在更新之前做的，没有记下过程。')}</div>
          {tk.result && <div className="step-result"><Markdown text={tk.result} /></div>}
        </> : (
          <div className="steps">
            {workers.length > 1 && <div className="step-filter">
              {['', ...workers].map(w => <button key={w} className={only === w ? 'on' : ''} onClick={() => setOnly(w)}>{w ? worker(w) : t('全部')}</button>)}
            </div>}
            {shown.map(s => <Step key={s.id} s={s} waiting={s === openTool ? waiting : ''} />)}
            {running && !busy && !waiting && <div className="step-wait"><span className="spin" />{t('正在想下一步…')}</div>}
          </div>
        )}
        {asks.map(a => a.doubt ? <DoubtCard key={a.id} a={a} inTask /> : <QuestionCard key={a.id} a={a} inTask />)}
      </div>
      <TaskInput tk={tk} asking={tk.waiting_kind === 'question'} onSent={resumed} />
    </div>
  )
}

// 任务详情底下的输入框：给任务发一句话。正在做的在下一步之前交给它；在等你回答的就是回答；
// 做完、失败、取消、中断的带着这句话接着做。没存过程的任务（Heavy、更新之前做的）做完以后不能接着说
function TaskInput({ tk, asking, onSent }: { tk: TaskInfo; asking: boolean; onSent: (tk: TaskInfo) => void }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const focus = useStore(s => s.taskFocus === tk.id)   // 结果卡片上点了“接着说”
  const ended = taskEnded(tk.status)
  const off = ended && !tk.can_continue
  useEffect(() => {
    if (!focus) return
    taskFocused()
    if (!off) ref.current?.focus()
  }, [focus, off])
  // 跟着内容长高，最多 6 行左右
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 140) + 'px'
  }, [text])
  if (off) return (
    <div className="td-input off">
      {tk.mode === 'heavy' ? t('Heavy 任务没有存下能接着做的过程，不能接着说。有新的要求，在对话里再交代一次。')
        : t('这个任务没有存下能接着做的过程（更新之前做的任务都没有），不能接着说。有新的要求，在对话里再交代一次。')}
    </div>
  )
  const submit = async () => {
    const s = text.trim()
    if (!s || sending) return
    setSending(true); setErr('')
    try { onSent(await tellTask(tk.id, s)); setText('') } catch (e: any) { setErr(e.message) } finally { setSending(false) }
  }
  return (
    <div className="td-input">
      <div className="composer">
        <textarea ref={ref} rows={1} value={text} maxLength={8000} onChange={e => setText(e.target.value)}
          placeholder={asking ? t('直接回答它的问题') : t('给这个任务发一句话')} aria-label={t('给这个任务发一句话')}
          onKeyDown={e => { if (isEnter(e) && !e.shiftKey) { e.preventDefault(); submit() } }} />
        <button className={'send' + (text.trim() && !sending ? ' on' : '')} onClick={submit} disabled={sending} title={t('发送（Enter）')} aria-label={t('发送')}>↑</button>
      </div>
      <div className={'composer-hint' + (err ? ' bad' : '')}>{err || (asking ? t('这句话就是给它的回答') : ended ? t('它会带着这句话接着做，做完再出一条结果')
        : t('在它下一步之前交给它'))}</div>
    </div>
  )
}

function Goal({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 160 || text.split('\n').length > 3
  return (
    <div className="td-goal">
      <div className={long && !open ? 'td-goal-text clamp3' : 'td-goal-text'}>{text}</div>
      {long && <button className="expand" onClick={() => setOpen(!open)}>{open ? t('收起') : t('展开全文')}</button>}
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

function Step({ s, waiting }: { s: TaskStep; waiting: string }) {
  const live = s.status === 'running' || s.status === 'waiting'
  const draft = s.kind === 'answer' && /^h\d+$/.test(s.worker)
  const [toggled, setOpen] = useState<boolean | null>(null)
  const open = toggled ?? (s.kind === 'answer' && !draft)   // 结果默认展开，草稿、思考、工具默认收起
  const now = useNow(live && s.kind === 'tool')   // 工具在跑：每秒更新用时
  const who = worker(s.worker)
  const tag = who ? <span className="step-who">{who}</span> : null
  if (s.kind === 'note') return <div className={`step-note s-${s.status}`}>{tag}{s.title}</div>
  if (s.kind === 'say') return <div className="step-say">{tag}{s.body}</div>
  if (s.kind === 'user') return (   // 你发给任务的话：和 Bot 的步骤分开，像对话里你的消息那样靠右
    <div className="step-user"><span className="step-user-who">{t('你说')} · {hm(s.ts)}</span><div className="step-user-text">{s.body}</div></div>
  )
  if (s.kind === 'text') return (   // 正在写的文字：边收边显示
    <div className="step-text">{tag}<Markdown text={s.body + (live ? ' ▍' : '')} /></div>
  )
  const title = s.kind === 'think' ? <>{live ? t('正在思考') : t('思考')}<span className="step-peek">{firstLine(s.body).slice(0, 80)}</span></>
    : s.kind === 'answer' ? (draft ? t('草稿') : t('写出结果')) : s.title
  const dur = s.kind === 'answer' ? '' : live ? (s.kind === 'tool' ? secs(now - s.ts) : '') : s.dur >= 1 ? secs(s.dur) : ''
  const args = s.kind === 'tool' ? pretty(s.detail) : ''
  return (
    <div className={`step k-${s.kind} s-${s.status}${open ? ' open' : ''}`}>
      <button className="step-head" onClick={() => setOpen(!open)}>
        <span className="step-icon">{live ? <span className="spin" /> : ICON[s.status] || '✓'}</span>
        <span className={'step-title' + (live && s.kind === 'think' ? ' pulse' : '')}>{title}</span>
        {tag}
        {(waiting || s.status === 'waiting') && <span className="step-state">{waiting || t('等你确认')}</span>}
        {s.status === 'rejected' && <span className="step-state bad">{t('被拒绝')}</span>}
        <span className="step-dur">{dur}</span>
        <span className="chev">›</span>
      </button>
      {open && (s.kind === 'think' ? <div className="step-body step-think">{s.body || '…'}</div>
        : s.kind === 'answer' ? <div className="step-body"><Markdown text={s.body} /></div>
        : <div className="step-body">
            {args && <><div className="step-label">{t('参数')}</div><pre className="step-pre">{args}</pre></>}
            <div className="step-label">{tc('step', '返回')}</div>
            {s.body ? <pre className="step-pre">{s.body}</pre> : <div className="muted small">{live ? t('还在做…') : t('（没有返回内容）')}</div>}
          </div>)}
    </div>
  )
}
