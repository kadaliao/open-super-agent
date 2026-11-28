// 文档的历史：右侧的版本列表（谁、什么时候、自动 / Bot / 恢复 / 手动存的），点一版在正文的位置看它和现在按块的对比，能恢复、能给现在存一版。
// 从 Open Agent Docs 的网页（apps/web/src/history/History.tsx）搬来，接口走 @open-agent-docs/client，文字和确认框用 OSA 自己的。
import type { createClient } from '@open-agent-docs/client'
import { useEffect, useMemo, useState } from 'react'
import { locale, t } from '../i18n'
import { Icon } from '../icons'
import { ask } from './Dialog'
import './DocHistory.css'

type Client = ReturnType<typeof createClient>
type Person = { kind: string; id: string; name: string }
export type Version = { id: number; kind: string; label?: string | null; created_at: number; authors?: Person[] }
type DiffBlock = { id?: string | null; op: 'same' | 'added' | 'removed' | 'changed'; type?: string | null; before?: string | null; after?: string | null; by?: Person[] }

const KIND: Record<string, () => string> = {
  auto: () => t('自动存的'), agent: () => t('Bot 改的'), restore: () => t('恢复出来的'), manual: () => t('手动存的'),
}
const when = (ms: number) => new Date(ms).toLocaleString(locale, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const COLORS = ['#7c9cff', '#f6c453', '#5cc8ff', '#c792ea', '#ff6b9a', '#4fd1a5', '#ff9f5a']
const colorOf = (id: string) => COLORS[[...id].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % COLORS.length]

function People({ people }: { people?: Person[] }) {
  if (!people?.length) return null
  return (
    <span className="hist-people">
      {people.slice(0, 4).map(p => (
        <span key={p.kind + p.id} className="hist-av" style={{ background: colorOf(p.id) }} title={p.name}>
          {p.kind === 'agent' ? <Icon name="sparkles" size={14} /> : (p.name || '?').slice(0, 1)}
        </span>
      ))}
      <span className="hist-names">{people.map(p => p.name).join('、')}</span>
    </span>
  )
}

export function HistoryPanel({ client, pageId, selected, onSelect, onClose }: {
  client: Client; pageId: string; selected: number | null; onSelect: (v: Version | null) => void; onClose: () => void
}) {
  const [versions, setVersions] = useState<Version[] | null>(null)
  const [error, setError] = useState('')
  const [naming, setNaming] = useState(false)
  const [label, setLabel] = useState('')
  const load = () => client.versions(pageId).then(
    v => { setVersions([...(v.versions as Version[])].sort((a, b) => b.created_at - a.created_at)); setError('') },
    (e: any) => { setVersions([]); setError(e?.message || String(e)) },
  )
  useEffect(() => { load() }, [pageId]) // eslint-disable-line react-hooks/exhaustive-deps
  const save = async () => {
    const l = label.trim()
    if (!l) return
    try { await client.saveVersion(pageId, l); setNaming(false); setLabel(''); load() } catch (e: any) { setError(e?.message || String(e)) }
  }
  return (
    <aside className="hist" aria-label={t('历史版本')}>
      <div className="hist-head">
        <h2>{t('历史版本')}</h2>
        <button className="icon-btn" onClick={onClose} aria-label={t('关闭')}><Icon name="x" size={16} /></button>
      </div>
      <div className="hist-name">
        {naming
          ? <form className="hist-name-form" onSubmit={e => { e.preventDefault(); save() }}>
              <input autoFocus value={label} placeholder={t('给这一版起个名字')} maxLength={80}
                onChange={e => setLabel(e.target.value)} onKeyDown={e => e.key === 'Escape' && setNaming(false)} />
              <button className="btn btn-sm btn-primary" disabled={!label.trim()}>{t('存一版')}</button>
            </form>
          : <button className="btn btn-sm hist-name-btn" onClick={() => setNaming(true)}><Icon name="plus" size={16} className="lead" />{t('给现在存一版')}</button>}
      </div>
      <div className="hist-list" role="listbox" aria-label={t('历史版本')}>
        <button className={'hist-item is-current' + (!selected ? ' is-on' : '')} role="option" aria-selected={!selected} onClick={() => onSelect(null)}>
          <span className="hist-dot" />
          <span className="hist-main"><span className="hist-when">{t('现在')}</span></span>
        </button>
        {error && <div className="hist-note">{error}</div>}
        {versions && !versions.length && !error && <div className="hist-note">{t('还没有存过的版本。编辑停下两分钟会自动存一版。')}</div>}
        {(versions ?? []).map(v => (
          <button key={v.id} className={'hist-item' + (selected === v.id ? ' is-on' : '') + ' is-' + v.kind} role="option"
            aria-selected={selected === v.id} onClick={() => onSelect(v)}>
            <span className="hist-dot" />
            <span className="hist-main">
              <span className="hist-when">{v.label ? <b>{v.label}</b> : when(v.created_at)}</span>
              <span className="hist-meta">
                <span className={'hist-kind is-' + v.kind}>{(KIND[v.kind] ?? KIND.auto)()}</span>
                {v.label && <span>{when(v.created_at)}</span>}
              </span>
              <People people={v.authors} />
            </span>
          </button>
        ))}
      </div>
    </aside>
  )
}

// ---------- 对比 ----------
type Part = { op: 'same' | 'del' | 'ins'; s: string }

/** 按词（英文单词、中文单字）比两段文字 */
function wordDiff(a: string, b: string): Part[] {
  const tok = (s: string) => s.match(/[A-Za-z0-9_]+|\s+|[^\sA-Za-z0-9_]/g) ?? []
  const x = tok(a), y = tok(b)
  if (x.length * y.length > 400_000) return [...(a ? [{ op: 'del' as const, s: a }] : []), ...(b ? [{ op: 'ins' as const, s: b }] : [])]
  return lcs(x, y, true)
}

function lcs(x: string[], y: string[], merge: boolean): Part[] {
  const n = x.length, m = y.length
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out: Part[] = []
  const push = (op: Part['op'], s: string) => {
    const last = out[out.length - 1]
    if (merge && last && last.op === op) last.s += s
    else out.push({ op, s })
  }
  let i = 0, j = 0
  while (i < n && j < m) {
    if (x[i] === y[j]) { push('same', x[i]); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) push('del', x[i++])
    else push('ins', y[j++])
  }
  while (i < n) push('del', x[i++])
  while (j < m) push('ins', y[j++])
  return out
}

/** 一行 Markdown 里的行内格式 → HTML（先全部转义） */
function inlineMd(s: string): string {
  let h = s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!)
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>')
  h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
  h = h.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
  h = h.replace(/~~([^~]+)~~/g, '<s>$1</s>')
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
  return h.replace(/\n/g, '<br>')
}

/** 一块 Markdown 画成大致的样子（标题、列表、待办、引用、代码） */
function MdBlock({ md, parts }: { md: string; parts?: Part[] }) {
  if (!parts && md.includes('\n') && /^\s*([-*+]|\d+\.)\s/.test(md)) return <>{md.split('\n').map((l, i) => <MdBlock key={i} md={l} />)}</>
  const m = /^(#{1,6})\s|^(\s*)([-*+]|\d+\.)\s(\[[ xX]\]\s)?|^>\s?|^```/.exec(md)
  let cls = 'hb-p'
  let body = md
  if (m?.[1]) { cls = 'hb-h' + m[1].length; body = md.slice(m[0].length) }
  else if (m?.[3]) { cls = m[4] ? 'hb-todo' + (/x/i.test(m[4]) ? ' is-done' : '') : /\d/.test(m[3]) ? 'hb-ol' : 'hb-ul'; body = md.slice(m[0].length) }
  else if (md.startsWith('>')) { cls = 'hb-quote'; body = md.replace(/^>\s?/gm, '') }
  else if (md.startsWith('```')) { cls = 'hb-code'; body = md.replace(/^```\w*\n?/, '').replace(/\n?```$/, '') }
  if (parts) {
    const skip = md.length - body.length   // 前缀（# 、- ）不进对比
    let used = 0
    const shown = parts.map(p => {
      if (p.op === 'ins') return p
      const start = used
      used += p.s.length
      if (used <= skip) return null
      return start < skip ? { ...p, s: p.s.slice(skip - start) } : p
    }).filter(Boolean) as Part[]
    return <div className={'hb ' + cls}>{shown.map((p, i) => p.op === 'same' ? <span key={i}>{p.s}</span> : p.op === 'del' ? <del key={i}>{p.s}</del> : <ins key={i}>{p.s}</ins>)}</div>
  }
  const html = cls === 'hb-code' ? body.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]!) : inlineMd(body)
  return <div className={'hb ' + cls} dangerouslySetInnerHTML={{ __html: html }} />
}

type Line = { op: 'same' | 'del' | 'ins' | 'mod'; md: string; parts?: Part[] }
/** 多行的块（列表、引用）：先按行对，成对的增删再按词对 */
function lineDiff(a: string, b: string): Line[] {
  const ops = lcs(a.split('\n'), b.split('\n'), false)
  const out: Line[] = []
  for (let k = 0; k < ops.length; k++) {
    const o = ops[k], n = ops[k + 1]
    if (o.op === 'del' && n?.op === 'ins') { out.push({ op: 'mod', md: n.s, parts: wordDiff(o.s, n.s) }); k++ }
    else out.push({ op: o.op, md: o.s })
  }
  return out
}
const plain = (p: Part[]) => p.map(x => ({ ...x, s: x.s.replace(/\*\*|~~|`/g, '') }))

function DiffRow({ b }: { b: DiffBlock }) {
  const lines = useMemo(() => (b.op === 'changed' && !(b.type ?? '').startsWith('code') ? lineDiff(b.before ?? '', b.after ?? '') : null), [b])
  const md = (b.op === 'removed' ? b.before : b.after ?? b.before) ?? ''
  const tag = b.op === 'added' ? t('加上的') : b.op === 'removed' ? t('删掉的') : t('改过的')
  return (
    <div className={'hd is-' + b.op}>
      {b.op !== 'same' && <span className="hd-tag">{tag}{b.by?.length ? ' · ' + b.by.map(p => p.name).join('、') : ''}</span>}
      {lines ? lines.map((l, i) => <div key={i} className={'hd-line is-' + l.op}><MdBlock md={l.md} parts={l.parts ? plain(l.parts) : undefined} /></div>)
        : b.op === 'changed' ? <MdBlock md={md} parts={plain(wordDiff(b.before ?? '', b.after ?? ''))} />
        : <MdBlock md={md} />}
    </div>
  )
}

export function VersionView({ client, pageId, version, onClose, onRestored }: {
  client: Client; pageId: string; version: Version; onClose: () => void; onRestored: () => void
}) {
  const [diff, setDiff] = useState<DiffBlock[] | null>(null)
  const [error, setError] = useState('')
  const [only, setOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    setDiff(null); setError('')
    client.versionDiff(pageId, version.id).then(d => setDiff((d.blocks ?? []) as DiffBlock[]), (e: any) => setError(e?.message || String(e)))
  }, [client, pageId, version.id])
  const changed = diff?.filter(b => b.op !== 'same').length ?? 0
  const shown = only ? diff?.filter(b => b.op !== 'same') : diff
  const name = version.label || when(version.created_at)
  const restore = async () => {
    if (!(await ask(t('把文档恢复到「{name}」的样子？恢复是一次新的改动，之后的历史都还在。', { name }), { ok: t('恢复这一版') }))) return
    setBusy(true)
    try { await client.restoreVersion(pageId, version.id); onRestored() } catch (e: any) { setError(e?.message || String(e)); setBusy(false) }
  }
  return (
    <div className="hv">
      <div className="hv-bar">
        <div className="hv-title">
          <b>{name}</b>
          <span className="muted">{t('和现在比')}{diff ? ' · ' + (changed ? t('{n} 处不同', { n: changed }) : t('和现在一样')) : ''}</span>
        </div>
        <label className="hv-only"><input type="checkbox" checked={only} onChange={e => setOnly(e.target.checked)} />{t('只看改动')}</label>
        <button className="btn btn-sm btn-primary" disabled={busy} onClick={restore}>{t('恢复这一版')}</button>
        <button className="icon-btn" onClick={onClose} aria-label={t('关闭')}><Icon name="x" size={16} /></button>
      </div>
      <article className="hv-doc">
        {error && <div className="error-line">{error}</div>}
        {!diff && !error && <div className="muted">{t('加载中…')}</div>}
        {shown?.map((b, i) => <DiffRow key={(b.id ?? '') + i} b={b} />)}
        {diff && !changed && <div className="hist-note">{t('和现在一样')}</div>}
      </article>
    </div>
  )
}
