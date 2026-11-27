// 需求看板：你要的每件事一张卡，在哪一列由背后的任务决定。四列（待办、进行中、等你处理、已完成），已取消的折在下面。
// 只有两种拖法：待办 → 进行中（开始），任何一列 → 已完成（标记完成）。手机上不拖，卡片页里有同样的按钮。
import { useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent } from 'react'
import { openNeed, useStore } from '../store'
import { locale, t, tc } from '../i18n'
import type { Bot } from '../types'
import { assignee, columnName, COLUMNS, fetchBoard, merge, needAction, oldest, onBoard, refreshWaiting, running, waitReason } from '../board'
import type { Board as BoardData, Column, Filter, Need } from '../board'
import BotAvatar from './BotAvatar'
import { Icon } from '../icons'
import { ask } from './Dialog'
import { NewNeed } from './BoardNeed'
import { Projects } from './BoardProjects'
import '../board.css'

// 侧栏入口的图标（Lucide 的 square-kanban）
export function BoardIcon({ size = 16 }: { size?: number }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect width="18" height="18" x="3" y="3" rx="2" /><path d="M8 7v7" /><path d="M12 7v4" /><path d="M16 7v9" />
    </svg>
  )
}

// ---------- 时间筛选 ----------
type Range = 'all' | 'today' | 'week' | 'month' | 'last' | 'custom'
const day0 = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
const sec = (d: Date) => Math.floor(d.getTime() / 1000)
const parseDay = (s: string) => { const [y, m, d] = s.split('-').map(Number); return y ? new Date(y, m - 1, d) : null }
// 起止：from 是那天 0 点，to 是结束那天的最后一秒（本月、本周到现在为止，不设 to）
function span(r: Range, a: string, b: string): [number | null, number | null] {
  const now = new Date()
  if (r === 'today') return [sec(day0(now)), null]
  if (r === 'week') { const d = day0(now); d.setDate(d.getDate() - (d.getDay() + 6) % 7); return [sec(d), null] }   // 从周一算
  if (r === 'month') return [sec(new Date(now.getFullYear(), now.getMonth(), 1)), null]
  if (r === 'last') return [sec(new Date(now.getFullYear(), now.getMonth() - 1, 1)), sec(new Date(now.getFullYear(), now.getMonth(), 1)) - 1]
  if (r === 'custom') {
    const from = parseDay(a), to = parseDay(b)
    if (to) to.setDate(to.getDate() + 1)
    return [from ? sec(from) : null, to ? sec(to) - 1 : null]
  }
  return [null, null]
}

const load = (k: string) => { try { return localStorage.getItem(k) } catch { return null } }
const save = (k: string, v: string) => { try { localStorage.setItem(k, v) } catch { /* 无痕窗口 */ } }

export default function Board() {
  const bots = useStore(s => s.bots)
  const connected = useStore(s => s.connected)
  const [project, setProject] = useState<number | 'none' | null>(() => {
    const v = load('osa.board.project')
    return v === 'none' ? 'none' : v ? Number(v) : null
  })
  const [range, setRange] = useState<Range>('all')
  const [fromDay, setFromDay] = useState('')
  const [toDay, setToDay] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')   // 停手 300 毫秒后才去搜
  const [archived, setArchived] = useState(false)
  const [data, setData] = useState<BoardData | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [projects, setProjects] = useState(false)
  const [folded, setFolded] = useState(true)                  // 已取消的折起来
  const [tab, setTab] = useState<Column>('todo')              // 手机上看哪一列
  const [dragging, setDragging] = useState<Need | null>(null)
  const [over, setOver] = useState<Column | null>(null)

  useEffect(() => save('osa.board.project', project === null ? '' : String(project)), [project])
  useEffect(() => { const h = setTimeout(() => setQuery(q), 300); return () => clearTimeout(h) }, [q])
  const [from, to] = useMemo(() => span(range, fromDay, toDay), [range, fromDay, toDay])
  const filter: Filter = { project, q: query, from, to, archived }
  const key = JSON.stringify(filter)
  const filtered = project !== null || !!query.trim() || range !== 'all'

  // 重新取：已经“加载更多”过的，一次取回同样多（服务端一页最多 200，多的再按页补）
  const pages = useRef(1)
  const seq = useRef(0)
  const reload = async () => {
    const my = ++seq.current
    setBusy(true)
    try {
      let b = await fetchBoard(filter)
      for (let i = 1; i < pages.current && b.more; i++) b = merge(b, await fetchBoard(filter, oldest(b)))
      if (my !== seq.current) return
      setData(b); setErr('')
      // 选着的项目被删了：回到全部
      if (typeof project === 'number' && !b.projects.some(p => p.id === project)) setProject(null)
    } catch (e: any) { if (my === seq.current) setErr(e.message) }
    if (my === seq.current) setBusy(false)
  }
  const latest = useRef(reload)
  latest.current = reload
  useEffect(() => { pages.current = 1; reload() }, [key])
  useEffect(() => onBoard(() => latest.current()), [])
  useEffect(() => { if (connected && data) latest.current() }, [connected])

  const more = async () => {
    if (!data) return
    setBusy(true)
    try { const b = await fetchBoard(filter, oldest(data)); pages.current++; setData(merge(data, b)) }
    catch (e: any) { setErr(e.message) }
    setBusy(false)
  }

  // 接口返回的卡放到它现在的列里（先在页面上挪过去，不等事件）
  const place = (n: Need) => setData(d => {
    if (!d) return d
    const cols = Object.fromEntries(Object.entries(d.columns).map(([c, list]) => [c, list.filter(x => x.id !== n.id)])) as Record<Column, Need[]>
    if (!!n.archived === archived) cols[n.column] = [n, ...(cols[n.column] || [])]
    return { ...d, columns: cols }
  })
  // 卡片页（盖在看板上）改了卡
  useEffect(() => {
    const h = (e: Event) => place((e as CustomEvent).detail as Need)
    window.addEventListener('osa-need', h)
    return () => window.removeEventListener('osa-need', h)
  }, [archived])
  const act = async (n: Need, a: 'start' | 'done') => {
    if (a === 'done' && running(n) && !await ask(t('“{title}”下面还有任务在做，标记完成会停掉它们。', { title: n.title }), { ok: t('标记完成'), danger: true })) return
    try { place(await needAction(n.id, a)); refreshWaiting() }
    catch (e: any) { setErr(e.message) }
  }

  // 拖放：只认两种
  const canDrop = (n: Need | null, c: Column) => !!n && !archived && ((n.column === 'todo' && c === 'doing') || (c === 'done' && n.column !== 'done'))
  const dragStart = (n: Need) => (e: DragEvent) => {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', String(n.id))
    setDragging(n)
  }
  const dragEnd = () => { setDragging(null); setOver(null) }
  const dragOver = (c: Column) => (e: DragEvent) => {
    if (!canDrop(dragging, c)) return
    e.preventDefault(); e.dataTransfer.dropEffect = 'move'
    if (over !== c) setOver(c)
  }
  const drop = (c: Column) => (e: DragEvent) => {
    e.preventDefault()
    const n = dragging
    dragEnd()
    if (!n || !canDrop(n, c)) return
    act(n, c === 'doing' ? 'start' : 'done')
  }

  const clear = () => { setProject(null); setRange('all'); setFromDay(''); setToDay(''); setQ(''); setQuery('') }
  const cols = data?.columns
  const total = cols ? Object.values(cols).reduce((s, l) => s + l.length, 0) : 0
  const cancelled = cols?.cancelled || []
  const projList = data?.projects || []

  const head = (
    <header className="chat-head bd-head">
      <span className="head-name">{archived ? t('已归档的需求') : t('需求')}</span>
      <div className="bd-head-actions">
        <button className={'btn tiny' + (archived ? ' on' : '')} onClick={() => setArchived(!archived)} title={archived ? t('回到看板') : t('看已归档的卡片')}>
          <Icon name="archive" size={14} /><span className="bd-wide">{archived ? t('回到看板') : t('已归档')}</span>
        </button>
        <button className="btn tiny" onClick={() => setProjects(true)}>{tc('看板', '项目')}</button>
        <button className="btn tiny primary" onClick={() => setCreating(true)}><Icon name="plus" size={14} />{t('新建卡片')}</button>
      </div>
    </header>
  )

  const filters = (
    <div className="bd-filters">
      <div className="bd-projects" role="tablist" aria-label={tc('看板', '项目')}>
        <button className={project === null ? 'on' : ''} onClick={() => setProject(null)}>{t('全部')}</button>
        {projList.map(p => (
          <button key={p.id} className={project === p.id ? 'on' : ''} title={p.brief || p.name} onClick={() => setProject(p.id)}>
            <b>{p.key}</b> {p.name}
          </button>
        ))}
        <button className={project === 'none' ? 'on' : ''} onClick={() => setProject('none')}>{t('不属于项目')}</button>
      </div>
      <div className="bd-filter-row">
        <select className="bd-range" value={range} onChange={e => setRange(e.target.value as Range)} aria-label={t('创建时间')}>
          <option value="all">{t('全部时间')}</option>
          <option value="today">{t('今天')}</option>
          <option value="week">{t('本周')}</option>
          <option value="month">{t('本月')}</option>
          <option value="last">{t('上月')}</option>
          <option value="custom">{t('自定义…')}</option>
        </select>
        {range === 'custom' && <span className="bd-days">
          <input type="date" value={fromDay} max={toDay || undefined} onChange={e => setFromDay(e.target.value)} aria-label={t('开始日期')} />
          <span className="muted">–</span>
          <input type="date" value={toDay} min={fromDay || undefined} onChange={e => setToDay(e.target.value)} aria-label={t('结束日期')} />
        </span>}
        <label className="bd-search">
          <Icon name="search" size={14} />
          <input value={q} placeholder={t('搜编号、标题、描述')} onChange={e => setQ(e.target.value)} />
          {q && <button className="bd-search-x" onClick={() => setQ('')} title={t('清空')}><Icon name="x" size={13} /></button>}
        </label>
        {filtered && <button className="link-btn small" onClick={clear}>{t('清除筛选')}</button>}
        {busy && <span className="muted small bd-busy">{t('加载中…')}</span>}
      </div>
    </div>
  )

  const card = (n: Need, drag: boolean) => (
    <NeedCard key={n.id} n={n} bots={bots} drag={drag && !archived}
      onOpen={() => openNeed(n.id)} onDragStart={dragStart(n)} onDragEnd={dragEnd} dragging={dragging?.id === n.id} />
  )

  let body
  if (!data) body = err ? null : <div className="muted bd-pad">{t('加载中…')}</div>
  else if (!total && !filtered) body = (
    <div className="lib-empty bd-empty">
      {archived ? <p>{t('还没有归档的卡片。')}</p> : <>
        <p>{t('还没有需求。')}</p>
        <p className="muted small">{t('把要做的事记成一张卡交给 Bot，做到哪一步、在等你什么，这里一眼就能看到。')}</p>
        <button className="btn primary" onClick={() => setCreating(true)}>{t('新建卡片')}</button>
      </>}
    </div>
  )
  else if (!total) body = (
    <div className="lib-empty bd-empty">
      <p>{t('没有符合条件的卡片。')}</p>
      <button className="btn" onClick={clear}>{t('清除筛选')}</button>
    </div>
  )
  else body = <>
    <div className="bd-tabs seg">
      {COLUMNS.map(c => <button key={c} className={tab === c ? 'on' : ''} onClick={() => setTab(c)}>
        {columnName(c)}<span className={'bd-count' + (c === 'waiting' && cols![c].length ? ' warn' : '')}>{cols![c].length}</span>
      </button>)}
    </div>
    <div className={'bd-cols' + (dragging ? ' dragging' : '')}>
      {COLUMNS.map(c => {
        const list = cols![c] || []
        const ok = canDrop(dragging, c)
        return (
          <section key={c} className={`bd-col ${c}` + (tab === c ? ' on' : '') + (dragging ? ok ? ' can-drop' : ' no-drop' : '') + (over === c ? ' over' : '')}
            onDragOver={dragOver(c)} onDragLeave={() => over === c && setOver(null)} onDrop={drop(c)} data-col={c}>
            <h4 className="bd-col-head">{columnName(c)}<span className={'bd-count' + (c === 'waiting' && list.length ? ' warn' : '')}>{list.length}</span></h4>
            <div className="bd-list">
              {list.map(n => card(n, n.column !== 'done'))}
              {!list.length && <div className="bd-none">{dragging && ok ? (c === 'doing' ? t('放到这里开始') : t('放到这里标记完成')) : '—'}</div>}
              {dragging && ok && list.length > 0 && <div className="bd-drop-hint">{c === 'doing' ? t('放到这里开始') : t('放到这里标记完成')}</div>}
            </div>
          </section>
        )
      })}
    </div>
    {cancelled.length > 0 && <div className="bd-cancelled">
      <button className="bd-fold" onClick={() => setFolded(!folded)} aria-expanded={!folded}>
        <Icon name="chevron" size={14} className={folded ? 'bd-rot' : ''} />{t('已取消')} <span className="bd-count">{cancelled.length}</span>
      </button>
      {!folded && <div className="bd-cancelled-list">{cancelled.map(n => card(n, false))}</div>}
    </div>}
    {data.more && <div className="bd-more"><button className="btn" disabled={busy} onClick={more}>{t('加载更多')}</button></div>}
  </>

  return (
    <main className="chat board">
      {head}
      {filters}
      <div className="scroller">
        <div className="bd-body">
          {err && <div className="error-line">{err}</div>}
          {body}
        </div>
      </div>
      {creating && <NewNeed projects={projList} project={typeof project === 'number' ? project : null}
        open={cols ? [...cols.todo, ...cols.doing, ...cols.waiting] : []}
        onClose={() => setCreating(false)} onCreated={n => { setCreating(false); place(n); refreshWaiting() }} />}
      {projects && <Projects onClose={() => setProjects(false)} onChanged={() => reload()} />}
    </main>
  )
}

function NeedCard({ n, bots, drag, dragging, onOpen, onDragStart, onDragEnd }: {
  n: Need; bots: Bot[]; drag: boolean; dragging: boolean; onOpen: () => void; onDragStart: (e: DragEvent) => void; onDragEnd: () => void
}) {
  const bot = assignee(bots, n.assignee_bot_id)
  const reason = n.column === 'waiting' ? waitReason(n) : ''
  const p = n.progress
  return (
    <div className={`bd-card ${n.column}` + (dragging ? ' lifted' : '')} role="button" tabIndex={0} draggable={drag}
      onDragStart={onDragStart} onDragEnd={onDragEnd} onClick={onOpen} onKeyDown={e => { if (e.key === 'Enter') onOpen() }} data-need={n.id}>
      {(n.key || reason) && <div className="bd-card-top">
        {n.key && <span className="bd-key">{n.key}</span>}
        {reason && <span className="bd-reason">{reason}</span>}
      </div>}
      <div className="bd-title">{n.title}</div>
      {n.column === 'doing' && (p && p.total > 0 || n.latest) && <div className="bd-prog">
        {p && p.total > 0 && <span className="bd-prog-n">
          <span className="bd-bar"><i style={{ width: `${Math.min(100, p.done / p.total * 100)}%` }} /></span>
          {t('{done}/{total} 项', { done: p.done, total: p.total })}
        </span>}
        {n.latest && <span className="bd-latest">{n.latest}</span>}
      </div>}
      {n.blocked_by.length > 0 && <div className="bd-blocked">
        {n.blocked_by.map(b => <span key={b.id} className="bd-wait-on" title={b.title}>⏳ {t('等 {who}', { who: b.key || b.title })}</span>)}
      </div>}
      <div className="bd-card-foot">
        {bot ? <span className="bd-who" title={t('交给 {name}', { name: bot.name })}><BotAvatar avatar={bot.avatar} size={18} busy={running(n)} /><span>{bot.name}</span></span> : <span />}
        <span className="bd-date">{new Date(n.updated * 1000).toLocaleDateString(locale, { month: 'numeric', day: 'numeric' })}</span>
      </div>
    </div>
  )
}
