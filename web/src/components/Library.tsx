import { useEffect, useRef, useState } from 'react'
import { api, openLibrary, useStore } from '../store'
import type { SearchHit, Avatar, FileEntry, FileListing, Page } from '../types'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'

const when = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
export const size = (n: number) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`
const shareUrl = (token: string) => `${location.origin}/s/${token}`

// 页面正文：markdown 直接渲染；html 放进沙箱 iframe，地址带 CSP，碰不到这个站点的数据
export function PageBody({ format, content, rawUrl }: { format: string; content: string; rawUrl: string }) {
  if (format === 'html') return <iframe className="page-frame" src={rawUrl} sandbox="allow-scripts allow-popups" title="页面" />
  return <div className="page-md"><Markdown text={content} /></div>
}

export default function Library() {
  const pageId = useStore(s => s.libraryPage)
  const [tab, setTab] = useState(() => localStorage.getItem('osa.libtab') || 'pages')
  useEffect(() => localStorage.setItem('osa.libtab', tab), [tab])
  if (pageId) return <main className="chat library"><PageView id={pageId} /></main>
  return (
    <main className="chat library">
      <header className="chat-head">
        <span className="head-name">资料库</span>
        <div className="panel-tabs">
          <button className={tab === 'pages' ? 'on' : ''} onClick={() => setTab('pages')}>页面</button>
          <button className={tab === 'files' ? 'on' : ''} onClick={() => setTab('files')}>文件</button>
        </div>
      </header>
      <div className="scroller"><div className="lib-body">{tab === 'pages' ? <Pages /> : <Files />}</div></div>
    </main>
  )
}

function BotTag({ id }: { id: number | null }) {
  const bot = useStore(s => s.bots.find(b => b.id === id))
  if (!bot) return <span className="muted small">已删除的 Bot</span>
  return <span className="bot-tag"><BotAvatar avatar={bot.avatar} size={16} />{bot.name}</span>
}

// ---------- 页面 ----------
// 按意思找：记忆面板和资料库共用
export function useSearch(kind: 'page' | 'memory', botId?: number) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  useEffect(() => {
    if (!q.trim()) { setHits(null); return }
    const t = setTimeout(() => api(`/api/search?kind=${kind}&q=${encodeURIComponent(q)}${botId ? `&bot_id=${botId}` : ''}`)
      .then(r => setHits(r.hits)).catch(() => setHits([])), 250)
    return () => clearTimeout(t)
  }, [q, kind, botId])
  return { q, setQ, hits }
}

function Pages() {
  const [list, setList] = useState<Page[] | null>(null)
  const { q, setQ, hits } = useSearch('page')
  const load = () => api('/api/pages').then(setList)
  useEffect(() => {
    load()
    window.addEventListener('osa-pages', load)
    return () => window.removeEventListener('osa-pages', load)
  }, [])
  if (!list) return <div className="muted">加载中…</div>
  if (!list.length) return (
    <div className="lib-empty">
      <p>还没有页面。</p>
      <p className="muted small">Bot 做好的报告、清单、网页会存在这里。在对话里说“把结果做成页面”，或者在任务结果下面点“存为页面”。</p>
    </div>
  )
  return (
    <div className="lib-list">
      <input className="lib-search" value={q} placeholder="按意思找页面，比如“上次做的竞品对比”" onChange={e => setQ(e.target.value)} />
      {hits && (hits.length ? hits.map(h => (
        <button key={h.ref} className="lib-row" onClick={() => openLibrary(h.ref)}>
          <span className="lib-icon">▤</span>
          <span className="lib-main"><b>{h.title}</b><span className="lib-meta">{h.snippet}</span></span>
        </button>
      )) : <div className="muted small">没找到相关的页面</div>)}
      {!hits && list.map(p => (
        <button key={p.id} className="lib-row" onClick={() => openLibrary(p.id)}>
          <span className="lib-icon">{p.format === 'html' ? '◧' : '▤'}</span>
          <span className="lib-main">
            <b>{p.pinned && <span className="pin-mark">📌 </span>}{p.title}</b>
            <span className="lib-meta"><BotTag id={p.bot_id} /> · {when(p.updated)}{p.share_token && <span className="tag">已分享</span>}</span>
          </span>
        </button>
      ))}
    </div>
  )
}

function PageView({ id }: { id: number }) {
  const [p, setP] = useState<Page | null>(null)
  const [err, setErr] = useState('')
  const [rename, setRename] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const load = () => api(`/api/pages/${id}`).then(setP).catch((e: any) => setErr(e.message))
  useEffect(() => {
    load()
    window.addEventListener('osa-pages', load)
    return () => window.removeEventListener('osa-pages', load)
  }, [id])
  const patch = async (body: any) => { setP(await api(`/api/pages/${id}`, { method: 'PATCH', body }).then(x => ({ ...p!, ...x }))) }
  const share = async (on: boolean) => {
    const x = await api(`/api/pages/${id}/share`, { method: 'POST', body: { on } })
    setP({ ...p!, ...x })
    if (on) copy(x.share_token)
  }
  const copy = (token: string) => { navigator.clipboard?.writeText(shareUrl(token)); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  const del = async () => {
    if (!confirm(`删除页面「${p!.title}」？分享链接也会失效。`)) return
    await api(`/api/pages/${id}`, { method: 'DELETE' }); openLibrary()
  }
  return (
    <>
      <header className="chat-head page-head">
        <button className="ghost-btn back" onClick={() => openLibrary()}>← 资料库</button>
        {p && (rename !== null
          ? <input className="page-title-input" autoFocus value={rename} onChange={e => setRename(e.target.value)}
              onBlur={() => { patch({ title: rename }); setRename(null) }}
              onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') setRename(null) }} />
          : <span className="head-name page-title" title="点击改名" onClick={() => setRename(p.title)}>{p.title}</span>)}
        <span className="grow" />
        {p && <div className="page-actions">
          <button className="btn" onClick={() => patch({ pinned: !p.pinned })}>{p.pinned ? '取消置顶' : '置顶'}</button>
          {p.share_token
            ? <><button className="btn" onClick={() => copy(p.share_token!)}>{copied ? '已复制链接' : '复制链接'}</button>
                <button className="btn" onClick={() => share(false)}>取消分享</button></>
            : <button className="btn" onClick={() => share(true)}>分享</button>}
          {p.format === 'html' && <a className="btn" href={`/api/pages/${id}/raw`} target="_blank" rel="noreferrer">新窗口</a>}
          <button className="btn danger" onClick={del}>删除</button>
        </div>}
      </header>
      {err ? <div className="lib-body"><div className="error-line">{err}</div></div> : !p ? <div className="lib-body muted">加载中…</div> : (
        <div className={'scroller' + (p.format === 'html' ? ' frame-wrap' : '')}>
          {p.format !== 'html' && <div className="page-info muted small"><BotTag id={p.bot_id} /> · 更新于 {when(p.updated)}{p.task_id ? ` · 来自任务 #${p.task_id}` : ''}
            {p.share_token && <> · 任何拿到链接的人都能看</>}</div>}
          <PageBody format={p.format} content={p.content || ''} rawUrl={`/api/pages/${id}/raw`} />
        </div>
      )}
    </>
  )
}

// 分享链接打开的页面：只读，不加载应用的其他部分
export function SharedPage({ token }: { token: string }) {
  const [p, setP] = useState<{ title: string; format: string; content: string; updated: number; bot?: { name: string; avatar: Avatar } } | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api(`/api/shared/${token}`).then(x => { setP(x); document.title = x.title }).catch((e: any) => setErr(e.message)) }, [token])
  if (err) return <div className="shared"><div className="shared-empty">{err}</div></div>
  if (!p) return <div className="loading">◆</div>
  return (
    <div className={'shared' + (p.format === 'html' ? ' html' : '')}>
      <header className="shared-head">
        {p.bot && <BotAvatar avatar={p.bot.avatar} size={22} />}
        <b>{p.title}</b>
        <span className="muted small">{p.bot ? `${p.bot.name} · ` : ''}{when(p.updated)}</span>
      </header>
      <PageBody format={p.format} content={p.content} rawUrl={`/api/shared/${token}/raw`} />
    </div>
  )
}

// ---------- 文件 ----------
function Files() {
  const [path, setPath] = useState('')
  const [data, setData] = useState<FileListing | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const load = async (p = path, start = false) => {
    setErr('')
    try { const d: FileListing = await api(`/api/files?path=${encodeURIComponent(p)}${start ? '&start=1' : ''}`); setData(d); setPath(d.path) }
    catch (e: any) { setErr(e.message) }
  }
  useEffect(() => { load() }, [])

  if (err && !data) return <div className="error-line">{err}</div>
  if (!data) return <div className="muted">加载中…</div>
  if (!data.running) return (
    <div className="lib-empty">
      <p>电脑没开{data.state === 'paused' ? '（已暂停）' : data.state === 'none' ? '（还没创建）' : ''}。</p>
      <p className="muted small">文件在所有 Bot 共用的电脑里（{data.root}），Bot 下载、生成的文件都在这里。</p>
      <button className="btn primary" disabled={!!busy} onClick={async () => { setBusy('开机中…'); await load(path, true); setBusy('') }}>{busy || '开机查看'}</button>
      {err && <div className="error-line">{err}</div>}
    </div>
  )

  const rel = data.path.slice(data.root.length).split('/').filter(Boolean)
  const upload = async (files: FileList | null) => {
    if (!files) return
    for (const f of Array.from(files)) {
      setBusy(`上传 ${f.name}…`)
      const r = await fetch(`/api/files/content?dir=${encodeURIComponent(data.path)}&name=${encodeURIComponent(f.name)}`, { method: 'PUT', body: f })
      if (!r.ok) setErr((await r.json().catch(() => ({}))).detail || `上传失败（${r.status}）`)
    }
    setBusy(''); load()
  }
  const del = async (e: FileEntry) => {
    if (!confirm(`删除${e.dir ? '文件夹' : '文件'}「${e.name}」？${e.dir ? '里面的东西会一起删掉。' : ''}`)) return
    try { await api(`/api/files?path=${encodeURIComponent(e.path)}`, { method: 'DELETE' }); load() } catch (x: any) { setErr(x.message) }
  }
  const pin = async (e: FileEntry, on: boolean) => { await api('/api/files/pin', { method: 'POST', body: { path: e.path, on } }); load() }
  const pinned = new Set(data.pins.map(x => x.path))

  return (
    <div>
      <div className="file-bar">
        <div className="crumbs">
          <button onClick={() => load(data.root)}>工作目录</button>
          {rel.map((seg, i) => <span key={i}> / <button onClick={() => load(data.root + '/' + rel.slice(0, i + 1).join('/'))}>{seg}</button></span>)}
        </div>
        <span className="grow" />
        {busy && <span className="muted small">{busy}</span>}
        <button className="btn" onClick={() => load()}>刷新</button>
        <button className="btn primary" disabled={!!busy} onClick={() => input.current?.click()}>上传</button>
        <input ref={input} type="file" multiple hidden onChange={e => { upload(e.target.files); e.target.value = '' }} />
      </div>
      {err && <div className="error-line">{err}</div>}
      {rel.length === 0 && data.pins.length > 0 && <>
        <div className="mem-group">置顶</div>
        {data.pins.map(e => <FileRow key={'p' + e.path} e={e} pinned onOpen={load} onPin={pin} onDelete={del} showPath />)}
        <div className="mem-group">全部</div>
      </>}
      {data.entries.length ? data.entries.map(e => <FileRow key={e.path} e={e} pinned={pinned.has(e.path)} onOpen={load} onPin={pin} onDelete={del} />)
        : <div className="lib-empty muted small">这个目录是空的。可以上传文件，Bot 在电脑里就能用。</div>}
    </div>
  )
}

function FileRow({ e, pinned, onOpen, onPin, onDelete, showPath }: {
  e: FileEntry; pinned: boolean; showPath?: boolean
  onOpen: (p: string) => void; onPin: (e: FileEntry, on: boolean) => void; onDelete: (e: FileEntry) => void
}) {
  const href = `/api/files/download?path=${encodeURIComponent(e.path)}`
  return (
    <div className={'file-row' + (e.missing ? ' missing' : '')}>
      <span className="lib-icon">{e.dir ? '📁' : '📄'}</span>
      <span className="lib-main">
        {e.missing ? <span>{e.name}</span>
          : e.dir ? <button className="file-name" onClick={() => onOpen(e.path)}>{e.name}</button>
          : <a className="file-name" href={href} target="_blank" rel="noreferrer">{e.name}</a>}
        <span className="lib-meta">{e.missing ? '已经不在了' : <>{!e.dir && size(e.size) + ' · '}{when(e.mtime)}</>}{showPath && ` · ${e.path}`}</span>
      </span>
      <div className="file-actions">
        <button className="ghost-btn" title={pinned ? '取消置顶' : '置顶'} onClick={() => onPin(e, !pinned)}>{pinned ? '取消置顶' : '置顶'}</button>
        {!e.missing && !e.dir && <a className="ghost-btn" href={href} download={e.name}>下载</a>}
        {!e.missing && <button className="ghost-btn" onClick={() => onDelete(e)}>删除</button>}
      </div>
    </div>
  )
}
