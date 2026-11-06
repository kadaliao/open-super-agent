import { useEffect, useRef, useState } from 'react'
import { loadSkills, send, useStore } from '../store'
import type { Bot } from '../types'
import { locale, t, tc } from '../i18n'

// 浏览器自带的语音识别：Safari（含 iPhone）直接能用；Chrome 要能连上 Google 的语音服务。不支持的浏览器不显示麦克风
const Speech: any = typeof window !== 'undefined' && ((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition)
const SPEECH_ERR: Record<string, string> = {
  'not-allowed': t('没有麦克风权限，请在浏览器设置里允许'), 'service-not-allowed': t('没有麦克风权限，请在浏览器设置里允许'),
  network: t('语音识别连不上服务（Chrome 要能访问 Google 的语音服务，Safari 可以直接用）'), 'no-speech': t('没听到声音'),
  'audio-capture': t('找不到麦克风'),
}
// @所有人：英文界面插入 @everyone，服务端两个都认
const ALL = t('所有人')

// bot：私聊的 Bot；群聊时为空，members 是群成员。busy：正在回复的 Bot 名字
export default function Composer({ chatId, bot, members, busy }: { chatId: number; bot?: Bot; members: Bot[]; busy: string }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  const apps = useStore(s => s.apps)
  const catalog = useStore(s => s.catalog)
  const skills = useStore(s => (bot ? s.skills[bot.id] : undefined))
  const group = !bot
  const [pick, setPick] = useState(0)
  const [uploading, setUploading] = useState('')
  const [upErr, setUpErr] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  useEffect(() => { if (bot && !skills) loadSkills(bot.id) }, [bot?.id, skills])
  // 光标前面是 @xxx 时弹出成员（群聊）和应用；私聊开头是 /xxx 时弹出技能列表
  const at = text.match(/(?:^|\s)@([^\s@]*)$/)
  const slash = group ? null : text.match(/^\/([\w\u4e00-\u9fff-]*)$/)
  type Item = { key: string; label: string; hint: string }
  const q = at ? at[1].toLowerCase() : ''
  const matches: Item[] = at
    ? [...(group ? [
        ...members.filter(b => b.name.toLowerCase().startsWith(q)).map(b => ({ key: b.name, label: '@' + b.name, hint: 'Bot' })),
        ...('所有人'.startsWith(q) || 'everyone'.startsWith(q) ? [{ key: ALL, label: '@' + ALL, hint: t('每个 Bot 都回答') }] : []),   // i18n-ignore：两种写法都能匹配
      ] : []),
      ...apps.map(a => ({ a, title: catalog.find(c => c.id === (a.catalog || a.name))?.title || tc('app', '应用') }))
        .filter(({ a, title }) => a.name.startsWith(q) || (q && title.toLowerCase().includes(q)))
        .map(({ a, title }) => ({ key: a.name, label: '@' + a.name, hint: title + ' · ' + (a.connected ? t('{n} 个工具', { n: a.tools.length }) : t('未连接')) }))]
    : slash
      ? (skills || []).filter(k => k.name.toLowerCase().startsWith(slash[1].toLowerCase()))
          .map(k => ({ key: k.name, label: '/' + k.name, hint: k.description || k.body.slice(0, 30) }))
      : []
  const choose = (name: string) => {
    setText(at ? text.replace(/@([^\s@]*)$/, `@${name} `) : `/${name} `); setPick(0); ref.current?.focus()
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
      setUploading(t('正在上传 {name}…（第一次会先开机）', { name: f.name }))
      try {
        const r = await fetch(`/api/files/content?dir=${encodeURIComponent('/data/workspace/uploads')}&name=${encodeURIComponent(f.name)}`, { method: 'PUT', body: f })
        const d = await r.json().catch(() => ({}))
        if (!r.ok) throw new Error(d.detail || t('上传失败（{status}）', { status: r.status }))
        setText(s => (s && !s.endsWith('\n') ? s + '\n' : s) + `[附件] ${d.path}\n`)   // i18n-ignore：消息里的附件标记，Bot 靠它找文件
      } catch (e: any) { setUpErr(e.message) }
    }
    setUploading(''); ref.current?.focus()
  }

  // 语音输入：说的话接在已经打的字后面，识别中的部分实时显示，停下来后可以改了再发
  const [listening, setListening] = useState(false)
  const rec = useRef<any>(null)
  const toggleMic = () => {
    if (listening) { rec.current?.stop(); return }
    const r = new Speech()
    r.lang = locale; r.interimResults = true; r.continuous = true
    const base = text && !/\s$/.test(text) ? text + ' ' : text
    let done = ''
    r.onresult = (e: any) => {
      let partial = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) done += e.results[i][0].transcript
        else partial += e.results[i][0].transcript
      }
      setText(base + done + partial)
    }
    r.onerror = (e: any) => setUpErr(SPEECH_ERR[e.error] || t('语音识别出错：{error}', { error: e.error }))
    r.onend = () => { setListening(false); rec.current = null; ref.current?.focus() }
    setUpErr('')
    try { r.start(); rec.current = r; setListening(true) } catch (e: any) { setUpErr(t('语音识别没能启动：{error}', { error: e.message })) }
  }
  useEffect(() => () => rec.current?.abort(), [chatId])

  const submit = () => {
    const msg = text.trim()
    if (!msg) return
    rec.current?.abort()
    send(chatId, msg)
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
        <button className="attach" title={t('上传附件（存到电脑的 uploads 目录）')} disabled={!!uploading} onClick={() => fileInput.current?.click()}>＋</button>
        <input ref={fileInput} type="file" multiple hidden onChange={e => { attach(e.target.files); e.target.value = '' }} />
        <textarea ref={ref} rows={1} value={text} placeholder={group ? t('发到群里，@ 指定 Bot') : t('给 {name} 发消息', { name: bot!.name })}
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
        {Speech && <button className={'mic' + (listening ? ' on' : '')} onClick={toggleMic} title={listening ? t('停止语音输入') : t('语音输入')}
          aria-label={t('语音输入')}>{listening ? '■' : '🎙'}</button>}
        <button className={'send' + (text.trim() ? ' on' : '')} onClick={submit} title={t('发送（Enter）')} aria-label={t('发送')}>↑</button>
      </div>
      {listening && <div className="composer-hint">{t('正在听…说完点 ■ 停止，确认文字后再发送')}</div>}
      {(uploading || upErr) && <div className={'composer-hint' + (upErr ? ' bad' : '')}>{upErr || uploading}</div>}
      <div className="composer-hint">{busy ? t('{name} 正在回复…你可以继续发消息', { name: busy })
        : group ? t('Enter 发送 · Shift+Enter 换行 · @ 指定 Bot 或应用 · @所有人') : t('Enter 发送 · Shift+Enter 换行 · @ 指定应用 · / 调用技能')}</div>
    </div>
  )
}
