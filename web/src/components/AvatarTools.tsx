import { useEffect, useRef, useState } from 'react'
import { api } from '../store'
import { t } from '../i18n'
import type { Avatar } from '../types'

// 能画图的模型（GET /api/avatars/models）。price：每张多少钱（config.yaml 的 pricing.images），不知道是 null
export type ImageModel = { provider: string; title: string; model: string; price: number | null }
export const imagePrice = (p: number | null | undefined, cur = '¥') =>
  p === null || p === undefined ? t('价格未知') : p === 0 ? t('免费') : t('{price} / 张', { price: cur + p })

// 把一张图（比如对话里画的）设为 Bot 或群聊的头像：裁成 256 像素的正方形存成头像，再改 Bot / 群聊
export async function setAsAvatar(src: string, target: { bot?: { id: number; avatar: Avatar }; chat?: number }) {
  const url = await store(await squareImage(src))
  if (target.bot) await api(`/api/bots/${target.bot.id}`, { method: 'PATCH', body: { avatar: { ...target.bot.avatar, image: url } } })
  else if (target.chat) await api(`/api/chats/${target.chat}`, { method: 'PATCH', body: { image: url } })
}

// 图片取中间的正方形、缩到 256 像素，转成 data URL。不支持编码 WebP 的浏览器（Safari）用 PNG
export async function squareImage(src: string, size = 256): Promise<string> {
  const img = new Image()
  img.src = src
  try { await img.decode() } catch { throw new Error(t('这张图片打不开，换一张 PNG 或 JPG 试试')) }
  const s = Math.min(img.naturalWidth, img.naturalHeight)
  const c = document.createElement('canvas')
  c.width = c.height = size
  const ctx = c.getContext('2d')!
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, size, size)
  const webp = c.toDataURL('image/webp', 0.9)
  return webp.startsWith('data:image/webp') ? webp : c.toDataURL('image/png')
}

// 传上去，拿到头像地址（/avatars/…）
const store = async (data: string): Promise<string> => (await api('/api/avatars', { method: 'POST', body: { data } })).image

const STYLE = () => t('扁平插画风格，正脸，居中，背景是干净的纯色，画面里不要有文字。')
export const botPrompt = (name: string, about = '') => {
  const what = about.trim().replace(/\s+/g, ' ').slice(0, 80)
  return t('给 AI 助手「{name}」画一个可爱的角色头像，头部特写。', { name: name.trim() || t('新 Bot') }) + (what ? t('它负责：{what}。', { what }) : '') + STYLE()
}
export const groupPrompt = (title: string, names: string[]) =>
  t('给群聊「{name}」画一个头像，群里有 {members}，画几个可爱的角色在一起。', { name: title, members: names.join(t('、')) }) + STYLE()

// 上传图片、AI 生成。选好后 onImage 拿到头像地址；保存不保存由外面决定
export function AvatarImageTools({ prompt, onImage }: { prompt: string; onImage: (url: string) => void }) {
  const file = useRef<HTMLInputElement>(null)
  const [ai, setAi] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [err, setErr] = useState('')
  const pick = async (f: File | undefined) => {
    if (!f) return
    setErr(''); setUploading(true)
    const url = URL.createObjectURL(f)
    try { onImage(await store(await squareImage(url))) } catch (e: any) { setErr(e.message) }
    finally { URL.revokeObjectURL(url); setUploading(false) }
  }
  return (
    <div className="avatar-tools">
      <div className="avatar-tool-row">
        <button className="btn" disabled={uploading} onClick={() => file.current?.click()}>{uploading ? t('上传中…') : t('上传图片')}</button>
        <button className={'btn' + (ai ? ' on' : '')} onClick={() => setAi(!ai)}>✦ {t('AI 生成')}</button>
        <input ref={file} type="file" accept="image/*" hidden onChange={e => { pick(e.target.files?.[0]); e.target.value = '' }} />
      </div>
      {ai && <AiAvatar prompt={prompt} onImage={onImage} />}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function AiAvatar({ prompt: initial, onImage }: { prompt: string; onImage: (url: string) => void }) {
  const [prompt, setPrompt] = useState(initial)
  const [models, setModels] = useState<ImageModel[] | null>(null)
  const [pick, setPick] = useState('')
  const [result, setResult] = useState('')   // 画好、裁好的图，还没用上
  const [busy, setBusy] = useState<'' | 'draw' | 'use'>('')
  const [err, setErr] = useState('')
  const key = (m: ImageModel) => m.provider + '\n' + m.model

  useEffect(() => {
    api('/api/avatars/models').then(r => { setModels(r.models); if (r.default) setPick(key(r.default)) })
      .catch(e => { setModels([]); setErr(e.message) })
  }, [])

  const draw = async () => {
    const m = models?.find(x => key(x) === pick)
    if (!m) return
    setBusy('draw'); setErr('')
    try {
      const r = await api('/api/avatars/generate', { method: 'POST', body: { prompt, provider: m.provider, model: m.model } })
      setResult(await squareImage(r.data))
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const use = async () => {
    setBusy('use'); setErr('')
    try { onImage(await store(result)); setResult('') } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }

  if (models === null) return <div className="ai-avatar muted small">{t('正在找能画图的模型…')}</div>
  if (!models.length) return (
    <div className="ai-avatar muted small">
      {t('还没有能画图的模型。在“设置 → 模型”里添加硅基流动、OpenAI、智谱这类有画图模型的服务，再回来生成。')}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
  return (
    <div className="ai-avatar">
      <textarea rows={3} value={prompt} maxLength={1000} onChange={e => setPrompt(e.target.value)} placeholder={t('想要什么样的头像')} />
      <div className="ai-row">
        <select value={pick} onChange={e => setPick(e.target.value)} title={t('用哪个模型画')}>
          {models.map(m => <option key={key(m)} value={key(m)}>{m.model} · {m.title} · {imagePrice(m.price)}</option>)}
        </select>
        <button className={'btn' + (result ? '' : ' primary')} disabled={!!busy || !prompt.trim()} onClick={draw}>
          {busy === 'draw' ? t('正在画…') : result ? t('再画一张') : t('生成')}</button>
      </div>
      {result && (
        <div className="ai-result">
          <img src={result} width={88} height={88} alt="" />
          <button className="btn primary" disabled={!!busy} onClick={use}>{busy === 'use' ? t('保存中…') : t('用这张')}</button>
        </div>
      )}
      {err && <div className="error-line">{err}</div>}
      <div className="muted small">{t('由你选的模型服务来画，按它的价格收费。')}</div>
    </div>
  )
}
