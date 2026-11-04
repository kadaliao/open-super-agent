import { useEffect, useRef, useState } from 'react'
import { send, useStore } from '../store'
import type { Bot } from '../types'

export default function Composer({ chatId, bot, busy }: { chatId: number; bot: Bot; busy: boolean }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  const apps = useStore(s => s.apps)
  const [pick, setPick] = useState(0)
  // 光标前面是 @xxx 时弹出应用列表
  const at = text.match(/(?:^|\s)@([\w-]*)$/)
  const matches = at ? apps.filter(a => a.name.startsWith(at[1].toLowerCase())) : []
  const choose = (name: string) => {
    setText(text.replace(/@([\w-]*)$/, `@${name} `)); setPick(0); ref.current?.focus()
  }

  useEffect(() => { ref.current?.focus() }, [chatId])
  useEffect(() => {
    const fill = (e: Event) => { setText((e as CustomEvent).detail); ref.current?.focus() }
    window.addEventListener('osa-fill', fill)
    return () => window.removeEventListener('osa-fill', fill)
  }, [])
  useEffect(() => {  // 自动增高
    const el = ref.current
    if (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 240) + 'px' }
  }, [text])

  const submit = () => {
    const t = text.trim()
    if (!t) return
    send(chatId, t)
    setText('')
  }

  return (
    <div className="composer-wrap">
      {matches.length > 0 && (
        <div className="mention-list">
          {matches.map((a, i) => (
            <button key={a.name} className={i === pick % matches.length ? 'on' : ''} onMouseDown={e => { e.preventDefault(); choose(a.name) }}>
              <b>@{a.name}</b><span className="muted small">{a.connected ? `${a.tools.length} 个工具` : '未连接'}</span>
            </button>))}
        </div>
      )}
      <div className="composer">
        <textarea ref={ref} rows={1} value={text} placeholder={`给 ${bot.name} 发消息`}
          onChange={e => setText(e.target.value)}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          onKeyDown={e => {
            if (matches.length && !composing.current) {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault(); setPick((pick + (e.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length); return
              }
              if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); choose(matches[pick % matches.length].name); return }
            }
            if (e.key === 'Enter' && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
          }} />
        <button className={'send' + (text.trim() ? ' on' : '')} onClick={submit} title="发送（Enter）" aria-label="发送">↑</button>
      </div>
      <div className="composer-hint">{busy ? `${bot.name} 正在回复…你可以继续发消息` : 'Enter 发送 · Shift+Enter 换行 · @ 指定应用'}</div>
    </div>
  )
}
