import { useEffect, useMemo, useState } from 'react'
import { api, openLibrary, showTask, useStore } from '../store'
import type { Bot } from '../types'
import { lang, locale, t, tc } from '../i18n'
import { Icon } from '../icons'
import { extLabel, fileUrl, fmtSize, mediaUrl, openImage } from './Media'

// 右栏“资料库”：这个 Bot 交给你的页面、文件、图片和链接，按时间倒序、按天分组（接口见 osa/outputs.py）

type Kind = 'page' | 'file' | 'image' | 'link'
type Output = {
  key: string; kind: Kind; ts: number; title: string; count: number
  chat_id: number | null; msg_id: number | null; task_id: number | null
  url?: string; host?: string; path?: string | null; media?: string; mime?: string; size?: number | null; w?: number; h?: number
  page_id?: number; format?: string; shared?: boolean; updated?: number
}

const KINDS: [Kind | 'all', string][] = [['all', t('全部')], ['page', tc('outputs', '页面')], ['file', tc('outputs', '文件')],
  ['image', tc('outputs', '图片')], ['link', tc('outputs', '链接')]]
const PAGE = 150   // 一次显示多少条，多了点“显示更多”

const hm = (ts: number) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: lang === 'en' ? 'numeric' : '2-digit', minute: '2-digit' })
function day(ts: number) {
  const d = new Date(ts * 1000), now = new Date()
  const n = Math.round((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000)
  if (n === 0) return t('今天')
  if (n === 1) return t('昨天')
  return d.toLocaleDateString(locale, { month: 'long', day: 'numeric', weekday: 'short', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) })
}
// 裸链接没有标题：显示 主机名 + 路径（去掉 www. 和结尾的 /）
const linkText = (o: Output) => {
  if (o.title) return o.title
  try {
    const u = new URL(o.url!)
    return (u.host.replace(/^www\./, '') + decodeURI(u.pathname).replace(/\/$/, '') + u.search).slice(0, 140)
  } catch { return o.url! }
}
const title = (o: Output) => o.kind === 'link' ? linkText(o) : o.title || o.path?.split('/').pop() || ''

export function BotLibraryPanel({ bot }: { bot: Bot }) {
  const [items, setItems] = useState<Output[] | null>(null)
  const [err, setErr] = useState('')
  const [kind, setKind] = useState<Kind | 'all'>('all')
  const [q, setQ] = useState('')
  const [shown, setShown] = useState(PAGE)
  // 它所在的对话有新消息（回复、任务结果、画好的图）时重新取一次
  const touched = useStore(s => Math.max(0, ...s.chats.filter(c => c.bot_ids.includes(bot.id)).map(c => c.updated)))
  const load = () => api<Output[]>(`/api/bots/${bot.id}/outputs`).then(x => { setItems(x); setErr('') }).catch((e: any) => setErr(e.message))
  useEffect(() => {
    window.addEventListener('osa-pages', load)
    return () => window.removeEventListener('osa-pages', load)
  }, [bot.id])
  useEffect(() => {
    if (!items) { load(); return }
    const timer = setTimeout(load, 800)   // 一轮回复里接连几条消息，等它们都到了再取
    return () => clearTimeout(timer)
  }, [bot.id, touched])
  useEffect(() => setShown(PAGE), [kind, q])

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items?.length || 0 }
    for (const o of items || []) c[o.kind] = (c[o.kind] || 0) + 1
    return c
  }, [items])
  const list = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean)
    return (items || []).filter(o => (kind === 'all' || o.kind === kind) &&
      words.every(w => [title(o), o.url, o.path, o.host].some(s => s?.toLowerCase().includes(w))))
  }, [items, kind, q])

  if (err && !items) return <div className="error-line">{err}</div>
  if (!items) return <div className="muted">{t('加载中…')}</div>
  if (!items.length) return (
    <div className="lib-empty">
      <p>{t('还没有交给你的东西')}</p>
      <p className="muted small">{t('{name} 做好的页面、发给你的文件和图片、回复里给的链接，会按时间存在这里。', { name: bot.name })}</p>
    </div>
  )

  // 按天分组：[天, 这一天的条目]
  const groups: [string, Output[]][] = []
  for (const o of list.slice(0, shown)) {
    const d = day(o.ts)
    if (groups.length && groups[groups.length - 1][0] === d) groups[groups.length - 1][1].push(o)
    else groups.push([d, [o]])
  }
  return (
    <div className="blib">
      <input className="lib-search" type="search" value={q} placeholder={t('找标题、网址、文件名')} onChange={e => setQ(e.target.value)} />
      <div className="blib-kinds">
        {KINDS.filter(([k]) => k === 'all' || counts[k]).map(([k, label]) => (
          <button key={k} className={kind === k ? 'on' : ''} onClick={() => setKind(k)}>{label}<span className="blib-n">{counts[k] || 0}</span></button>
        ))}
      </div>
      {!list.length && <div className="muted small">{t('没有符合的')}</div>}
      {groups.map(([d, rows]) => (
        <section key={d} className="blib-day">
          <h5>{d}</h5>
          {rows.map(o => <Row key={o.key} o={o} />)}
        </section>
      ))}
      {list.length > shown && <button className="ghost-btn" onClick={() => setShown(shown + PAGE)}>{t('显示更多（还有 {n} 条）', { n: list.length - shown })}</button>}
    </div>
  )
}

function Row({ o }: { o: Output }) {
  const name = title(o)
  const icon = o.kind === 'image' ? <img className="blib-thumb" src={mediaUrl(o.media!)} alt="" loading="lazy" />
    : o.kind === 'file' ? <span className="file-ext blib-ext">{extLabel(name)}</span>
    : o.kind === 'page' ? <span className="blib-icon">{o.format === 'html' ? '◧' : '▤'}</span>
    : <span className="blib-icon"><Icon name="external" size={15} /></span>
  const meta = [hm(o.ts),
    o.kind === 'link' ? o.host?.replace(/^www\./, '') : '',
    (o.kind === 'file' || o.kind === 'image') && o.size ? fmtSize(o.size) : '',
    o.kind === 'file' ? o.path : '',
  ].filter(Boolean).join(' · ')
  const body = <>
    {icon}
    <span className="lib-main">
      <b title={o.kind === 'link' ? o.url : o.path || name}>{name}</b>
      <span className="lib-meta">{meta}{o.shared && <span className="tag">{t('已分享')}</span>}
        {o.count > 1 && <span className="tag" title={t('给过 {n} 次，这里是最近的一次', { n: o.count })}>×{o.count}</span>}</span>
    </span>
  </>
  const open = o.kind === 'page' ? <button className="blib-main" onClick={() => openLibrary(o.page_id!)}>{body}</button>
    : o.kind === 'image' ? <button className="blib-main" onClick={() => openImage({ src: mediaUrl(o.media!), name: name, download: mediaUrl(o.media!) + '?download=1' })}>{body}</button>
    : o.kind === 'file' ? <a className="blib-main" href={fileUrl(o.path!)} target="_blank" rel="noreferrer" title={t('打开（电脑没开会先开机）')}>{body}</a>
    : <a className="blib-main" href={o.url} target="_blank" rel="noreferrer">{body}</a>
  return (
    <div className="blib-row">
      {open}
      {o.task_id ? <button className="link-btn blib-task" title={t('任务 #{id}', { id: o.task_id }) + ' · ' + t('看这个任务每一步做了什么')}
        onClick={() => showTask(o.task_id!)}>#{o.task_id}</button> : null}
    </div>
  )
}
