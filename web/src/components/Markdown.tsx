import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { t } from '../i18n'
import { openImage } from './Media'

function CodeBlock(props: any) {
  const { children, ...rest } = props
  const copy = (e: React.MouseEvent) => {
    const pre = (e.currentTarget as HTMLElement).parentElement?.querySelector('code')
    navigator.clipboard?.writeText(pre?.textContent || '')
    ;(e.currentTarget as HTMLElement).textContent = t('已复制')
    setTimeout(() => ((e.target as HTMLElement).textContent = t('复制')), 1200)
  }
  return <div className="code-wrap"><button className="code-copy" onClick={copy}>{t('复制')}</button><pre {...rest}>{children}</pre></div>
}

// 站内的资料库链接（/library/pages/12）在应用里打开；其余链接开新标签页
function Link({ node, href, ...p }: any) {
  const page = href?.match(/^\/library\/pages\/(\d+)$/)
  if (page) return <a {...p} href={href} onClick={e => {
    if (e.metaKey || e.ctrlKey) return
    e.preventDefault(); window.dispatchEvent(new CustomEvent('osa-open-page', { detail: Number(page[1]) }))
  }} />
  return <a {...p} href={href} target="_blank" rel="noreferrer" />
}

const AUDIO = /\.(mp3|m4a|wav|ogg|oga|opus|aac|flac)$/i
const VIDEO = /\.(mp4|webm|mov|m4v|ogv)$/i
const pathOf = (href: string) => { try { return new URL(href, location.href).pathname } catch { return href || '' } }

// 图片限宽限高，点开看大图；![](x.mp4) 这样写的视频直接播放
function Img({ node, src, alt, ...p }: any) {
  if (typeof src === 'string' && VIDEO.test(pathOf(src))) return <video className="md-video" controls preload="metadata" src={src} />
  return <img {...p} src={src} alt={alt || ''} loading="lazy" onClick={() => src && openImage({ src, name: alt || undefined })} />
}

// 段落里只有一个链接（一个裸网址，或者一个 [文字](网址)）时返回它，句子里的链接不算
export function soleLink(node: any): { href: string } | null {
  const kids = (node?.children || []).filter((c: any) => !(c.type === 'text' && !c.value.trim()))
  const a = kids.length === 1 && kids[0].type === 'element' && kids[0].tagName === 'a' ? kids[0] : null
  return a && typeof a.properties?.href === 'string' ? { href: a.properties.href } : null
}

// 单独一段的音频、视频链接换成播放器，链接留在下面
function Para({ node, children, ...p }: any) {
  const one = soleLink(node)
  const path = one ? pathOf(one.href) : ''
  if (one && AUDIO.test(path)) return <div className="md-media"><audio controls preload="metadata" src={one.href} /><div className="md-media-cap">{children}</div></div>
  if (one && VIDEO.test(path)) return <div className="md-media"><video className="md-video" controls preload="metadata" src={one.href} /><div className="md-media-cap">{children}</div></div>
  return <p {...p}>{children}</p>
}

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
        components={{ pre: CodeBlock, a: Link, img: Img, p: Para }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
export default Markdown
