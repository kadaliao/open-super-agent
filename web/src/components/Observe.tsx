import { useEffect, useMemo, useRef, useState } from 'react'
import { api, getState, openChat, showTask, useStore } from '../store'
import type { Routine, RoutineRun, Trigger } from '../types'
import { lang, locale, t, tc } from '../i18n'
import BotAvatar from './BotAvatar'
import { Icon } from '../icons'
import { describe, STATUS, when } from './Routines'
import { ROLE_NAMES } from './ModelSettings'

// 观测：Bot 们花了多少、做了多少、快不快、哪里出了问题。数据见 osa/observe.py
type Range = 'today' | '7d' | '30d' | 'all'
type Metric = 'cost' | 'tokens' | 'tasks' | 'calls'
type Cell = { cost: number; tokens: number; calls: number; tasks: number }
type Point = { t: number; cost: number; tokens: number; calls: number; errors: number; tasks: number; bots: Record<string, Cell> }
type Kpi = {
  cost: number; tokens: number; input: number; output: number; cache_hit: number; cache_rate: number; calls: number; errors: number
  ttft: number | null; speed: number | null
  tasks: number; done: number; failed: number; cancelled: number; running: number; interrupted: number
  success: number | null; avg_s: number | null; routine_runs: number; routine_failed: number; routine_quiet: number
  tool_calls?: number; tool_errors?: number
}
type BotRow = { bot_id: number; cost: number; tokens: number; calls: number; errors: number; ttft: number | null
  tasks: number; done: number; failed: number; avg_s: number | null; success?: number | null; routine_runs: number }
type ModelRow = { provider: string; model: string; cost: number; tokens: number; output: number; cache_rate: number
  calls: number; errors: number; ttft: number | null; speed: number | null }
type ToolRow = { name: string; calls: number; errors: number; rejected: number; avg_s: number | null }
type RoutineRow = { id: number; bot_id: number; name: string; enabled: boolean; trigger: Trigger; describe?: string; category?: string; next_run: number | null; running: boolean
  last_run: RoutineRun | null; runs: number; done: number; failed: number; cancelled: number; interrupted: number; quiet: number; cost: number }
type TaskRow = { id: number; ts: number; goal: string; status: string; elapsed: number | null; steps: number | null; quiet: boolean
  chat_id: number; bot_id: number | null; routine: string | null; cost: number; tokens: number }
type Problem = { kind: 'task' | 'model' | 'tool'; status?: string; ts: number; bot_id: number | null; task_id: number | null
  title: string; detail: string; routine?: string | null; role?: string; count: number }
type Stats = {
  currency: string; range: Range; start: number; end: number; size: number; kpi: Kpi; prev: Kpi | null; series: Point[]
  bots: BotRow[]; models: ModelRow[]; roles: { role: string; cost: number; tokens: number; calls: number; errors: number }[]
  tools: ToolRow[]; routines: RoutineRow[]; tasks: TaskRow[]; problems: Problem[]; hours: number[][]
}
type Call = { id: number; ts: number; bot_id: number | null; task_id: number | null; role: string; provider: string | null; model: string
  input: number; cache_hit: number; output: number; cost: number; ms: number | null; ttft: number | null; err: string | null }

const UNKNOWN = -1               // 没记录是哪个 Bot 的调用（升级前对话里的回复）
const TZ = new Date().getTimezoneOffset()
const COLORS = 7                 // 有自己颜色的 Bot 个数，其余的归“其他”
const RANGES: [Range, string][] = [['today', t('今天')], ['7d', t('7 天')], ['30d', t('30 天')], ['all', tc('range', '全部')]]
const PREV: Record<string, string> = { today: t('比昨天'), '7d': t('比前 7 天'), '30d': t('比前 30 天') }
const WEEK = [tc('weekday', '一'), tc('weekday', '二'), tc('weekday', '三'), tc('weekday', '四'), tc('weekday', '五'), tc('weekday', '六'), tc('weekday', '日')]

// ---------- 数字的写法 ----------
const money = (v: number, cur: string) => cur + (v === 0 ? '0' : v < 1 ? v.toFixed(3) : v < 1000 ? v.toFixed(2) : Math.round(v).toLocaleString(locale))
export function compact(n: number): string {
  if (lang === 'zh') return n < 1e4 ? n.toLocaleString(locale) : n < 1e8 ? `${+(n / 1e4).toFixed(1)}万` : `${+(n / 1e8).toFixed(1)}亿`   // i18n-ignore：中文的数量单位
  return n < 1e4 ? n.toLocaleString(locale) : n < 1e6 ? `${+(n / 1e3).toFixed(1)}K` : n < 1e9 ? `${+(n / 1e6).toFixed(1)}M` : `${+(n / 1e9).toFixed(1)}B`
}
const pct = (r: number) => `${Math.round(r * 100)}%`
export function dur(s: number | null | undefined): string {
  if (s == null) return '—'
  if (s < 60) return t('{n} 秒', { n: s < 10 ? +s.toFixed(1) : Math.round(s) })
  if (s < 3600) return t('{m} 分 {s} 秒', { m: Math.floor(s / 60), s: Math.round(s % 60) })
  return t('{h} 小时 {m} 分', { h: Math.floor(s / 3600), m: Math.round(s % 3600 / 60) })
}
const secs = (ms: number | null | undefined) => ms == null ? '—' : t('{n} 秒', { n: +(ms / 1000).toFixed(ms < 10000 ? 1 : 0) })
const toolName = (n: string) => n.includes('__') ? `@${n.split('__')[0]} · ${n.split('__').slice(1).join('__')}` : n
const roleName = (r: string) => ROLE_NAMES[r] || (r.startsWith('plugin:') ? t('插件 {name}', { name: r.slice(7) }) : r)
const bucketLabel = (ts: number, size: number, long = false) => {
  const d = new Date(ts * 1000)
  if (size === 3600) return long ? d.toLocaleString(locale, { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : `${d.getHours()}:00`
  const day = d.toLocaleDateString(locale, { month: 'numeric', day: 'numeric' })
  if (size > 86400) return long ? t('{day} 那一周', { day }) : day
  return long ? d.toLocaleDateString(locale, { month: 'numeric', day: 'numeric', weekday: 'short' }) : day
}
// 坐标轴的最大值取个整：1、2、2.5、5 乘 10 的几次方
function niceMax(v: number): number {
  if (v <= 0) return 1
  const p = Math.pow(10, Math.floor(Math.log10(v)))
  return ([1, 2, 2.5, 5, 10].find(k => k * p >= v) || 10) * p
}

export default function Observe() {
  const bots = useStore(s => s.bots)
  const [range, setRange] = useState<Range>(() => (localStorage.getItem('osa.obs.range') as Range) || '7d')
  const [bot, setBot] = useState<number | null>(null)
  const [data, setData] = useState<Stats | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { try { localStorage.setItem('osa.obs.range', range) } catch { /* 无痕窗口 */ } }, [range])
  const load = async () => {
    setBusy(true)
    try { setData(await api<Stats>(`/api/observe?range=${range}&tz=${TZ}${bot !== null ? `&bot=${bot}` : ''}`)); setErr('') }
    catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  useEffect(() => { load() }, [range, bot])
  // 开着的时候每 30 秒刷新一次（页面在后台时不刷）
  useEffect(() => {
    const timer = setInterval(() => { if (document.visibilityState === 'visible') load() }, 30000)
    window.addEventListener('osa-routines', load)
    return () => { clearInterval(timer); window.removeEventListener('osa-routines', load) }
  }, [range, bot])

  // 每个 Bot 一个固定的颜色：按建立的先后排，前 7 个各有一个，其余的（和没记录是谁的）归“其他”
  const color = useMemo(() => {
    const ids = [...bots].sort((a, b) => a.id - b.id).map(b => b.id)
    return (id: number | null) => { const i = id == null ? -1 : ids.indexOf(id); return i >= 0 && i < COLORS ? `var(--s${i + 1})` : 'var(--s-other)' }
  }, [bots])
  const name = (id: number | null) => id === UNKNOWN || id == null ? t('没记录是哪个 Bot') : bots.find(b => b.id === id)?.name || t('已删除的 Bot')

  const k = data?.kpi
  const empty = data && !k!.calls && !k!.errors && !k!.tasks
  return (
    <main className="chat observe">
      <header className="chat-head">
        <span className="head-name">{t('观测')}</span>
        <button className="icon-btn ob-refresh" title={t('刷新')} disabled={busy} onClick={load}><Icon name="refresh" size={16} /></button>
        <div className="ob-filters">
          <div className="seg">{RANGES.map(([v, label]) => <button key={v} className={range === v ? 'on' : ''} onClick={() => setRange(v)}>{label}</button>)}</div>
          <select className="ob-bot" value={bot ?? ''} onChange={e => setBot(e.target.value === '' ? null : Number(e.target.value))} aria-label={t('看哪个 Bot')}>
            <option value="">{t('所有 Bot')}</option>
            {bots.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
            {(bot === UNKNOWN || data?.bots.some(r => r.bot_id === UNKNOWN)) && <option value={UNKNOWN}>{t('没记录是哪个 Bot')}</option>}
          </select>
        </div>
      </header>
      <div className="scroller">
        <div className="ob-body">
          {err && <div className="error-line">{err}</div>}
          {!data ? <div className="muted">{t('加载中…')}</div> : <>
            <Tiles s={data} />
            {empty ? <div className="lib-empty">
              <p>{t('这段时间还没有动静。')}</p>
              <p className="muted small">{t('Bot 回复、做任务、跑定时任务时，花了多少、用了多久、哪里出错都会记在这里。')}</p>
            </div> : <>
              <Trend s={data} color={color} name={name} single={bot !== null} />
              {data.problems.length > 0 && <Problems s={data} name={name} />}
              <div className="ob-grid2">
                {bot === null && <BotTable s={data} color={color} name={name} onPick={setBot} />}
                <Heat s={data} />
              </div>
              <Models s={data} />
              {data.routines.length > 0 && <Routines s={data} name={name} onChange={rs => setData({ ...data, routines: rs })} />}
              <div className="ob-grid2">
                {data.tools.length > 0 && <Tools s={data} />}
                <Roles s={data} />
              </div>
              {data.tasks.length > 0 && <Tasks s={data} name={name} />}
            </>}
            <Calls range={range} bot={bot} name={name} currency={data.currency} />
            <p className="muted small ob-foot">{t('费用按各个模型的单价估算（设置 → 费用）。任务的过程只留 90 天，更早的工具调用统计不到。时间按这台设备的时区。')}</p>
          </>}
        </div>
      </div>
    </main>
  )
}

// 打开任务的过程：去这个 Bot 的私聊，右栏“任务”里打开
async function openTask(botId: number | null, taskId: number) {
  const b = getState().bots.find(x => x.id === botId)
  if (b) await openChat(b.chat_id)
  showTask(taskId)
}

// ---------- 顶上的数字 ----------
function Tiles({ s }: { s: Stats }) {
  const k = s.kpi, p = s.prev
  const delta = (now: number, before: number | undefined, upIsBad: boolean) => {
    if (!p || before === undefined || before === 0 || now === before) return null
    const r = (now - before) / before
    const up = r > 0
    return <span className={'ob-delta' + (upIsBad ? (up ? ' bad' : ' good') : '')}>{up ? '↑' : '↓'} {pct(Math.abs(r))} {PREV[s.range]}</span>
  }
  const problems = k.errors + k.failed + k.interrupted + (k.tool_errors || 0)
  return (
    <div className="ob-tiles">
      <div className="ob-tile">
        <span className="ob-label">{t('费用')}</span>
        <b className="ob-value">{money(k.cost, s.currency)}</b>
        {delta(k.cost, p?.cost, true) || <span className="ob-sub">{t('{n} 次模型调用', { n: k.calls })}</span>}
      </div>
      <div className="ob-tile">
        <span className="ob-label">Token</span>
        <b className="ob-value">{compact(k.tokens)}</b>
        <span className="ob-sub">{t('缓存命中 {rate}', { rate: pct(k.cache_rate) })}</span>
      </div>
      <div className="ob-tile">
        <span className="ob-label">{tc('tab', '任务')}</span>
        <b className="ob-value">{k.tasks.toLocaleString(locale)}</b>
        <span className="ob-sub">{k.success != null ? t('成功 {rate}', { rate: pct(k.success) }) : t('还没有做完的')}{k.avg_s != null && ' · ' + t('平均 {time}', { time: dur(k.avg_s) })}</span>
      </div>
      <div className="ob-tile">
        <span className="ob-label">{t('定时任务')}</span>
        <b className="ob-value">{t('{n} 次', { n: k.routine_runs })}</b>
        <span className="ob-sub">{t('失败 {n}', { n: k.routine_failed })} · {t('没有新内容 {n}', { n: k.routine_quiet })}</span>
      </div>
      <div className="ob-tile">
        <span className="ob-label">{t('多久开始回复')}</span>
        <b className="ob-value">{k.ttft != null ? secs(k.ttft) : '—'}</b>
        <span className="ob-sub">{k.speed != null ? t('之后每秒 {n} token', { n: Math.round(k.speed) }) : t('对话里的回复')}</span>
      </div>
      <div className={'ob-tile' + (problems ? ' warn' : '')}>
        <span className="ob-label">{problems ? <Icon name="alert" size={13} /> : <Icon name="check" size={13} />}{t('出错')}</span>
        <b className="ob-value">{problems.toLocaleString(locale)}</b>
        <span className="ob-sub">{t('模型 {a} · 任务 {b} · 工具 {c}', { a: k.errors, b: k.failed + k.interrupted, c: k.tool_errors || 0 })}</span>
      </div>
    </div>
  )
}

// ---------- 趋势：每段时间的费用 / token / 任务 / 调用，按 Bot 叠起来 ----------
const METRICS: [Metric, string][] = [['cost', t('费用')], ['tokens', 'Token'], ['tasks', tc('tab', '任务')], ['calls', t('模型调用')]]

function Trend({ s, color, name, single }: { s: Stats; color: (id: number | null) => string; name: (id: number | null) => string; single: boolean }) {
  const [metric, setMetric] = useState<Metric>(() => (localStorage.getItem('osa.obs.metric') as Metric) || 'cost')
  const [hover, setHover] = useState<number | null>(null)
  const plot = useRef<HTMLDivElement>(null)
  useEffect(() => { try { localStorage.setItem('osa.obs.metric', metric) } catch { /* 无痕窗口 */ } }, [metric])
  const fmt = (v: number) => metric === 'cost' ? money(v, s.currency) : compact(v)
  // 叠的顺序和图例：按颜色分组，“其他”在最上面
  const order = useMemo(() => {
    const seen = new Set<string>()
    s.series.forEach(p => Object.keys(p.bots).forEach(k => seen.add(k)))
    const ids = [...seen].map(Number)
    const own = ids.filter(i => color(i) !== 'var(--s-other)').sort((a, b) => Number(color(a).slice(7, -1)) - Number(color(b).slice(7, -1)))
    return { own, other: ids.filter(i => color(i) === 'var(--s-other)') }
  }, [s, color])
  const parts = (p: Point) => {
    const seg = order.own.map(i => ({ id: i as number | null, v: p.bots[i]?.[metric] || 0, c: color(i) }))
    const rest = order.other.reduce((n, i) => n + (p.bots[i]?.[metric] || 0), 0)
    return [...seg, { id: null, v: rest, c: 'var(--s-other)' }].filter(x => x.v > 0)
  }
  const total = (p: Point) => metric === 'cost' ? p.cost : metric === 'tokens' ? p.tokens : metric === 'tasks' ? p.tasks : p.calls
  const max = niceMax(Math.max(0, ...s.series.map(total)))
  const n = s.series.length
  const step = Math.max(1, Math.ceil(n / (s.size === 3600 ? 6 : 7)))
  // “其他”里只有没记录是谁的调用时，直接这么叫它
  const otherLabel = order.other.every(i => i === UNKNOWN) ? name(UNKNOWN) : t('其他')
  const legend = [...order.own.map(i => ({ id: i, c: color(i), label: name(i) })), ...(order.other.length ? [{ id: -2, c: 'var(--s-other)', label: otherLabel }] : [])]
  const h = hover !== null ? s.series[hover] : null
  const sum = s.series.reduce((a, p) => a + total(p), 0)
  return (
    <section className="ob-card">
      <div className="ob-card-head">
        <h4>{t('趋势')}</h4>
        <span className="muted small">{tc('total', '合计')} {fmt(sum)}</span>
        <span className="grow" />
        <div className="seg small">{METRICS.map(([v, label]) => <button key={v} className={metric === v ? 'on' : ''} onClick={() => setMetric(v)}>{label}</button>)}</div>
      </div>
      {!single && legend.length > 1 && <div className="ob-legend">{legend.map(l => <span key={l.id}><i style={{ background: l.c }} />{l.label}</span>)}</div>}
      <div className="ob-chart" onPointerLeave={() => setHover(null)}>
        <div className="ob-plot" ref={plot}>
          {[0, 0.5, 1].map(f => <div key={f} className="ob-gridline" style={{ bottom: `${f * 100}%` }}><span>{fmt(max * f)}</span></div>)}
          <div className="ob-cols" style={{ gridTemplateColumns: `repeat(${n}, 1fr)` }}>
            {s.series.map((p, i) => {
              const v = total(p)
              return (
                <button key={p.t} className={'ob-col' + (hover === i ? ' on' : '')} onPointerEnter={() => setHover(i)} onFocus={() => setHover(i)} onClick={() => setHover(i)}
                  aria-label={`${bucketLabel(p.t, s.size, true)}：${fmt(v)}`}>
                  <span className="ob-stack" style={{ height: `${(v / max) * 100}%` }}>
                    {parts(p).map(x => <i key={String(x.id)} style={{ flexGrow: x.v, background: x.c }} />)}
                  </span>
                </button>
              )
            })}
          </div>
          {h && <div className={'ob-tip' + (hover! > n / 2 ? ' left' : '')} style={{ left: `${((hover! + 0.5) / n) * 100}%` }}>
            <b>{bucketLabel(h.t, s.size, true)}</b>
            <div className="ob-tip-row total"><span>{tc('total', '合计')}</span><span>{fmt(total(h))}</span></div>
            {!single && Object.entries(h.bots).map(([id, c]) => ({ id: Number(id), v: c[metric] })).filter(x => x.v > 0).sort((a, b) => b.v - a.v).slice(0, 8)
              .map(x => <div key={x.id} className="ob-tip-row"><span><i style={{ background: color(x.id) }} />{name(x.id)}</span><span>{fmt(x.v)}</span></div>)}
            {metric === 'calls' && h.errors > 0 && <div className="ob-tip-row bad"><span>{t('出错')}</span><span>{h.errors}</span></div>}
          </div>}
        </div>
        <div className="ob-xaxis" style={{ gridTemplateColumns: `repeat(${n}, 1fr)` }}>
          {s.series.map((p, i) => <span key={p.t}>{(n - 1 - i) % step === 0 ? bucketLabel(p.t, s.size) : ''}</span>)}
        </div>
      </div>
    </section>
  )
}

// ---------- 需要留意 ----------
function Problems({ s, name }: { s: Stats; name: (id: number | null) => string }) {
  const [all, setAll] = useState(false)
  const list = all ? s.problems : s.problems.slice(0, 5)
  const KIND = { task: tc('problem', '任务'), model: tc('problem', '模型'), tool: tc('problem', '工具') }
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('需要留意')}</h4><span className="muted small">{t('失败的任务、模型和工具出的错')}</span></div>
      <div className="ob-problems">
        {list.map((p, i) => (
          <div key={i} className="ob-problem">
            <span className={'chip ' + (p.kind === 'task' && p.status === 'interrupted' ? 'warn' : 'bad')}><Icon name="alert" size={12} />{KIND[p.kind]}</span>
            <div className="ob-problem-main">
              <b>{p.kind === 'tool' ? toolName(p.title) : p.title}{p.routine && <span className="tag">{t('定时任务 {name}', { name: p.routine })}</span>}
                {p.kind === 'task' && p.status === 'interrupted' && <span className="tag">{t('服务重启时中断')}</span>}
                {p.kind === 'model' && p.role && <span className="tag">{roleName(p.role)}</span>}</b>
              {p.detail && <span className="ob-detail">{p.detail}</span>}
              <span className="lib-meta">{name(p.bot_id)} · {when(p.ts)}{p.count > 1 && ' · ' + t('出现了 {n} 次', { n: p.count })}</span>
            </div>
            {p.task_id != null && <button className="ghost-btn" onClick={() => openTask(p.bot_id, p.task_id!)}>{t('查看过程')}</button>}
          </div>
        ))}
      </div>
      {s.problems.length > 5 && <button className="expand" onClick={() => setAll(!all)}>{all ? t('收起') : t('全部 {n} 条', { n: s.problems.length })}</button>}
    </section>
  )
}

// ---------- 按 Bot ----------
function BotTable({ s, color, name, onPick }: { s: Stats; color: (id: number | null) => string; name: (id: number | null) => string; onPick: (id: number) => void }) {
  const bots = useStore(x => x.bots)
  const max = Math.max(...s.bots.map(b => b.cost), 1e-9)
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('各个 Bot')}</h4><span className="muted small">{t('点一行只看它')}</span></div>
      <div className="ob-scroll">
        <table className="ob-table">
          <thead><tr><th>Bot</th><th>{t('费用')}</th><th>{tc('tab', '任务')}</th><th>{t('成功')}</th><th>{t('平均用时')}</th></tr></thead>
          <tbody>
            {s.bots.map(r => {
              const b = bots.find(x => x.id === r.bot_id)
              return (
                <tr key={r.bot_id} className="click" onClick={() => onPick(r.bot_id)}>
                  <td><span className="ob-name">{b ? <BotAvatar avatar={b.avatar} size={18} /> : <i className="ob-swatch" style={{ background: color(r.bot_id) }} />}{name(r.bot_id)}</span>
                    <span className="ob-bar"><i style={{ width: `${(r.cost / max) * 100}%`, background: color(r.bot_id) }} /></span></td>
                  <td>{money(r.cost, s.currency)}</td>
                  <td>{r.tasks || '—'}</td>
                  <td className={r.success != null && r.success < 0.8 ? 'bad' : ''}>{r.success != null ? pct(r.success) : '—'}</td>
                  <td>{dur(r.avg_s)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// ---------- 什么时候忙：星期 × 小时 ----------
function Heat({ s }: { s: Stats }) {
  const [hover, setHover] = useState<[number, number] | null>(null)
  const max = Math.max(...s.hours.flat(), 0)
  const level = (v: number) => v === 0 ? 0 : Math.min(4, Math.ceil((v / max) * 4))
  const total = s.hours.flat().reduce((a, b) => a + b, 0)
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('什么时候忙')}</h4>
        <span className="muted small">{hover ? t('周{day} {hour}:00 · {n} 次模型调用', { day: WEEK[hover[0]], hour: hover[1], n: s.hours[hover[0]][hover[1]] })
          : t('按星期和钟点，{n} 次模型调用', { n: total })}</span></div>
      <div className="ob-heat" onPointerLeave={() => setHover(null)}>
        {s.hours.map((row, d) => (
          <div key={d} className="ob-heat-row">
            <span className="ob-heat-day">{WEEK[d]}</span>
            {row.map((v, h) => <i key={h} className={'l' + level(v) + (hover && hover[0] === d && hover[1] === h ? ' on' : '')}
              onPointerEnter={() => setHover([d, h])} onClick={() => setHover([d, h])} />)}
          </div>
        ))}
        <div className="ob-heat-row ob-heat-hours"><span className="ob-heat-day" />{Array.from({ length: 24 }, (_, h) => <span key={h}>{h % 6 === 0 ? h : ''}</span>)}</div>
      </div>
      <div className="ob-heat-scale"><span>{t('少')}</span>{[0, 1, 2, 3, 4].map(l => <i key={l} className={'l' + l} />)}<span>{t('多')}</span></div>
    </section>
  )
}

// ---------- 模型 ----------
function Models({ s }: { s: Stats }) {
  if (!s.models.length) return null
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('模型')}</h4><span className="muted small">{t('首字：多久开始出字；速度：开始出字以后每秒多少 token')}</span></div>
      <div className="ob-scroll">
        <table className="ob-table">
          <thead><tr><th>{t('模型')}</th><th>{t('调用')}</th><th>{t('出错')}</th><th>{t('费用')}</th><th>Token</th><th>{t('缓存命中')}</th><th>{t('首字')}</th><th>{t('速度')}</th></tr></thead>
          <tbody>
            {s.models.map(m => (
              <tr key={m.provider + '/' + m.model}>
                <td><span className="ob-name">{m.model}{m.provider && <span className="muted small">{m.provider}</span>}</span></td>
                <td>{m.calls.toLocaleString(locale)}</td>
                <td className={m.errors ? 'bad' : ''}>{m.errors ? `${m.errors}（${pct(m.errors / (m.calls + m.errors))}）` : '—'}</td>
                <td>{money(m.cost, s.currency)}</td>
                <td>{compact(m.tokens)}</td>
                <td>{m.calls ? pct(m.cache_rate) : '—'}</td>
                <td>{secs(m.ttft)}</td>
                <td>{m.speed != null ? Math.round(m.speed) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// ---------- 定时任务：跑了几次、结果怎样，能直接开关 ----------
function Routines({ s, name, onChange }: { s: Stats; name: (id: number | null) => string; onChange: (rs: RoutineRow[]) => void }) {
  const [busy, setBusy] = useState<number | null>(null)
  const [err, setErr] = useState('')
  const toggle = async (r: RoutineRow, on: boolean) => {
    setBusy(r.id); setErr('')
    try {
      const n: Routine = await api(`/api/routines/${r.id}/${on ? 'resume' : 'pause'}`, { method: 'POST' })
      onChange(s.routines.map(x => x.id === r.id ? { ...x, enabled: n.enabled, next_run: n.next_run } : x))
    } catch (e: any) { setErr(e.message) }
    setBusy(null)
  }
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('定时任务和事件')}</h4><span className="muted small">{t('这段时间里跑了几次、结果怎样；开关和右栏“定时”“事件”里的一样')}</span></div>
      {err && <div className="error-line">{err}</div>}
      <div className="ob-scroll">
        <table className="ob-table">
          <thead><tr><th>{t('名字')}</th><th>{t('运行')}</th><th>{t('成功')}</th><th>{t('失败')}</th><th>{t('没有新内容')}</th><th>{t('费用')}</th><th>{t('开关')}</th></tr></thead>
          <tbody>
            {s.routines.map(r => (
              <tr key={r.id} className={r.enabled ? '' : 'off'}>
                <td><span className="ob-name"><b>{r.category === 'event' && <span className="tag ev-tag">{t('事件')}</span>}{r.name}</b><span className="muted small">{name(r.bot_id)} · {describe(r.trigger, r.describe)}</span></span>
                  <span className="muted small">{r.enabled && r.next_run && r.category !== 'event' ? t('下次 {when}', { when: when(r.next_run) }) : !r.enabled ? t('已暂停') : ''}
                    {r.last_run && <>{r.enabled && r.next_run && r.category !== 'event' ? ' · ' : ''}<span className={'run-' + r.last_run.status}>{t('上次 {when} {status}', { when: when(r.last_run.ts), status: STATUS[r.last_run.status] || r.last_run.status })}</span></>}</span></td>
                <td>{r.runs || '—'}</td>
                <td>{r.done - r.quiet || '—'}</td>
                <td className={r.failed + r.interrupted ? 'bad' : ''}>{r.failed + r.interrupted || '—'}</td>
                <td>{r.quiet || '—'}</td>
                <td>{money(r.cost, s.currency)}</td>
                <td><label className="switch" title={r.enabled ? t('暂停') : t('开启')}><input type="checkbox" checked={r.enabled} disabled={busy === r.id}
                  onChange={e => toggle(r, e.target.checked)} /><span /></label></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// ---------- 工具、用途 ----------
function Tools({ s }: { s: Stats }) {
  const [all, setAll] = useState(false)
  const list = all ? s.tools : s.tools.slice(0, 10)
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('工具')}</h4><span className="muted small">{t('任务里调了哪些工具')}</span></div>
      <div className="ob-scroll">
        <table className="ob-table">
          <thead><tr><th>{t('工具')}</th><th>{t('次数')}</th><th>{t('出错')}</th><th>{t('被拒绝')}</th><th>{t('平均用时')}</th></tr></thead>
          <tbody>
            {list.map(x => (
              <tr key={x.name}>
                <td><span className="ob-name mono">{toolName(x.name)}</span></td>
                <td>{x.calls}</td>
                <td className={x.errors ? 'bad' : ''}>{x.errors || '—'}</td>
                <td>{x.rejected || '—'}</td>
                <td>{dur(x.avg_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {s.tools.length > 10 && <button className="expand" onClick={() => setAll(!all)}>{all ? t('收起') : t('全部 {n} 个', { n: s.tools.length })}</button>}
    </section>
  )
}

function Roles({ s }: { s: Stats }) {
  if (!s.roles.length) return null
  const max = Math.max(...s.roles.map(r => r.cost), 1e-9)
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('按用途')}</h4><span className="muted small">{t('钱花在哪类工作上')}</span></div>
      {s.roles.map(r => (
        <div key={r.role} className="ob-role">
          <div className="kv"><span>{roleName(r.role)} · {t('{n} 次模型调用', { n: r.calls })}{r.errors > 0 && <span className="bad"> · {t('出错 {n}', { n: r.errors })}</span>}</span><span>{money(r.cost, s.currency)}</span></div>
          <span className="ob-bar"><i style={{ width: `${(r.cost / max) * 100}%` }} /></span>
        </div>
      ))}
    </section>
  )
}

// ---------- 最近的任务 ----------
const TASK_STATUS: Record<string, string> = { done: t('完成'), failed: t('失败'), cancelled: t('已取消'), running: t('运行中'), interrupted: t('中断') }

function Tasks({ s, name }: { s: Stats; name: (id: number | null) => string }) {
  return (
    <section className="ob-card">
      <div className="ob-card-head"><h4>{t('最近的任务')}</h4><span className="muted small">{t('最多 {n} 个，点开看每一步', { n: 30 })}</span></div>
      <div className="ob-tasks">
        {s.tasks.map(x => (
          <button key={x.id} className="ob-task" onClick={() => openTask(x.bot_id, x.id)}>
            <span className={'ob-dot ' + x.status} title={TASK_STATUS[x.status] || x.status} />
            <span className="ob-task-main">
              <span className="ob-task-goal">{x.goal}</span>
              <span className="lib-meta">{name(x.bot_id)} · {when(x.ts)}{x.routine && <span className="tag">{t('定时任务 {name}', { name: x.routine })}</span>}
                {x.quiet && <span className="tag">{t('没有新内容')}</span>}</span>
            </span>
            <span className="ob-task-nums">
              <span className={x.status === 'failed' || x.status === 'interrupted' ? 'bad' : ''}>{TASK_STATUS[x.status] || x.status}</span>
              <span className="muted">{dur(x.elapsed)} · {money(x.cost, s.currency)}</span>
            </span>
          </button>
        ))}
      </div>
    </section>
  )
}

// ---------- 调用明细：一次次模型调用，能导出 ----------
function Calls({ range, bot, name, currency }: { range: Range; bot: number | null; name: (id: number | null) => string; currency: string }) {
  const [open, setOpen] = useState(false)
  const [failed, setFailed] = useState(false)
  const [list, setList] = useState<Call[] | null>(null)
  const [more, setMore] = useState(false)
  const [total, setTotal] = useState(0)
  const q = `range=${range}&tz=${TZ}${bot !== null ? `&bot=${bot}` : ''}`
  const load = async (before?: number) => {
    const r = await api<{ items: Call[]; more: boolean; total: number }>(`/api/observe/calls?${q}${failed ? '&failed=1' : ''}${before ? `&before=${before}` : ''}`)
    setList(l => before && l ? [...l, ...r.items] : r.items); setMore(r.more); setTotal(r.total)
  }
  useEffect(() => { if (open) load() }, [open, q, failed])
  return (
    <section className="ob-card">
      <div className="ob-card-head">
        <button className="ob-fold" onClick={() => setOpen(!open)} aria-expanded={open}><h4>{t('调用明细')}</h4><Icon name="chevron" size={14} className={open ? 'up' : ''} /></button>
        {open && <span className="muted small">{t('共 {n} 次', { n: total })}</span>}
        <span className="grow" />
        {open && <label className="check small"><input type="checkbox" checked={failed} onChange={e => setFailed(e.target.checked)} /> {t('只看出错的')}</label>}
        <a className="btn small" href={`/api/observe/calls.csv?${q}`} download>{t('导出 CSV')}</a>
      </div>
      {open && (!list ? <div className="muted small">{t('加载中…')}</div> : !list.length ? <div className="muted small">{t('没有记录')}</div> : <>
        <div className="ob-scroll">
          <table className="ob-table ob-calls">
            <thead><tr><th>{t('时间')}</th><th>Bot</th><th>{t('用途')}</th><th>{t('模型')}</th><th>{t('输入')}</th><th>{t('输出')}</th><th>{t('费用')}</th><th>{t('用时')}</th><th>{t('首字')}</th></tr></thead>
            <tbody>
              {list.map(c => (
                <tr key={c.id} className={c.err ? 'err' : ''}>
                  <td>{when(c.ts)}</td>
                  <td>{name(c.bot_id)}{c.task_id ? <span className="muted"> #{c.task_id}</span> : ''}</td>
                  <td>{roleName(c.role)}</td>
                  <td><span className="ob-name">{c.model}{c.err && <span className="bad small">{c.err}</span>}</span></td>
                  <td title={c.cache_hit ? t('其中缓存命中 {n}', { n: c.cache_hit.toLocaleString(locale) }) : ''}>{c.err ? '—' : compact(c.input)}</td>
                  <td>{c.err ? '—' : compact(c.output)}</td>
                  <td>{c.err ? '—' : money(c.cost, currency)}</td>
                  <td>{secs(c.ms)}</td>
                  <td>{secs(c.ttft)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {more && <button className="expand" onClick={() => load(list[list.length - 1].id)}>{t('加载更多')}</button>}
      </>)}
    </section>
  )
}
