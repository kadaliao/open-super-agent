import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

function CodeBlock(props: any) {
  const { children, ...rest } = props
  const copy = (e: React.MouseEvent) => {
    const pre = (e.currentTarget as HTMLElement).parentElement?.querySelector('code')
    navigator.clipboard?.writeText(pre?.textContent || '')
    ;(e.currentTarget as HTMLElement).textContent = '已复制'
    setTimeout(() => ((e.target as HTMLElement).textContent = '复制'), 1200)
  }
  return <div className="code-wrap"><button className="code-copy" onClick={copy}>复制</button><pre {...rest}>{children}</pre></div>
}

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
        components={{ pre: CodeBlock, a: ({ node, ...p }) => <a {...p} target="_blank" rel="noreferrer" /> }}>
        {text}
      </ReactMarkdown>
    </div>
  )
})
export default Markdown
