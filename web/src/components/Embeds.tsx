import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../store'
import { lang, t } from '../i18n'

// 链接卡片和嵌入（像 Notion）：对话里单独一段的链接，YouTube、B 站显示封面，点了才加载播放器；X 的推文用官方的嵌入页；
// 其他网页由服务器取标题、摘要、配图（/api/unfurl），显示成书签卡片。抓不到就退回普通链接（fallback）。
// 这些要浏览器能访问对应的网站才显示：YouTube、X 在国内要翻墙；网页卡片看服务器能不能访问

export const MAX_EMBEDS = 3   // 一条消息最多展开几个

type Embed = { kind: 'youtube'; id: string; start: number; tall: boolean } | { kind: 'bilibili'; id: string; p: string }
  | { kind: 'tweet'; id: string } | { kind: 'card' }

const seconds = (v: string | null) => {
  if (!v) return 0
  if (/^\d+$/.test(v)) return Number(v)
  const m = v.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/)
  return m ? Number(m[1] || 0) * 3600 + Number(m[2] || 0) * 60 + Number(m[3] || 0) : 0
}

// 认链接：站内链接、不是 http(s) 的不展开（返回 null）
export function parseEmbed(href: string): Embed | null {
  let u: URL
  try { u = new URL(href, location.href) } catch { return null }
  if (!/^https?:$/.test(u.protocol) || u.origin === location.origin || !href.match(/^https?:\/\//)) return null
  const host = u.hostname.toLowerCase().replace(/^(www|m|mobile|music)\./, '')
  if (host === 'youtube.com' || host === 'youtube-nocookie.com') {
    const v = u.searchParams.get('v')
    if (u.pathname === '/watch' && v && /^[\w-]{11}$/.test(v)) return { kind: 'youtube', id: v, start: seconds(u.searchParams.get('t')), tall: false }
    const s = u.pathname.match(/^\/(shorts|live|embed)\/([\w-]{11})/)
    if (s) return { kind: 'youtube', id: s[2], start: seconds(u.searchParams.get('t')), tall: s[1] === 'shorts' }
  }
  if (host === 'youtu.be') {
    const id = u.pathname.slice(1).split('/')[0]
    if (/^[\w-]{11}$/.test(id)) return { kind: 'youtube', id, start: seconds(u.searchParams.get('t')), tall: false }
  }
  if (host === 'bilibili.com') {
    const m = u.pathname.match(/^\/video\/(BV[0-9A-Za-z]{10})/)
    if (m) return { kind: 'bilibili', id: m[1], p: (u.searchParams.get('p') || '').replace(/\D/g, '') }
  }
  if (host === 'x.com' || host === 'twitter.com') {
    const m = u.pathname.match(/^\/[^/]+\/status(?:es)?\/(\d+)/)
    if (m) return { kind: 'tweet', id: m[1] }
  }
  return { kind: 'card' }
}

// ---------- 网页卡片的信息：同一个地址只问服务器一次 ----------
export type Card = { url: string; title: string; description: string; image: string; site: string; icon: string }
const cards = new Map<string, Promise<Card | null>>()
const fetchCard = (url: string) => {
  if (!cards.has(url)) cards.set(url, api(`/api/unfurl?url=${encodeURIComponent(url)}`).then(d => (d.ok ? d as Card : null)).catch(() => null))
  return cards.get(url)!
}
// undefined：还在取；null：取不到
function useCard(url: string) {
  const [c, setC] = useState<Card | null | undefined>(undefined)
  useEffect(() => {
    let live = true
    fetchCard(url).then(d => { if (live) setC(d) })
    return () => { live = false }
  }, [url])
  return c
}

const hide = (e: React.SyntheticEvent<HTMLImageElement>) => { e.currentTarget.style.display = 'none' }
// 别人的网站：只给播放、弹窗（点推文里的链接）这些权限，不能把整个页面跳走
const SANDBOX = 'allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-presentation'

export function LinkEmbed({ href, fallback }: { href: string; fallback: ReactNode }) {
  const e = parseEmbed(href)
  if (!e) return <>{fallback}</>
  if (e.kind === 'youtube') return <YouTube e={e} href={href} />
  if (e.kind === 'bilibili') return <Bilibili e={e} href={href} />
  if (e.kind === 'tweet') return <Tweet id={e.id} href={href} />
  return <Bookmark href={href} fallback={fallback} />
}

function Bookmark({ href, fallback }: { href: string; fallback: ReactNode }) {
  const c = useCard(href)
  if (!c) return <>{fallback}</>
  return (
    <a className="lk-card" href={href} target="_blank" rel="noreferrer">
      <span className="lk-text">
        <span className="lk-title">{c.title}</span>
        {c.description && <span className="lk-desc">{c.description}</span>}
        <span className="lk-site">{c.icon && <img src={c.icon} alt="" referrerPolicy="no-referrer" onError={hide} />}<span>{c.site}</span></span>
      </span>
      {c.image && <span className="lk-img"><img src={c.image} alt="" loading="lazy" referrerPolicy="no-referrer"
        onError={e => { (e.currentTarget.parentElement as HTMLElement).style.display = 'none' }} /></span>}
    </a>
  )
}

// 封面 + 播放按钮，点了才换成播放器（不点就不加载对方的播放器）
function Player({ cover, src, title, tall, caption, href }: { cover?: string; src: string; title: string; tall?: boolean; caption: string; href: string }) {
  const [play, setPlay] = useState(false)
  return (
    <div className={'emb' + (tall ? ' tall' : '')}>
      <div className="emb-frame">
        {play ? <iframe src={src} title={title} allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowFullScreen sandbox={SANDBOX} />
          : <button className="emb-cover" onClick={() => setPlay(true)} aria-label={t('播放')} title={t('播放')}>
              {/* 不带 Referer：B 站的图片有防盗链 */}
              {cover && <img src={cover} alt="" referrerPolicy="no-referrer" onError={hide} />}
              <span className="emb-play">▶</span>
            </button>}
      </div>
      <a className="emb-cap" href={href} target="_blank" rel="noreferrer">{caption}</a>
    </div>
  )
}

function YouTube({ e, href }: { e: Extract<Embed, { kind: 'youtube' }>; href: string }) {
  const c = useCard(href)   // 只为了标题；服务器连不上 YouTube 也没关系
  return <Player cover={`https://i.ytimg.com/vi/${e.id}/hqdefault.jpg`} tall={e.tall} title={c?.title || 'YouTube'} href={href}
    src={`https://www.youtube-nocookie.com/embed/${e.id}?autoplay=1${e.start ? '&start=' + e.start : ''}`}
    caption={'YouTube' + (c?.title ? ' · ' + c.title : '')} />
}

function Bilibili({ e, href }: { e: Extract<Embed, { kind: 'bilibili' }>; href: string }) {
  const c = useCard(href)   // 封面和标题
  return <Player cover={c?.image} title={c?.title || 'bilibili'} href={href}
    src={`https://player.bilibili.com/player.html?bvid=${e.id}&autoplay=0${e.p ? '&p=' + e.p : ''}`}
    caption={t('B 站') + (c?.title ? ' · ' + c.title : '')} />
}

// X 的官方嵌入页放在 iframe 里（不在我们的页面加载 widgets.js），高度按它发来的消息调整：
// {"twttr.embed": {"method": "twttr.private.resize", "params": [{"height": …}]}}
function Tweet({ id, href }: { id: string; href: string }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const [h, setH] = useState(0)
  const dark = matchMedia('(prefers-color-scheme: dark)').matches
  useEffect(() => {
    const on = (e: MessageEvent) => {
      if (e.origin !== 'https://platform.twitter.com' || e.source !== ref.current?.contentWindow) return
      let d = e.data
      if (typeof d === 'string') { try { d = JSON.parse(d) } catch { return } }
      const m = d?.['twttr.embed']
      const height = m?.method === 'twttr.private.resize' ? Number(m.params?.[0]?.height) : 0
      if (height > 0) setH(Math.ceil(height))
    }
    window.addEventListener('message', on)
    return () => window.removeEventListener('message', on)
  }, [])
  const src = `https://platform.twitter.com/embed/Tweet.html?id=${id}&theme=${dark ? 'dark' : 'light'}&dnt=true&lang=${lang === 'en' ? 'en' : 'zh-cn'}`
  return (
    <div className="emb tweet">
      <iframe ref={ref} src={src} title="X" sandbox={SANDBOX} style={{ height: h || 240 }} className={h ? '' : 'loading'} />
      <a className="emb-cap" href={href} target="_blank" rel="noreferrer">{t('在 X 上打开')}</a>
    </div>
  )
}

// 用户自己发的消息是纯文字：单独一行的网址（最多 MAX_EMBEDS 个）显示在气泡下面
export function textEmbeds(text: string): string[] {
  return text.split('\n').map(l => l.trim()).filter(l => /^https?:\/\/\S+$/.test(l) && parseEmbed(l))
    .filter((l, i, a) => a.indexOf(l) === i).slice(0, MAX_EMBEDS)
}
