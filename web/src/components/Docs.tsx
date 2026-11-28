// 资料库里的文档：交给 Open Agent Docs（见 docs/plans/oad.md）。浏览器只连本站：/api/v1/* 由 osa-server 带上用户转过去。
// 这个文件按需加载（编辑器、Yjs 比较大），不进首屏的包。
import { createClient, threadAnchor, type Me, type Page } from '@open-agent-docs/client'
import {
  OadComments, OadDatabaseView, OadEditor, OadProvider, OadTitle, colorFor, focusStart, setCommentThreads, setLocale as setOadLocale,
  useOadPage, usePageComments, usePeople, type AuthorInfo, type CommentAnchor, type Editor, type MediaServices, type Unfurl,
} from '@open-agent-docs/editor'
import '@open-agent-docs/editor/style.css'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { HistoryPanel, VersionView, type Version } from './DocHistory'
import { openLibrary } from '../store'
import { lang, locale, t } from '../i18n'
import { Icon } from '../icons'
import { ask } from './Dialog'

const client = createClient({ baseUrl: '', locale: lang })
setOadLocale(lang)

const when = (ms: number) => new Date(ms).toLocaleString(locale, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
// 桌面 App 在这台电脑上运行时，地址是 127.0.0.1：分享出去的链接别人打不开
const localOnly = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname) || location.protocol === 'tauri:'

let meP: Promise<Me> | null = null
const getMe = () => (meP ??= client.me().catch(e => { meP = null; throw e }))

function useMe() {
  const [me, setMe] = useState<Me | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => { getMe().then(setMe).catch((e: any) => setErr(e.message || String(e))) }, [])
  return { me, err }
}

// 页面按目录排：父页面下面跟着子页面
function ordered(pages: Page[]) {
  const kids = new Map<string | null, Page[]>()
  for (const p of pages) {
    if (p.kind === 'row') continue
    const k = p.parent_id ?? null
    kids.set(k, [...(kids.get(k) || []), p])
  }
  const out: { p: Page; depth: number }[] = []
  const walk = (k: string | null, depth: number) => {
    for (const p of (kids.get(k) || []).sort((a, b) => (a.position < b.position ? -1 : 1))) {
      out.push({ p, depth })
      walk(p.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
}

export function DocList() {
  const { me, err } = useMe()
  const [pages, setPages] = useState<Page[] | null>(null)
  const ws = me?.workspaces[0]?.id
  useEffect(() => {
    if (!ws) return
    const load = () => client.pages(ws).then(setPages).catch(() => setPages([]))
    load()
    const ev = client.events(ws)
    const upsert = (p: Page) => setPages(l => (l ? (l.some(x => x.id === p.id) ? l.map(x => (x.id === p.id ? { ...x, ...p } : x)) : [...l, p]) : l))
    ev.on('page.created', upsert)
    ev.on('page.updated', upsert)
    ev.on('page.deleted', (p: { id: string }) => setPages(l => l && l.filter(x => x.id !== p.id)))
    ev.on('resync', load)
    return () => ev.close()
  }, [ws])
  if (err) return <div className="error-line">{t('文档服务连不上：{error}', { error: err })}</div>
  if (!pages) return <div className="muted">{t('加载中…')}</div>
  const create = async () => {
    const p = await client.createPage(ws!, { title: '' })
    openLibrary(p.id)
  }
  const rows = ordered(pages)
  return (
    <div className="lib-list">
      <p className="lib-intro muted small">{t('文档是你和 Bot 一起写的：直接在里面编辑，选中一段写评论、@ 某个 Bot，它就会来改、来回复。可以分享给别人看。')}</p>
      <div className="doc-new"><button className="btn primary" onClick={create}><Icon name="plus" size={16} className="lead" />{t('新建文档')}</button></div>
      {!rows.length && <div className="lib-empty"><p className="muted small">{t('还没有文档。新建一篇，或者让 Bot 把结果写成文档。')}</p></div>}
      {rows.map(({ p, depth }) => (
        <button key={p.id} className="lib-row" style={{ paddingLeft: 10 + depth * 18 }} onClick={() => openLibrary(p.id)}>
          <span className="lib-icon">{p.icon || <Icon name={p.kind === 'database' ? 'table' : 'file-text'} size={16} />}</span>
          <span className="lib-main">
            <b>{p.title || t('无标题')}</b>
            <span className="lib-meta">{when(p.updated_at)}</span>
          </span>
        </button>
      ))}
    </div>
  )
}

// 编辑器用到的外部服务：上传图片和文件、链接预览
const unfurls = new Map<string, Unfurl>()
function mediaServices(ws: string, pageId: string): Partial<MediaServices> {
  return {
    upload: (file, onProgress) => client.upload(ws, file, { name: file.name, onProgress }) as any,
    unfurl: url => unfurls.has(url) ? Promise.resolve(unfurls.get(url)!) : client.unfurl(url).then(d => { if (d) unfurls.set(url, d as Unfurl); return d as Unfurl | null }, () => null),
    peek: url => unfurls.get(url) ?? null,
    remember: (url, d) => { unfurls.set(url, d) },
    pageUrl: () => `${location.origin}/p/${pageId}`,
  }
}

// 正文里的链接：本工作区的文档（/p/<id>）在资料库里打开，别的交给编辑器（新标签页）
function openLink(href: string) {
  let u: URL
  try { u = new URL(href, location.origin) } catch { return false }
  const id = u.origin === location.origin && u.pathname.startsWith('/p/') ? u.pathname.split('/')[2] : ''
  if (!id) return false
  openLibrary(decodeURIComponent(id))
  return true
}

type Panel = 'comments' | 'history' | null

export function DocView({ id }: { id: string }) {
  const { me, err } = useMe()
  const [page, setPage] = useState<Page | null>(null)
  const [missing, setMissing] = useState('')
  const [panel, setPanel] = useState<Panel>(null)
  const [version, setVersion] = useState<Version | null>(null)
  const [openCount, setOpenCount] = useState(0)
  useEffect(() => {
    setPage(null); setMissing(''); setPanel(null); setVersion(null)
    client.page(id).then(setPage).catch((e: any) => setMissing(e.message || String(e)))
  }, [id])
  const ws = me?.workspaces[0]?.id
  const isDoc = page && page.kind !== 'database'
  const toggle = (p: Exclude<Panel, null>) => { setVersion(null); setPanel(x => (x === p ? null : p)) }
  return (
    <OadProvider client={client}>
      <header className="chat-head page-head">
        <button className="ghost-btn back" onClick={() => openLibrary()}><Icon name="arrow-left" size={14} className="lead" />{t('资料库')}</button>
        <span className="head-name page-title">{page ? page.title || t('无标题') : ''}</span>
        <span className="grow" />
        {page && <div className="page-actions">
          {isDoc && <button className={'btn' + (panel === 'history' ? ' on' : '')} onClick={() => toggle('history')} title={t('看以前的版本、恢复')}>{t('历史')}</button>}
          {isDoc && <button className={'btn' + (panel === 'comments' ? ' on' : '')} onClick={() => toggle('comments')}>
            {t('评论')}{openCount > 0 && <span className="doc-count">{openCount}</span>}
          </button>}
          <DocActions page={page} />
        </div>}
      </header>
      {err || missing ? <div className="lib-body"><div className="error-line">{err || missing}</div></div>
        : !me || !page || !ws ? <div className="lib-body muted">{t('加载中…')}</div>
        : page.kind === 'database' ? <DbBody page={page} me={me} ws={ws} />
        : <DocBody key={page.id} page={page} me={me} ws={ws} panel={panel} setPanel={setPanel} version={version} setVersion={setVersion}
            onOpenCount={setOpenCount} onTitle={title => setPage(p => (p ? { ...p, title } : p))} />}
    </OadProvider>
  )
}

function DocActions({ page }: { page: Page }) {
  const [share, setShare] = useState<{ token: string } | null | undefined>(undefined)
  const [copied, setCopied] = useState(false)
  useEffect(() => { client.getShare(page.id).then(setShare).catch(() => setShare(null)) }, [page.id])
  const url = (tok: string) => `${location.origin}/s/${tok}`
  const copy = (tok: string) => { navigator.clipboard?.writeText(url(tok)); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  const on = async () => {
    if (localOnly && !(await ask(t('现在是在这台电脑上运行，分享链接只有这台电脑能打开。要让别人也能看，用连到服务器的方式。仍然生成链接？'), { ok: t('生成') }))) return
    const s = await client.createShare(page.id, {})
    setShare(s)
    copy(s.token)
  }
  const off = async () => { await client.revokeShare(page.id); setShare(null) }
  const del = async () => {
    if (!(await ask(t('把文档「{title}」移到回收站？分享链接也会失效。', { title: page.title || t('无标题') }), { ok: t('删除'), danger: true }))) return
    await client.deletePage(page.id)
    openLibrary()
  }
  return (
    <>
      {share === undefined ? null : share
        ? <><button className="btn" onClick={() => copy(share.token)}>{copied ? t('已复制链接') : t('复制链接')}</button>
            <button className="btn" onClick={off}>{t('取消分享')}</button></>
        : <button className="btn" onClick={on}>{t('分享')}</button>}
      <button className="btn danger" onClick={del}>{t('删除')}</button>
    </>
  )
}

function DbBody({ page, me, ws }: { page: Page; me: Me; ws: string }) {
  const events = useMemo(() => client.events(ws), [ws])
  useEffect(() => () => events.close(), [events])
  return (
    <div className="scroller oad-host"><div className="doc-db">
      <OadDatabaseView dbId={page.id} me={me} ws={ws} canWrite events={events} embedded navigate={(pid: string) => openLibrary(pid)} />
    </div></div>
  )
}

function DocBody({ page, me, ws, panel, setPanel, version, setVersion, onOpenCount, onTitle }: {
  page: Page; me: Me; ws: string; panel: Panel; setPanel: (p: Panel) => void; version: Version | null; setVersion: (v: Version | null) => void
  onOpenCount: (n: number) => void; onTitle: (t: string) => void
}) {
  const user = useMemo(() => ({ id: me.user.id, name: me.user.name, color: colorFor(me.user.id) }), [me])
  const doc = useOadPage(client, page.id, { user })
  const events = useMemo(() => client.events(ws), [ws])
  useEffect(() => () => events.close(), [events])
  const { threads, reload } = usePageComments(client, page.id, events)
  // 每一段是谁写的（编辑器左边的小头像）：新出现的写作者再去问
  const [authors, setAuthors] = useState<Record<string, AuthorInfo>>({})
  const asked = useRef(new Set<string>())
  const loadAuthors = useCallback((c?: string) => {
    if (c) { if (asked.current.has(c)) return; asked.current.add(c) }
    client.authors(page.id).then(r => setAuthors((r.clients ?? {}) as Record<string, AuthorInfo>), () => {})
  }, [page.id])
  useEffect(() => { if (doc.ready) loadAuthors() }, [doc.ready, loadAuthors])
  const allAuthors = useMemo(() => (doc.ydoc ? { ...authors, [String(doc.ydoc.clientID)]: { kind: 'user', id: me.user.id, name: me.user.name } as AuthorInfo } : authors), [authors, doc.ydoc, me])
  const people = usePeople(client, ws, me, authors, threads)
  const media = useMemo(() => mediaServices(ws, page.id), [ws, page.id])
  const [draft, setDraft] = useState<CommentAnchor | null>(null)
  const [active, setActive] = useState<string | null>(null)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [histKey, setHistKey] = useState(0)
  const [toast, setToast] = useState('')
  useEffect(() => { onOpenCount(threads.filter(x => !x.resolved_at).length) }, [threads, onOpenCount])
  // 正文里画出评论的底色；正在写的新评论也画上（不然点进评论框，选中的那段就看不出来了）
  useEffect(() => {
    if (!editor || editor.isDestroyed || !doc.ready) return
    const marks = threads.map(th => ({ id: th.id, anchor: threadAnchor(th as any), resolved: !!th.resolved_at }))
    if (draft) marks.push({ id: '__draft', anchor: draft as any, resolved: false })
    setCommentThreads(editor, marks, draft ? '__draft' : active)
  }, [editor, doc.ready, threads, active, draft])
  useEffect(() => { if (panel !== 'comments') { setDraft(null); setActive(null) } }, [panel])
  // 等本机缓存或服务器第一次同步到了再画编辑器（连不上最多等 2.5 秒）：不然先闪一下“无标题”的空白页，再跳成正文
  if (!doc.ydoc || !doc.ready) return <div className="lib-body muted">{t('加载中…')}</div>
  return (
    <div className="doc-split oad-host">
      <div className="doc-col">
        <div className="scroller">
          <article className="doc-main" data-status={doc.status}>
            <OadTitle ydoc={doc.ydoc} placeholder={t('无标题')} onEnter={() => editor && focusStart(editor)} onChange={onTitle} />
            <OadEditor ydoc={doc.ydoc} provider={doc.provider} user={user} media={media} authors={allAuthors} onUnknownAuthor={loadAuthors}
              onReady={setEditor} onOpenLink={openLink}
              onComment={a => { setDraft(a); setActive(null); setPanel('comments') }}
              onOpenThreads={ids => { const id = ids.find(x => x !== '__draft'); if (!id) return; setDraft(null); setActive(id); setPanel('comments') }} />
          </article>
        </div>
        {panel === 'history' && version && (
          <VersionView client={client} pageId={page.id} version={version} onClose={() => setVersion(null)}
            onRestored={() => { setVersion(null); setHistKey(k => k + 1); setToast(t('已恢复')); setTimeout(() => setToast(''), 2000) }} />
        )}
        {toast && <div className="doc-toast" role="status">{toast}</div>}
      </div>
      {panel === 'comments' && (
        <OadComments pageId={page.id} me={me} editor={doc.ready ? editor : null} threads={threads} reload={reload}
          active={active} setActive={setActive} draft={draft} setDraft={setDraft}
          candidates={people.candidates} nameOf={people.nameOf} canWrite docVersion={0}
          onClose={() => { setDraft(null); setActive(null); setPanel(null) }} />
      )}
      {panel === 'history' && <HistoryPanel key={histKey} client={client} pageId={page.id} selected={version?.id ?? null} onSelect={setVersion}
        onClose={() => { setVersion(null); setPanel(null) }} />}
    </div>
  )
}
