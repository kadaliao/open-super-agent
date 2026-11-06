import { useEffect, useState } from 'react'
import { api } from '../store'
import { lang, locale, t, tc } from '../i18n'
import type { Bot, Memory, MemoryList } from '../types'
import { useSearch } from './Library'

// 英文：Nov 6；中文保持 11/06
const day = (ts: number) => new Date(ts * 1000).toLocaleDateString(locale, lang === 'en' ? { month: 'short', day: 'numeric' } : { month: '2-digit', day: '2-digit' })
const SOURCE: Record<string, string> = { chat: t('对话里记下'), manual: t('手动添加'), import: tc('source', '导入'), task: t('自动') }
// 让用户发给别的助手的话：英文界面给英文版
const EXPORT_PROMPT = t('请把你记得的关于我的所有信息列出来，每条一行：我的基本情况、偏好、习惯、正在做的事。只列内容，不要解释。')

export function MemoryPanel({ bot }: { bot: Bot }) {
  const [list, setList] = useState<MemoryList | null>(null)
  const [adding, setAdding] = useState(false)
  const [importing, setImporting] = useState(false)
  const [showWork, setShowWork] = useState(false)
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
      </>}
      <div className="row-actions"><button className="btn" onClick={() => setImporting(true)}>{t('从其他助手导入')}</button></div>
    </div>
  )
}

function MemoryItem({ m, bot, onChange }: { m: Memory; bot: Bot; onChange: () => void }) {
  const [edit, setEdit] = useState(false)
  const [text, setText] = useState(m.text)
  const [shared, setShared] = useState(m.shared)
  const [err, setErr] = useState('')
  const save = async () => {
    try {
      await api(`/api/memories/${m.id}`, { method: 'PATCH', body: { text, shared, bot_id: bot.id } })
      setEdit(false); onChange()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async (e?: React.MouseEvent) => {
    e?.stopPropagation()
    await api(`/api/memories/${m.id}`, { method: 'DELETE' }); onChange()
  }
  if (edit) return (
    <div className="mem-item editing">
      <textarea rows={3} value={text} maxLength={500} autoFocus onChange={e => setText(e.target.value)} />
      {m.kind === 'note' && <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> {tc('check', '所有 Bot 共享')}</label>}
      {shared && !m.shared && <div className="muted small">{t('共享后其他 Bot 也会知道这一条。')}</div>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={() => { setEdit(false); setText(m.text); setShared(m.shared) }}>{t('取消')}</button>
        <button className="btn danger" onClick={() => del()}>{t('删除')}</button>
      </div>
    </div>
  )
  return (
    <div className="mem-item" onClick={() => setEdit(true)} title={`${SOURCE[m.source] || m.source} · ${day(m.updated)}`}>
      <span className="mem-text">{m.text}</span>
      <button className="ghost-btn" title={t('删除')} onClick={del}>✕</button>
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
