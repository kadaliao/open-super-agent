import { useEffect, useRef, useState } from 'react'
import { loadSkills, send, useStore } from '../store'
import type { Attachment, Bot } from '../types'
import { locale, t, tc } from '../i18n'
import { desktop } from '../desktop'
import { shrinkImage, uploadMedia } from './Media'
import { isEnter } from '../keys'

type Pending = Attachment & { key: string; busy: boolean; preview?: string }   // 输入框上面还没发出去的附件

// 浏览器自带的语音识别：Safari（含 iPhone）直接能用；Chrome 要能连上 Google 的语音服务。不支持的浏览器不显示麦克风。
// 桌面 App 里外壳说能用才显示
const Speech: any = typeof window !== 'undefined' && (!desktop || desktop.speech) &&
  ((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition)
const SPEECH_ERR: Record<string, string> = {
  'not-allowed': t('没有麦克风权限，请在浏览器设置里允许'), 'service-not-allowed': t('没有麦克风权限，请在浏览器设置里允许'),
  network: t('语音识别连不上服务（Chrome 要能访问 Google 的语音服务，Safari 可以直接用）'), 'no-speech': t('没听到声音'),
  'audio-capture': t('找不到麦克风'),
}
// @所有人：英文界面插入 @everyone，服务端两个都认
const ALL = t('所有人')

// bot：私聊的 Bot；群聊时为空，members 是群成员。busy：正在回复的 Bot 名字。
// inThread：讨论串里的输入框，发消息走 onSend；拖进窗口的文件、“填进输入框”的事件归主对话的输入框管
export default function Composer({ chatId, bot, members, busy, onSend, inThread }: {
  chatId: number; bot?: Bot; members: Bot[]; busy: string; onSend?: (text: string, attachments: Attachment[]) => void; inThread?: boolean
}) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)
  const composing = useRef(false)
  const apps = useStore(s => s.apps)
  const catalog = useStore(s => s.catalog)
  const skills = useStore(s => (bot ? s.skills[bot.id] : undefined))
  const group = !bot
  const [pick, setPick] = useState(0)
  const [atts, setAtts] = useState<Pending[]>([])
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

  // 切换对话、加完附件后把光标放进输入框：只在用鼠标、触控板的设备上。手机上一 focus 就弹键盘，挡住半个对话
  const focusIfPointer = () => { if (matchMedia('(pointer: fine)').matches) ref.current?.focus() }
  useEffect(focusIfPointer, [chatId])
  useEffect(() => {
    if (inThread) return
    const fill = (e: Event) => { setText((e as CustomEvent).detail); ref.current?.focus() }
    window.addEventListener('osa-fill', fill)
    return () => window.removeEventListener('osa-fill', fill)
  }, [])
  useEffect(() => {  // 自动增高
    const el = ref.current
    if (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 240) + 'px' }
  }, [text])

  // 附件：原文件传到共用电脑的 uploads 目录（Bot 派任务时靠路径处理它）；图片另外缩小后存一份到服务端，
  // 对话里显示缩略图，对话模型能看图时直接给它看。发出去时服务端在正文后面接上“[附件] 路径”
  const putFile = async (f: File) => {
    const r = await fetch(`/api/files/content?dir=uploads&name=${encodeURIComponent(f.name)}`, { method: 'PUT', body: f })
    const d = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(d.detail || t('上传失败（{status}）', { status: r.status }))
    return d as { path: string; name: string; size: number }
  }
  const attachOne = async (f: File) => {
    const key = `${Date.now()}-${Math.random()}`
    const img = f.type.startsWith('image/') && f.type !== 'image/svg+xml'
    setAtts(a => [...a, { key, name: f.name, size: f.size, busy: true, preview: img ? URL.createObjectURL(f) : undefined }])
    const [m, p] = await Promise.allSettled([img ? shrinkImage(f).then(b => uploadMedia(b, f.name)) : Promise.reject(null), putFile(f)])
    const patch: Partial<Pending> = { busy: false }
    if (p.status === 'fulfilled') Object.assign(patch, { path: p.value.path, name: p.value.name })
    if (m.status === 'fulfilled') Object.assign(patch, { media: m.value.id, mime: m.value.mime, w: m.value.w, h: m.value.h })
    const why = p.status === 'rejected' ? (p.reason as Error)?.message || '' : ''
    if (!patch.path && !patch.media) { drop(key); setUpErr(why); return }
    if (!patch.path) setUpErr(t('「{name}」没存到电脑里（{error}）：Bot 能看到图，但不能用它处理文件', { name: f.name, error: why }))
    setAtts(a => a.map(x => (x.key === key ? { ...x, ...patch } : x)))
  }
  const attach = async (files: FileList | null) => {
    setUpErr('')
    await Promise.all(Array.from(files || []).map(attachOne))
    focusIfPointer()
  }
  const drop = (key: string) => setAtts(a => {
    a.filter(x => x.key === key).forEach(x => x.preview && URL.revokeObjectURL(x.preview))
    return a.filter(x => x.key !== key)
  })
  const uploading = atts.find(a => a.busy)
  const attachRef = useRef(attach)
  attachRef.current = attach

  // 把文件拖到窗口里任何地方，都传给这个对话
  const [dragging, setDragging] = useState(false)
  useEffect(() => {
    if (inThread) return
    const files = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes('Files')
    let depth = 0   // dragenter / dragleave 在子元素之间也会触发，数一下进出才知道是不是真的离开了窗口
    const enter = (e: DragEvent) => { if (files(e)) { depth++; setDragging(true) } }
    const leave = (e: DragEvent) => { if (files(e) && --depth <= 0) { depth = 0; setDragging(false) } }
    const drop = (e: DragEvent) => {
      if (!files(e)) return
      e.preventDefault(); depth = 0; setDragging(false)
      attachRef.current(e.dataTransfer!.files)
    }
    window.addEventListener('dragenter', enter)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragenter', enter)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
    }
  }, [])
  // 粘贴截图：剪贴板里的图片都叫 image.png，换成带时间的名字，免得后一张把前一张覆盖掉
  const paste = (e: React.ClipboardEvent) => {
    const list = Array.from(e.clipboardData.files)
    if (!list.length) return
    e.preventDefault()
    const d = new Date(), pad = (n: number) => String(n).padStart(2, '0')
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
    const dt = new DataTransfer()
    list.forEach((f, i) => dt.items.add(f.name === 'image.png' || !f.name
      ? new File([f], `paste-${stamp}${list.length > 1 ? '-' + (i + 1) : ''}.${(f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`, { type: f.type })
      : f))
    attach(dt.files)
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
    if ((!msg && !atts.length) || uploading) return
    rec.current?.abort()
    const out: Attachment[] = atts.map(({ key: _k, busy: _b, preview, ...a }) => { if (preview) URL.revokeObjectURL(preview); return a })
    if (onSend) onSend(msg, out)
    else send(chatId, msg, out)
    setText(''); setAtts([]); setUpErr('')
  }
  const ready = (text.trim() || atts.length) && !uploading

  return (
    <div className="composer-wrap">
      {dragging && <div className="drop-overlay">{t('松开鼠标，上传到电脑的 uploads 目录')}</div>}
      {matches.length > 0 && (
        <div className="mention-list">
          {matches.map((a, i) => (
            <button key={a.key} className={i === pick % matches.length ? 'on' : ''} onMouseDown={e => { e.preventDefault(); choose(a.key) }}>
              <b>{a.label}</b><span className="muted small">{a.hint}</span>
            </button>))}
        </div>
      )}
      {atts.length > 0 && (
        <div className="composer-atts">
          {atts.map(a => (
            <div key={a.key} className={'c-att' + (a.preview ? ' img' : '') + (a.busy ? ' busy' : '')} title={a.path || a.name}>
              {a.preview ? <img src={a.preview} alt="" /> : <span className="c-att-name">{a.name}</span>}
              {a.busy && <span className="spin" />}
              <button className="c-att-x" onClick={() => drop(a.key)} title={t('移除')} aria-label={t('移除')}>✕</button>
            </div>))}
        </div>
      )}
      <div className="composer">
        <button className="attach" title={t('上传附件（存到电脑的 uploads 目录）')} onClick={() => fileInput.current?.click()}>＋</button>
        <input ref={fileInput} type="file" multiple hidden onChange={e => { attach(e.target.files); e.target.value = '' }} />
        <textarea ref={ref} rows={1} value={text} placeholder={inThread ? t('回复…') : group ? t('发到群里，@ 指定 Bot') : t('给 {name} 发消息', { name: bot!.name })}
          onChange={e => setText(e.target.value)}
          onPaste={paste}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          onKeyDown={e => {
            if (matches.length && !composing.current) {
              if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault(); setPick((pick + (e.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length); return
              }
              if (isEnter(e) || e.key === 'Tab') { e.preventDefault(); choose(matches[pick % matches.length].key); return }
            }
            if (isEnter(e) && !e.shiftKey && !composing.current) { e.preventDefault(); submit() }
          }} />
        {Speech && <button className={'mic' + (listening ? ' on' : '')} onClick={toggleMic} title={listening ? t('停止语音输入') : t('语音输入')}
          aria-label={t('语音输入')}>{listening ? '■' : '🎙'}</button>}
        <button className={'send' + (ready ? ' on' : '')} onClick={submit} title={t('发送（Enter）')} aria-label={t('发送')}>↑</button>
      </div>
      {listening && <div className="composer-hint">{t('正在听…说完点 ■ 停止，确认文字后再发送')}</div>}
      {(uploading || upErr) && <div className={'composer-hint' + (upErr ? ' bad' : '')}>{upErr || t('正在上传 {name}…（第一次会先开机）', { name: uploading!.name })}</div>}
      <div className={'composer-hint' + (busy ? '' : ' keys')}>{busy ? t('{name} 正在回复…你可以继续发消息', { name: busy })
        : inThread ? t('Enter 发送 · Shift+Enter 换行 · Esc 关闭') : group ? t('Enter 发送 · Shift+Enter 换行 · @ 指定 Bot 或应用 · @所有人') : t('Enter 发送 · Shift+Enter 换行 · @ 指定应用 · / 调用技能')}</div>
    </div>
  )
}
