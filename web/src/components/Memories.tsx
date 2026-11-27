import { useEffect, useRef, useState } from 'react'
import { api } from '../store'
import { lang, locale, t, tc } from '../i18n'
import type { Bot, Memory, MemoryList } from '../types'
import { useSearch } from './Library'
import '../memories.css'

type LastRun = { run: number; ts: number; changed: number; undone: boolean } | null
type RunStatus = { running: boolean; done?: number; total?: number; last: LastRun }

// 英文：Nov 6；中文保持 11/06
const day = (ts: number) => new Date(ts * 1000).toLocaleDateString(locale, lang === 'en' ? { month: 'short', day: 'numeric' } : { month: '2-digit', day: '2-digit' })
const time = (ts: number) => new Date(ts * 1000).toLocaleString(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
// 截止日在日期框里的写法（YYYY-MM-DD）：存的是那一天结束的时刻，往前退一秒
const untilDay = (until: number) => {
  const d = new Date((until - 1) * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const expired = (m: Memory) => !m.pinned && m.until != null && m.until * 1000 <= Date.now()
const SOURCE: Record<string, string> = { chat: t('对话里记下'), manual: t('手动添加'), import: tc('source', '导入'), task: t('自动') }
// 让用户发给别的助手的话：英文界面给英文版
const EXPORT_PROMPT = t('请把你记得的关于我的所有信息列出来，每条一行：我的基本情况、偏好、习惯、正在做的事。只列内容，不要解释。')

export function MemoryPanel({ bot }: { bot: Bot }) {
  const [list, setList] = useState<MemoryList | null>(null)
  const [adding, setAdding] = useState(false)
  const [importing, setImporting] = useState(false)
  const [showWork, setShowWork] = useState(false)
  const [showDigest, setShowDigest] = useState(false)
  const [err, setErr] = useState('')
  const { q, setQ, hits } = useSearch('memory', bot.id)
  const load = () => api(`/api/bots/${bot.id}/memories`).then(setList).catch((e: any) => setErr(e.message))
  useEffect(() => {
    load()
    window.addEventListener('osa-memories', load)
    return () => window.removeEventListener('osa-memories', load)
  }, [bot.id])

  if (adding) return <MemoryEditor bot={bot} onDone={() => { setAdding(false); load() }} />
  if (importing) return <ImportMemories bot={bot} onDone={() => { setImporting(false); load() }} />
  return (
    <div>
      <div className="section-head"><h4>{t('记忆')}</h4><button className="btn" onClick={() => setAdding(true)}>＋ {t('添加')}</button></div>
      <p className="muted small">{t('{name} 每次对话都带着这些。在对话里说“记住……”也能添加；点一条可以修改。', { name: bot.name })}</p>
      {err && <div className="error-line">{err}</div>}
      <Consolidate onChange={load} />
      <input className="lib-search" value={q} placeholder={t('按意思找记忆')} onChange={e => setQ(e.target.value)} />
      {hits ? (hits.length ? hits.map(h => <div key={h.ref} className="mem-hit">
        <span className="tag">{h.bot_id === null ? t('共享') : h.sub === 'work' ? t('做过的事') : t('自己的')}</span> {h.snippet}</div>)
        : <div className="muted small">{t('没找到相关的记忆')}</div>)
      : !list ? <div className="muted">{t('加载中…')}</div> : <>
        <div className="mem-group">{t('关于你')} <span className="muted small">{t('所有 Bot 共享')}</span></div>
        {list.shared.length ? list.shared.map(m => <MemoryItem key={m.id} m={m} bot={bot} onChange={load} />)
          : <div className="muted small">{t('还没有。比如你的名字、时区、所在城市。')}</div>}
        <div className="mem-group">{t('{name} 自己记得的', { name: bot.name })}</div>
        {list.own.length ? list.own.map(m => <MemoryItem key={m.id} m={m} bot={bot} onChange={load} />)
          : <div className="muted small">{t('还没有。比如你对它做事方式的要求。')}</div>}
        {list.work.length > 0 && <>
          <button className="mem-group link-like" onClick={() => setShowWork(!showWork)}>
            {t('最近做过的事')} <span className="muted small">{t('自动记录，{n} 条', { n: list.work.length })} {showWork ? '▾' : '▸'}</span>
          </button>
          {showWork && list.work.map(m => <MemoryItem key={m.id} m={m} bot={bot} onChange={load} />)}
        </>}
        {!!list.digest?.length && <>
          <button className="mem-group link-like" onClick={() => setShowDigest(!showDigest)}>
            {t('每日摘要')} <span className="muted small">{t('整理时自动写，所有 Bot 共享，{n} 条', { n: list.digest.length })} {showDigest ? '▾' : '▸'}</span>
          </button>
          {showDigest && list.digest.map(m => <DigestItem key={m.id} m={m} onChange={load} />)}
        </>}
      </>}
      <div className="row-actions"><button className="btn" onClick={() => setImporting(true)}>{t('从其他助手导入')}</button></div>
    </div>
  )
}

/** 立即整理、整理中的进度、整理完改了几条和撤销 */
function Consolidate({ onChange }: { onChange: () => void }) {
  const [st, setSt] = useState<RunStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  const poll = useRef<number | undefined>(undefined)
  const refresh = () => api('/api/memories/consolidate').then(setSt).catch(() => {})
  useEffect(() => {
    refresh()
    window.addEventListener('osa-memories', refresh)
    return () => { window.removeEventListener('osa-memories', refresh); clearInterval(poll.current) }
  }, [])
  const run = async () => {
    setBusy(true); setErr(''); setNote('')
    poll.current = window.setInterval(refresh, 400)
    try {
      const r = await api('/api/memories/consolidate', { method: 'POST' })
      setNote(r.changed ? t('整理完了，改了 {n} 条', { n: r.changed }) : r.digest ? t('整理完了，没有要改的；写了一条每日摘要') : t('整理完了，没有要改的'))
    } catch (e: any) { setErr(e.message) }
    clearInterval(poll.current); setBusy(false); refresh(); onChange()
  }
  const undo = async () => {
    setBusy(true); setErr('')
    try {
      await api('/api/memories/consolidate/undo', { method: 'POST' })
      setNote(t('已撤销，记忆回到整理前的样子'))
    } catch (e: any) { setErr(e.message) }
    setBusy(false); refresh(); onChange()
  }
  const running = busy || !!st?.running
  const last = st?.last
  return (
    <div className="mem-tidy">
      <div className="mem-tidy-row">
        <span className="mem-tidy-text">
          {st?.running && st.total ? t('整理中… {done}/{total}', { done: st.done ?? 0, total: st.total })
            : busy ? t('整理中…')
            : note || (last ? t('上次整理：{time}，改了 {n} 条', { time: time(last.ts), n: last.changed }) : t('每天夜里自动整理：删过期的、合并重复的、写每日摘要'))}
        </span>
        <span className="mem-tidy-actions">
          {!running && last && !last.undone && last.changed > 0 && <button className="btn" onClick={undo}>{t('撤销')}</button>}
          <button className="btn" disabled={running} onClick={run}>{running ? t('整理中…') : t('立即整理')}</button>
        </span>
      </div>
      {st?.running && st.total ? <div className="mem-tidy-bar"><span style={{ width: `${Math.round(100 * (st.done ?? 0) / st.total)}%` }} /></div> : null}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function DigestItem({ m, onChange }: { m: Memory; onChange: () => void }) {
  const del = async () => { await api(`/api/memories/${m.id}`, { method: 'DELETE' }); onChange() }
  return (
    <div className="mem-item static mem-digest" title={day(m.created)}>
      <span className="mem-text">{m.text}</span>
      <button className="ghost-btn" title={t('删除')} onClick={del}>✕</button>
    </div>
  )
}

// 一条记忆：点了就地修改。bot：在某个 Bot 的记忆面板里（能改共享）；不给是项目记忆（看板 → 项目），没有共享这一说
export function MemoryItem({ m, bot, onChange }: { m: Memory; bot?: Bot; onChange: () => void }) {
  const [edit, setEdit] = useState(false)
  const [text, setText] = useState(m.text)
  const [shared, setShared] = useState(m.shared)
  const [until, setUntil] = useState(m.until ? untilDay(m.until) : '')
  const [pinned, setPinned] = useState(!!m.pinned)
  const [err, setErr] = useState('')
  const save = async () => {
    try {
      const body: Record<string, unknown> = bot ? { text, shared, bot_id: bot.id } : { text }
      if (m.kind === 'note') Object.assign(body, { until: until || null, pinned })
      await api(`/api/memories/${m.id}`, { method: 'PATCH', body })
      setEdit(false); onChange()
    } catch (e: any) { setErr(e.message) }
  }
  const pin = async (e: React.MouseEvent) => {
    e.stopPropagation()
    try { await api(`/api/memories/${m.id}`, { method: 'PATCH', body: { pinned: !m.pinned } }); onChange() }
    catch (e: any) { setErr(e.message) }
  }
  const del = async (e?: React.MouseEvent) => {
    e?.stopPropagation()
    await api(`/api/memories/${m.id}`, { method: 'DELETE' }); onChange()
  }
  if (edit) return (
    <div className="mem-item editing">
      <textarea rows={3} value={text} maxLength={500} autoFocus onChange={e => setText(e.target.value)} />
      {m.kind === 'note' && bot && <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> {tc('check', '所有 Bot 共享')}</label>}
      {shared && !m.shared && <div className="muted small">{t('共享后其他 Bot 也会知道这一条。')}</div>}
      {m.kind === 'note' && <div className="mem-until">
        <label>{t('截止日')} <input type="date" value={until} onChange={e => setUntil(e.target.value)} /></label>
        {until ? <button className="link-like small" onClick={() => setUntil('')}>{t('清除')}</button>
          : <span className="muted small">{t('不过期')}</span>}
      </div>}
      {m.kind === 'note' && <label className="check"><input type="checkbox" checked={pinned} onChange={e => setPinned(e.target.checked)} /> {t('置顶（不过期，整理时不动）')}</label>}
      {m.kind === 'note' && until && <div className="muted small">{pinned ? t('置顶的不会过期。') : t('过了这一天就不再带进对话，下次整理时删掉。')}</div>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={() => { setEdit(false); setText(m.text); setShared(m.shared); setUntil(m.until ? untilDay(m.until) : ''); setPinned(!!m.pinned) }}>{t('取消')}</button>
        <button className="btn danger" onClick={() => del()}>{t('删除')}</button>
      </div>
    </div>
  )
  const old = expired(m)
  return (
    <div className={`mem-item${old ? ' expired' : ''}${m.pinned ? ' pinned' : ''}`} onClick={() => setEdit(true)} title={`${SOURCE[m.source] || m.source} · ${day(m.updated)}`}>
      <span className="mem-text">
        {m.text}
        {old ? <span className="tag mem-expired">{t('已过期')}</span>
          : m.until && !m.pinned ? <span className="tag">{t('到 {day}', { day: day(m.until - 1) })}</span> : null}
      </span>
      {m.kind === 'note' && <button className={`ghost-btn mem-pin${m.pinned ? ' on' : ''}`} title={m.pinned ? t('取消置顶') : t('置顶（不过期，整理时不动）')} onClick={pin}>
        <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M9.8 1.5 14.5 6.2l-1.4.6-2.4 2.4-.3 3.1-1.3 1.3-2.5-2.5-3.4 3.4-.7-.7 3.4-3.4L3.4 7.9l1.3-1.3 3.1-.3 2.4-2.4z" fill="currentColor" /></svg>
      </button>}
      <button className="ghost-btn" title={t('删除')} onClick={del}>✕</button>
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function MemoryEditor({ bot, onDone }: { bot: Bot; onDone: () => void }) {
  const [text, setText] = useState('')
  const [shared, setShared] = useState(false)
  const [err, setErr] = useState('')
  const save = async () => {
    try { await api(`/api/bots/${bot.id}/memories`, { method: 'POST', body: { text, shared } }); onDone() }
    catch (e: any) { setErr(e.message) }
  }
  return (
    <div>
      <h4>{t('添加记忆')}</h4>
      <label className="field">{t('内容（一句完整的话）')}
        <textarea rows={4} value={text} maxLength={500} autoFocus placeholder={t('例如：用户希望回答先给结论，再给细节')} onChange={e => setText(e.target.value)} />
      </label>
      <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> {t('所有 Bot 共享（适合名字、时区这类关于你的基本事实）')}</label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions"><button className="btn primary" disabled={!text.trim()} onClick={save}>{t('保存')}</button><button className="btn" onClick={onDone}>{t('返回')}</button></div>
    </div>
  )
}

function ImportMemories({ bot, onDone }: { bot: Bot; onDone: () => void }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<Memory[] | null>(null)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)
  const run = async () => {
    setBusy(true); setErr('')
    try { setDone(await api(`/api/bots/${bot.id}/memories/import`, { method: 'POST', body: { text } })) }
    catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  const shared = done?.filter(m => m.shared).length
  if (done) return (
    <div>
      <h4>{t('导入完成')}</h4>
      <p className="muted small">{shared ? t('记下了 {n} 条，其中 {k} 条所有 Bot 共享。不对的可以回到列表里改或删。', { n: done.length, k: shared })
        : t('记下了 {n} 条。不对的可以回到列表里改或删。', { n: done.length })}</p>
      {done.map(m => <div key={m.id} className="mem-item static"><span className="mem-text">{m.text}</span>{m.shared && <span className="tag">{t('共享')}</span>}</div>)}
      <div className="row-actions"><button className="btn primary" onClick={onDone}>{t('好的')}</button></div>
    </div>
  )
  return (
    <div>
      <h4>{t('从其他助手导入')}</h4>
      <p className="muted small">{t('1. 把下面这句话发给 ChatGPT、Claude 等助手：')}</p>
      <div className="export-prompt">
        <span>{EXPORT_PROMPT}</span>
        <button className="ghost-btn" onClick={() => { navigator.clipboard?.writeText(EXPORT_PROMPT); setCopied(true); setTimeout(() => setCopied(false), 1200) }}>{copied ? t('已复制') : t('复制')}</button>
      </div>
      <p className="muted small">{t('2. 把它的回答粘贴到这里，{name} 会整理成一条条记忆：', { name: bot.name })}</p>
      <textarea className="full" rows={9} value={text} onChange={e => setText(e.target.value)} placeholder={t('粘贴导出的内容')} />
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" disabled={!text.trim() || busy} onClick={run}>{busy ? t('整理中…') : t('导入')}</button>
        <button className="btn" onClick={onDone}>{t('返回')}</button>
      </div>
    </div>
  )
}
