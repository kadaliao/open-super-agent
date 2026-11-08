import { useEffect, useRef, useState } from 'react'
import type { Attachment } from '../types'
import { t } from '../i18n'
import { Icon } from '../icons'
import { useBack } from '../swipe'

// 对话里的图片和附件：缩略图、文件卡片、看大图。图片存在服务端的 media 表（/api/media/<id>），
// 文件在电脑里（/api/files/download?path=…）。附件的格式见 osa/media.py

export const fmtSize = (n: number) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`
export const mediaUrl = (id: string) => `/api/media/${id}`
export const fileUrl = (path: string) => `/api/files/download?path=${encodeURIComponent(path)}`

// 大图先在浏览器里缩到最长边 MAX 像素再传：显示够用，给模型看也够，电脑没开时也不用去取原图。
// 小图、GIF（可能是动图）原样传；PNG 缩完还是 PNG（截图里的字清楚），其余转 JPEG（浏览器能编码 WebP 的转 WebP）
const MAX = 1600
const SMALL = 1.5 << 20
export async function shrinkImage(f: File): Promise<Blob> {
  if (f.type === 'image/gif') return f
  const url = URL.createObjectURL(f)
  try {
    const img = new Image()
    img.src = url
    await img.decode()
    const w = img.naturalWidth, h = img.naturalHeight
    const native = ['image/png', 'image/jpeg', 'image/webp'].includes(f.type)
    if (native && Math.max(w, h) <= MAX && f.size <= SMALL) return f
    const k = Math.min(1, MAX / Math.max(w, h))
    const c = document.createElement('canvas')
    c.width = Math.round(w * k); c.height = Math.round(h * k)
    const ctx = c.getContext('2d')!
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(img, 0, 0, c.width, c.height)
    const blob = (type: string, q?: number) => new Promise<Blob | null>(r => c.toBlob(r, type, q))
    if (f.type === 'image/png') return (await blob('image/png')) || f
    const webp = await blob('image/webp', 0.86)
    return (webp?.type === 'image/webp' ? webp : await blob('image/jpeg', 0.86)) || f
  } finally { URL.revokeObjectURL(url) }
}

export type MediaInfo = { id: string; mime: string; name: string; size: number; w: number; h: number }
export async function uploadMedia(blob: Blob, name: string): Promise<MediaInfo> {
  const r = await fetch(`/api/media?name=${encodeURIComponent(name)}`, { method: 'PUT', body: blob })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d.detail || t('上传失败（{status}）', { status: r.status }))
  return d
}

// 消息正文里的“[附件] 路径”行：有 meta.attachments 时藏起来，改成显示附件；旧消息没有的，按路径显示成文件卡片
const MARK = /^\[附件\] (.+)$/   // i18n-ignore：消息里的附件标记
export function splitMarks(content: string, atts?: Attachment[]): { text: string; items: Attachment[] } {
  const lines = content.split('\n')
  const marks = lines.filter(l => MARK.test(l)).map(l => l.match(MARK)![1].trim())
  const text = lines.filter(l => !MARK.test(l)).join('\n').trim()
  if (atts?.length) return { text, items: atts }
  return { text, items: marks.map(p => ({ name: p.split('/').pop() || p, path: p })) }
}

// ---------- 看大图 ----------
// actions：大图下面的按钮（比如“设为头像”）
export type ViewerItem = { src: string; name?: string; download?: string; actions?: { label: string; run: () => void }[] }
export const openImage = (v: ViewerItem) => window.dispatchEvent(new CustomEvent('osa-image', { detail: v }))

export function ImageViewer() {
  const [v, setV] = useState<ViewerItem | null>(null)
  const root = useRef<HTMLDivElement>(null)
  useBack(() => setV(null), root, !!v)
  useEffect(() => {
    const h = (e: Event) => setV((e as CustomEvent).detail)
    window.addEventListener('osa-image', h)
    return () => window.removeEventListener('osa-image', h)
  }, [])
  useEffect(() => {
    if (!v) return
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); setV(null) } }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [v])
  if (!v) return null
  return (
    <div className="modal-bg viewer" ref={root} onClick={() => setV(null)}>
      <img src={v.src} alt={v.name || ''} onClick={e => e.stopPropagation()} />
      <div className="viewer-bar" onClick={e => e.stopPropagation()}>
        {v.name && <span className="viewer-name" title={v.name}>{v.name}</span>}
        {v.actions?.map(a => <button key={a.label} className="btn" onClick={() => { setV(null); a.run() }}>{a.label}</button>)}
        <a className="btn" href={v.download || v.src} download={v.name || true}>{t('下载')}</a>
        <a className="btn" href={v.src} target="_blank" rel="noreferrer" title={t('在新标签页打开')}><Icon name="external" size={14} /></a>
        <button className="btn" onClick={() => setV(null)} title={t('关闭')}><Icon name="x" size={14} /></button>
      </div>
    </div>
  )
}

// ---------- 附件 ----------
const EXT_ICON: [RegExp, string][] = [
  [/\.(pdf)$/i, 'PDF'], [/\.(docx?|odt|rtf|pages)$/i, 'DOC'], [/\.(xlsx?|csv|tsv|numbers|ods)$/i, 'XLS'],
  [/\.(pptx?|key|odp)$/i, 'PPT'], [/\.(zip|rar|7z|tar|gz|tgz|bz2|xz)$/i, 'ZIP'], [/\.(mp3|m4a|wav|ogg|aac|flac)$/i, '♪'],
  [/\.(mp4|webm|mov|m4v|mkv|avi)$/i, '▶'], [/\.(png|jpe?g|gif|webp|svg|heic|bmp)$/i, 'IMG'], [/\.(md|txt|log|json|ya?ml|html?)$/i, 'TXT'],
]
export const extLabel = (name: string) => EXT_ICON.find(([re]) => re.test(name))?.[1] || (name.split('.').pop() || '').slice(0, 4).toUpperCase() || 'FILE'

export function FileCard({ a }: { a: Attachment }) {
  const body = <>
    <span className="file-ext">{extLabel(a.name)}</span>
    <span className="file-main"><span className="file-name" title={a.path || a.name}>{a.name}</span>
      <span className="file-sub">{a.size != null ? fmtSize(a.size) : t('电脑里的文件')}</span></span>
  </>
  return a.path ? <a className="file-card" href={fileUrl(a.path)} target="_blank" rel="noreferrer" title={t('打开（电脑没开会先开机）')}>{body}</a>
    : <span className="file-card">{body}</span>
}

// 消息里的附件：图片显示缩略图（一张按原比例，几张排成方格），其余是文件卡片。actions：看大图时多出的按钮
export function Attachments({ items, actions }: { items?: Attachment[]; actions?: (a: Attachment) => ViewerItem['actions'] }) {
  if (!items?.length) return null
  const imgs = items.filter(a => a.media), files = items.filter(a => !a.media)
  const open = (a: Attachment) => openImage({ src: mediaUrl(a.media!), name: a.name, download: mediaUrl(a.media!) + '?download=1', actions: actions?.(a) })
  return (
    <div className="atts">
      {imgs.length > 0 && <div className={'att-imgs' + (imgs.length > 1 ? ' grid' : '')}>
        {imgs.map(a => (
          <button key={a.media} className="att-img" onClick={() => open(a)} title={a.name}>
            <img src={mediaUrl(a.media!)} alt={a.name} loading="lazy" width={a.w || undefined} height={a.h || undefined} />
          </button>))}
      </div>}
      {files.map((a, i) => <FileCard key={(a.path || a.name) + i} a={a} />)}
    </div>
  )
}
