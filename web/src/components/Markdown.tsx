import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { t } from '../i18n'
import { openImage } from './Media'
import { LinkEmbed, MAX_EMBEDS, isMedia, parseEmbed } from './Embeds'
import { PageEmbed } from './Applet'

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

// 模型常在表格单元格里写 <br> 换行。网页里的 HTML 一律不当真（防注入），只把单独的 <br> 换成真的换行
function brTags() {
  const walk = (node: any) => {
    if (!node.children) return
    node.children = node.children.map((c: any) => (c.type === 'html' && /^<br\s*\/?>$/i.test(c.value.trim()) ? { type: 'break' } : c))
    node.children.forEach(walk)
  }
  return (tree: any) => walk(tree)
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

const isPage = (href: string) => /^\/library\/pages\/\d+$/.test(href)   // 站内的页面：小应用原地显示，普通页面显示成卡片（Applet.tsx）
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

// 要展开的链接（按顺序，前 MAX_EMBEDS 个，同一个只展开一次）标上 dataEmbed，显示成卡片或播放器：
// - 单独一段的链接：原地换掉
// - 句子里、列表里的视频和推文链接：在它所在的那一块（段落、列表、引用）后面加一段
// 代码块、表格里的不管
const SKIP = ['pre', 'table', 'code']
function markEmbeds() {
  return (tree: any) => {
    const seen = new Set<string>()
    const take = (href: string) => (seen.size < MAX_EMBEDS && !seen.has(href) ? (seen.add(href), true) : false)
    const sole = (node: any) => {
      for (const c of node.children || []) {
        if (c.type !== 'element' || SKIP.includes(c.tagName)) continue
        if (c.tagName !== 'p') { sole(c); continue }
        const one = soleLink(c)
        const path = one ? pathOf(one.href) : ''
        if (one && (isPage(one.href) || (!AUDIO.test(path) && !VIDEO.test(path) && parseEmbed(one.href))) && take(one.href))
          c.properties = { ...c.properties, dataEmbed: one.href }
      }
    }
    const inline = (node: any, out: string[]) => {
      for (const c of node.children || []) {
        if (c.type !== 'element' || SKIP.includes(c.tagName) || c.properties?.dataEmbed) continue
        const href = c.tagName === 'a' ? c.properties?.href : null
        if (typeof href === 'string' && (isMedia(href) || isPage(href))) out.push(href)   // 站内页面写在句子里也展开（小应用要能直接用）
        else inline(c, out)
      }
      return out
    }
    const kids: any[] = []
    for (const top of tree.children || []) {
      kids.push(top)
      if (top.type !== 'element' || SKIP.includes(top.tagName)) continue
      sole({ children: [top] })
      for (const href of inline({ children: [top] }, []))
        if (take(href)) kids.push({ type: 'element', tagName: 'p', properties: { dataEmbed: href }, children: [] })
    }
    tree.children = kids
  }
}

// 单独一段的音频、视频链接换成播放器，链接留在下面；标了 dataEmbed 的换成卡片或嵌入
function Para({ node, children, ...p }: any) {
  if (node?.properties?.dataEmbed) {
    const href = node.properties.dataEmbed
    if (isPage(href)) return <PageEmbed href={href} fallback={<p {...p}>{children}</p>} />
    return <LinkEmbed href={href} fallback={<p {...p}>{children}</p>} />
  }
  const one = soleLink(node)
  const path = one ? pathOf(one.href) : ''
  if (one && AUDIO.test(path)) return <div className="md-media"><audio controls preload="metadata" src={one.href} /><div className="md-media-cap">{children}</div></div>
  if (one && VIDEO.test(path)) return <div className="md-media"><video className="md-video" controls preload="metadata" src={one.href} /><div className="md-media-cap">{children}</div></div>
  return <p {...p}>{children}</p>
}

// embeds：链接展开成卡片、播放器（对话里说完的消息才开；流式输出、资料库、任务过程里不开）
const Markdown = memo(function Markdown({ text, embeds }: { text: string; embeds?: boolean }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }], brTags]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }], ...(embeds ? [markEmbeds] : [])]}
        components={{ pre: CodeBlock, a: Link, img: Img, p: Para }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
export default Markdown
