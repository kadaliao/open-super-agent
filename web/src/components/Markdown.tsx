import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { t } from '../i18n'

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

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
        components={{ pre: CodeBlock, a: Link }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
export default Markdown
