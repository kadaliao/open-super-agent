import { useEffect, useRef, useState } from 'react'
import type { Attachment } from '../types'
import { t } from '../i18n'
import { Icon } from '../icons'
import { useBack } from '../swipe'

// 对话里的图片和附件：缩略图、文件卡片、音视频播放、看大图。图片存在服务端的 media 表（/api/media/<id>），
// 文件在电脑里（/api/files/download?path=…，派到 Mac 上的任务发的带 &mac=）。附件的格式见 osa/media.py
//
// 下载一律用带 download 属性的链接：桌面 App、手机 App 的外壳只在主窗口里接下载，
// 开新窗口去下载（target=_blank）在 App 里只会弹出一个空白窗口。

export const fmtSize = (n: number) => n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / (1 << 20)).toFixed(1)} MB`
export const mediaUrl = (id: string) => `/api/media/${id}`
// download：一律当附件下载（不在浏览器里打开）
export const fileUrl = (path: string, mac?: number | null, download?: boolean) =>
  `/api/files/download?path=${encodeURIComponent(path)}${mac ? `&mac=${mac}` : ''}${download ? '&download=1' : ''}`

export const VIDEO = /\.(mp4|m4v|webm|mov|ogv)$/i
export const AUDIO = /\.(mp3|m4a|wav|ogg|oga|opus|aac|flac)$/i
// 浏览器里能直接看的（服务端 inline 发的那些）：点了开新窗口看；其余点了就下载
const PREVIEW = /\.(pdf|txt|png|jpe?g|gif|webp|bmp|avif|mp3|m4a|wav|ogg|oga|opus|aac|flac)$/i
export const canPreview = (name: string) => PREVIEW.test(name)

// <video>、<audio> 放不了时不说为什么：再问一次服务器，它有报错（Mac 没连上、文件没了）就用它的话
async function whyFailed(url: string): Promise<string> {
  try {
    const r = await fetch(url, { headers: { Range: 'bytes=0-0' } })
    if (!r.ok) return (await r.json().catch(() => ({}))).detail || t('打不开（{status}）', { status: r.status })
  } catch { return t('连不上服务器') }
  return t('浏览器放不了这个格式，可以下载下来看')
}

export function DownloadBtn({ href, name, className = 'att-dl' }: { href: string; name?: string; className?: string }) {
  return <a className={className} href={href} download={name || true} title={t('下载')} aria-label={t('下载')}
    onClick={e => e.stopPropagation()}><Icon name="download" size={15} /></a>
}

// 电脑里的一个文件：视频在看大图的框里播放，浏览器能直接看的开新窗口，其余直接下载
export function FileLink({ path, mac, name, className, children }: {
  path: string; mac?: number | null; name: string; className?: string; children: React.ReactNode
}) {
  if (VIDEO.test(name)) return <button className={className} title={t('播放')}
    onClick={() => openImage({ src: fileUrl(path, mac), name, download: fileUrl(path, mac, true), video: true })}>{children}</button>
  if (canPreview(name)) return <a className={className} href={fileUrl(path, mac)} target="_blank" rel="noreferrer"
    title={t('打开（电脑没开会先开机）')}>{children}</a>
  return <a className={className} href={fileUrl(path, mac, true)} download={name} title={t('下载（电脑没开会先开机）')}>{children}</a>
}

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

// ---------- 看大图、看视频 ----------
// actions：大图下面的按钮（比如“设为头像”）；video：src 是视频，在框里播放
export type ViewerItem = { src: string; name?: string; download?: string; video?: boolean; actions?: { label: string; run: () => void }[] }
export const openImage = (v: ViewerItem) => window.dispatchEvent(new CustomEvent('osa-image', { detail: v }))

export function ImageViewer() {
  const [v, setV] = useState<ViewerItem | null>(null)
  const [err, setErr] = useState('')
  const root = useRef<HTMLDivElement>(null)
  useBack(() => setV(null), root, !!v)
  useEffect(() => {
    const h = (e: Event) => { setErr(''); setV((e as CustomEvent).detail) }
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
      {!v.video ? <img src={v.src} alt={v.name || ''} onClick={e => e.stopPropagation()} />
        : err ? <div className="viewer-err" onClick={e => e.stopPropagation()}>{err}</div>
        : <video key={v.src} src={v.src} controls autoPlay playsInline onClick={e => e.stopPropagation()} onError={() => whyFailed(v.src).then(setErr)} />}
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

// 文件卡片：点了打开或下载（见 FileLink），浏览器能直接看的旁边另有下载按钮
export function FileCard({ a }: { a: Attachment }) {
  const body = <>
    <span className="file-ext">{extLabel(a.name)}</span>
    <span className="file-main"><span className="file-name" title={a.path || a.name}>{a.name}</span>
      <span className="file-sub">{a.size != null ? fmtSize(a.size) : t('电脑里的文件')}</span></span>
  </>
  if (!a.path) return <span className="file-card">{body}</span>
  return (
    <span className="file-card">
      <FileLink className="file-open" path={a.path} mac={a.mac} name={a.name}>{body}</FileLink>
      <DownloadBtn href={fileUrl(a.path, a.mac, true)} name={a.name} />
    </span>
  )
}

// 视频、音频：直接在对话里放。点了播放才开始取（preload=none）：翻到这条消息不会去开电脑、连 Mac，手机上也不白耗流量
function MediaCard({ a, kind }: { a: Attachment; kind: 'video' | 'audio' }) {
  const el = useRef<HTMLVideoElement & HTMLAudioElement>(null)
  const [on, setOn] = useState(kind === 'audio')
  const [err, setErr] = useState('')
  const url = fileUrl(a.path!, a.mac)
  const play = () => {
    setOn(true)
    if (err) { setErr(''); el.current?.load() }
    el.current?.play().catch(() => {})   // 在点击里直接 play()：手机上才允许出声；放不了时下面的 onError 会说原因
  }
  const fail = () => whyFailed(url).then(setErr)
  return (
    <div className={`att-media ${kind}${on ? ' on' : ''}`}>
      {kind === 'video' ? <div className="att-video">
        <video ref={el} src={url} preload="none" controls={on} playsInline onError={fail} />
        {!on && <button className="att-play" onClick={play} title={t('播放')} aria-label={t('播放')}><Icon name="play" size={26} /></button>}
      </div> : <audio ref={el} src={url} preload="none" controls onError={fail} />}
      <div className="att-media-bar">
        <span className="file-name" title={a.path}>{a.name}</span>
        {a.size != null && <span className="file-sub">{fmtSize(a.size)}</span>}
        <DownloadBtn href={fileUrl(a.path!, a.mac, true)} name={a.name} />
      </div>
      {err && <div className="att-err">{err}</div>}
    </div>
  )
}

const kindOf = (a: Attachment) => a.media ? 'image' : a.path && VIDEO.test(a.name) ? 'video' : a.path && AUDIO.test(a.name) ? 'audio' : 'file'

// 消息里的附件：图片显示缩略图（一张按原比例，几张排成方格），视频、音频直接放，其余是文件卡片。actions：看大图时多出的按钮
export function Attachments({ items, actions }: { items?: Attachment[]; actions?: (a: Attachment) => ViewerItem['actions'] }) {
  if (!items?.length) return null
  const imgs = items.filter(a => kindOf(a) === 'image'), rest = items.filter(a => kindOf(a) !== 'image')
  const dl = (a: Attachment) => mediaUrl(a.media!) + '?download=1'
  const open = (a: Attachment) => openImage({ src: mediaUrl(a.media!), name: a.name, download: dl(a), actions: actions?.(a) })
  return (
    <div className="atts">
      {imgs.length > 0 && <div className={'att-imgs' + (imgs.length > 1 ? ' grid' : '')}>
        {imgs.map(a => (
          <div key={a.media} className="att-img-box">
            <button className="att-img" onClick={() => open(a)} title={a.name}>
              <img src={mediaUrl(a.media!)} alt={a.name} loading="lazy" width={a.w || undefined} height={a.h || undefined} />
            </button>
            <DownloadBtn className="att-dl att-img-dl" href={dl(a)} name={a.name} />
          </div>))}
      </div>}
      {rest.map((a, i) => {
        const k = kindOf(a)
        return k === 'video' || k === 'audio' ? <MediaCard key={(a.path || a.name) + i} a={a} kind={k} /> : <FileCard key={(a.path || a.name) + i} a={a} />
      })}
    </div>
  )
}
