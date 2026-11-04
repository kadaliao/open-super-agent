import { useEffect, useRef, useState } from 'react'
import { loadSkills, send, useStore } from '../store'
import type { Bot } from '../types'

export default function Composer({ chatId, bot, busy }: { chatId: number; bot: Bot; busy: boolean }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  const apps = useStore(s => s.apps)
  const skills = useStore(s => s.skills[bot.id])
  const [pick, setPick] = useState(0)
  const [uploading, setUploading] = useState('')
  const [upErr, setUpErr] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  useEffect(() => { if (!skills) loadSkills(bot.id) }, [bot.id, skills])
  // 光标前面是 @xxx 时弹出应用列表；开头是 /xxx 时弹出技能列表
  const at = text.match(/(?:^|\s)@([\w-]*)$/)
  const slash = text.match(/^\/([\w\u4e00-\u9fff-]*)$/)
  type Item = { key: string; label: string; hint: string }
  const matches: Item[] = at
    ? apps.filter(a => a.name.startsWith(at[1].toLowerCase()))
        .map(a => ({ key: a.name, label: '@' + a.name, hint: a.connected ? `${a.tools.length} 个工具` : '未连接' }))
    : slash
      ? (skills || []).filter(k => k.name.toLowerCase().startsWith(slash[1].toLowerCase()))
          .map(k => ({ key: k.name, label: '/' + k.name, hint: k.description || k.body.slice(0, 30) }))
      : []
  const choose = (name: string) => {
    setText(at ? text.replace(/@([\w-]*)$/, `@${name} `) : `/${name} `); setPick(0); ref.current?.focus()
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

  // 附件传到共用电脑的 uploads 目录，消息里写上路径，Bot 派任务时就能处理
  const attach = async (files: FileList | null) => {
    setUpErr('')
    for (const f of Array.from(files || [])) {
      setUploading(`正在上传 ${f.name}…（第一次会先开机）`)
      try {
        const r = await fetch(`/api/files/content?dir=${encodeURIComponent('/data/workspace/uploads')}&name=${encodeURIComponent(f.name)}`, { method: 'PUT', body: f })
        const d = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(d.detail || `上传失败（${r.status}）`)
        setText(t => (t && !t.endsWith('\n') ? t + '\n' : t) + `[附件] ${d.path}\n`)
      } catch (e: any) { setUpErr(e.message) }
    }
    setUploading(''); ref.current?.focus()
  }

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
            <button key={a.key} className={i === pick % matches.length ? 'on' : ''} onMouseDown={e => { e.preventDefault(); choose(a.key) }}>
              <b>{a.label}</b><span className="muted small">{a.hint}</span>
            </button>))}
        </div>
      )}
      <div className="composer">
        <button className="attach" title="上传附件（存到电脑的 uploads 目录）" disabled={!!uploading} onClick={() => fileInput.current?.click()}>＋</button>
        <input ref={fileInput} type="file" multiple hidden onChange={e => { attach(e.target.files); e.target.value = '' }} />
        <textarea ref={ref} rows={1} value={text} placeholder={`给 ${bot.name} 发消息`}
          onChange={e => setText(e.target.value)}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          onKeyDown={e => {
            if (matches.length && !composing.current) {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault(); setPick((pick + (e.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length); return
              }
              if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); choose(matches[pick % matches.length].key); return }
            }
            if (e.key === 'Enter' && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
          }} />
        <button className={'send' + (text.trim() ? ' on' : '')} onClick={submit} title="发送（Enter）" aria-label="发送">↑</button>
      </div>
      {(uploading || upErr) && <div className={'composer-hint' + (upErr ? ' bad' : '')}>{upErr || uploading}</div>}
      <div className="composer-hint">{busy ? `${bot.name} 正在回复…你可以继续发消息` : 'Enter 发送 · Shift+Enter 换行 · @ 指定应用 · / 调用技能'}</div>
    </div>
  )
}
