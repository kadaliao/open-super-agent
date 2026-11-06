import { useEffect, useState } from 'react'
import { api, useStore } from '../store'
import type { Channel, ChannelKind } from '../types'

// 设置 → 聊天软件：在 Telegram、飞书、钉钉、企业微信、Discord 里和 Bot 对话
const STATE: Record<string, [string, string]> = {
  connected: ['已连接', 'ok'], connecting: ['连接中…', ''], error: ['出错了', 'bad'], off: ['已停用', ''],
}
const hhmm = (t: number) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })

function useTargets() {
  const bots = useStore(s => s.bots)
  const chats = useStore(s => s.chats)
  return [
    ...bots.filter(b => b.chat_id).map(b => ({ id: b.chat_id!, label: b.name + (b.is_primary ? '（主 Bot）' : '') })),
    ...chats.filter(c => c.kind === 'group').map(c => ({ id: c.id, label: `群聊：${c.title}` })),
  ]
}

export default function Channels() {
  const [list, setList] = useState<Channel[] | null>(null)
  const [url, setUrl] = useState('')
  const [saved, setSaved] = useState('')
  const [err, setErr] = useState('')
  const load = () => api('/api/channels').then(r => { setList(r.channels); setUrl(u => u || r.public_url); setSaved(r.public_url) })
  useEffect(() => {
    load()
    const f = () => load()
    window.addEventListener('osa-channels', f)
    return () => window.removeEventListener('osa-channels', f)
  }, [])
  const saveUrl = async () => {
    setErr('')
    try { const r = await api('/api/channels/settings', { method: 'PUT', body: { public_url: url } }); setSaved(r.public_url); setUrl(r.public_url) }
    catch (e: any) { setErr(e.message) }
  }
  if (!list) return <div className="muted">加载中…</div>
  return (
    <div>
      <p className="muted">在 Telegram、飞书、钉钉、企业微信、Discord 里直接和 Bot 说话，和网页上是同一个对话。Bot 的回复、任务结果会发过去；需要你确认的操作变成消息里的按钮。
        收消息走长连接，服务器不需要公网地址。</p>
      {list.map(c => <ChannelCard key={c.id} c={c} onChange={load} />)}
      <AddChannel first={!list.length} onAdded={load} />
      <h4>网页地址</h4>
      <p className="muted small">消息里的页面链接、“去网页接手”按钮会用这个地址。手机上要能打开，最好是 https。</p>
      <div className="env-row">
        <input value={url} onChange={e => setUrl(e.target.value)} placeholder={location.origin} />
        <button className="btn" disabled={url === saved} onClick={saveUrl}>保存</button>
      </div>
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function ChannelCard({ c, onChange }: { c: Channel; onChange: () => void }) {
  const targets = useTargets()
  const [err, setErr] = useState('')
  const act = async (fn: () => Promise<any>) => {
    setErr('')
    try { await fn(); onChange() } catch (e: any) { setErr(e.message) }
  }
  const patch = (body: any) => act(() => api(`/api/channels/${c.id}`, { method: 'PATCH', body }))
  const [label, cls] = STATE[c.state] || [c.state, '']
  const meta = KINDS[c.kind]
  return (
    <div className="ch-card">
      <div className="ch-head">
        <span className={'ch-badge ' + c.kind}>{c.title}</span>
        <b>{c.name}</b>
        <span className={'pill ' + cls}>{label}</span>
        <span className="grow" />
        <button className="ghost-btn" onClick={() => patch({ enabled: !c.enabled })}>{c.enabled ? '停用' : '启用'}</button>
        <button className="ghost-btn" onClick={() => confirm(`删除 ${c.title} 连接 ${c.name}？聊天软件里的机器人账号本身不会被删除。`) &&
          act(() => api(`/api/channels/${c.id}`, { method: 'DELETE' }))}>删除</button>
      </div>
      {c.error && <div className="error-line">{c.error}</div>}
      <label className="field inline">和谁对话
        <select value={c.chat_id} onChange={e => patch({ chat_id: Number(e.target.value) })}>
          {targets.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
          {!targets.some(t => t.id === c.chat_id) && <option value={c.chat_id}>已删除的对话</option>}
        </select>
      </label>
      {c.paired ? (
        <div className="ch-pair-done muted small">
          已和{c.peer_name ? `「${c.peer_name}」` : '你的账号'}配对，只有这个私聊能使用它。
          <button className="link-btn" onClick={() => confirm('重新配对后，原来的私聊就不能再用了。继续？') &&
            act(() => api(`/api/channels/${c.id}/pair`, { method: 'POST' }))}>重新配对</button>
        </div>
      ) : c.pair_code ? (
        <div className="ch-pair">
          <div>{meta.pairHint}</div>
          <div className="ch-code">{c.pair_code}</div>
          <div className="row-actions">
            {c.setup_link && <a className="btn" href={c.setup_link} target="_blank" rel="noreferrer">{meta.setup} ↗</a>}
            {c.pair_link && <a className="btn primary" href={c.pair_link} target="_blank" rel="noreferrer">{meta.open} ↗</a>}
            <span className="muted small">{hhmm(c.pair_expires!)} 前有效，配对后只有你能用</span>
          </div>
        </div>
      ) : (
        <div className="ch-pair">
          <div className="muted small">配对码过期了（或输错太多次）。</div>
          <button className="btn small-btn" onClick={() => act(() => api(`/api/channels/${c.id}/pair`, { method: 'POST' }))}>生成新的配对码</button>
        </div>
      )}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

const ext = (href: string, text: string) => <a href={href} target="_blank" rel="noreferrer">{text}</a>

// 每个聊天软件：要填的项、步骤、配对时的说明和按钮
type Field = { key: string; label: string; placeholder?: string; secret?: boolean; optional?: boolean }
type Kind = {
  label: string; fields: Field[]; steps: React.ReactNode[]; note: string
  pairHint: string; open?: string; setup?: string; variants?: [string, string][]
}
const KINDS: Record<ChannelKind, Kind> = {
  telegram: {
    label: 'Telegram',
    fields: [{ key: 'token', label: 'Bot token', placeholder: '123456789:AAH…', secret: true }],
    steps: [
      <>在 Telegram 里打开 {ext('https://t.me/BotFather', '@BotFather')}，发送 <code>/newbot</code>，按提示起名字</>,
      <>把它回复里的 token（像 <code>123456789:AAH…</code>）贴到下面，点连接</>,
      <>连上后会给你一个配对码，在 Telegram 里发给你的机器人就能用了</>,
    ],
    note: '服务器在国内的话，要设置 HTTPS_PROXY 才连得上 Telegram。',
    pairHint: '还差一步：在 Telegram 里打开这个机器人，把配对码发给它。', open: '在 Telegram 里打开（自动带上配对码）',
  },
  feishu: {
    label: '飞书 / Lark',
    variants: [['feishu', '飞书'], ['lark', 'Lark（海外版）']],
    fields: [{ key: 'app_id', label: 'App ID', placeholder: 'cli_…' }, { key: 'app_secret', label: 'App Secret', secret: true }],
    steps: [
      <>打开 {ext('https://open.feishu.cn/app', '飞书开发者后台')}，创建<b>企业自建应用</b>，在“添加应用能力”里添加<b>机器人</b></>,
      <>“权限管理”里开通：读取用户发给机器人的单聊消息、以应用的身份发消息、获取与上传图片或文件资源（可选：发送、删除消息表情回复，用来显示“正在输入”）</>,
      <>把“凭证与基础信息”里的 App ID 和 App Secret 填到下面，点连接</>,
      <>连上后回到后台的“事件与回调”：事件配置选<b>使用长连接接收事件</b>，添加事件“接收消息”；回调配置也选长连接，添加“卡片回传交互”</>,
      <>“版本管理与发布”里创建版本并发布，然后在飞书里打开机器人，发送配对码</>,
    ],
    note: '密钥加密存在你自己的服务器上。',
    pairHint: '还差一步：在飞书里打开这个机器人，把配对码发给它。', open: '在飞书里打开机器人',
  },
  dingtalk: {
    label: '钉钉',
    fields: [
      { key: 'app_key', label: 'Client ID（AppKey）', placeholder: 'ding…' },
      { key: 'app_secret', label: 'Client Secret（AppSecret）', secret: true },
      { key: 'name', label: '名字（可选，只在这里显示）', placeholder: '比如：工作助理', optional: true },
    ],
    steps: [
      <>打开 {ext('https://open-dev.dingtalk.com', '钉钉开发者后台')}，在“应用开发”里创建一个<b>企业内部应用</b></>,
      <>“添加应用能力”里添加<b>机器人</b>，机器人配置里的消息接收模式选 <b>Stream 模式</b></>,
      <>“权限管理”里搜索“机器人”，开通<b>企业内机器人发送消息权限</b>（任务做完主动发结果要用）</>,
      <>把“凭证与基础信息”里的 Client ID 和 Client Secret 填到下面，点连接</>,
      <>“版本管理与发布”里发布应用；在钉钉里搜索机器人的名字，打开单聊，发送配对码</>,
    ],
    note: '钉钉的卡片按钮要另建模板，确认请求改成点链接（或回复“允许 #编号”）。',
    pairHint: '还差一步：在钉钉里搜索这个机器人，打开单聊，把配对码发给它。',
  },
  wecom: {
    label: '企业微信',
    fields: [
      { key: 'bot_id', label: 'Bot ID' },
      { key: 'secret', label: 'Secret', secret: true },
      { key: 'name', label: '名字（可选，只在这里显示）', placeholder: '比如：工作助理', optional: true },
    ],
    steps: [
      <>在企业微信里创建一个<b>智能机器人</b>（管理后台或工作台里的“智能机器人”）</>,
      <>打开机器人的配置页面，开启 <b>API 模式</b>，连接方式选<b>长连接</b>，拿到 Bot ID 和 Secret</>,
      <>填到下面，点连接</>,
      <>在企业微信里找到这个机器人，发消息给它（单聊），把配对码发过去</>,
    ],
    note: '同一个机器人只能连一处，改成回调模式后这里就断了。',
    pairHint: '还差一步：在企业微信里找到这个机器人，单聊发配对码给它。',
  },
  discord: {
    label: 'Discord',
    fields: [{ key: 'token', label: 'Bot token', secret: true }],
    steps: [
      <>打开 {ext('https://discord.com/developers/applications', 'Discord Developer Portal')}，点 New Application 建一个应用</>,
      <>左边的 <b>Bot</b> 页面点 Reset Token，把 token 贴到下面，点连接（不用打开任何 Privileged Gateway Intents）</>,
      <>连上后点“把机器人加到你的服务器”，选一个你自己的服务器（没有就先建一个）。Discord 只让同一个服务器里的人私信机器人</>,
      <>点“打开机器人”，给它发私信，把配对码发过去</>,
    ],
    note: '服务器在国内的话，要设置 HTTPS_PROXY 才连得上 Discord。',
    pairHint: '还差两步：先把机器人加到你自己的服务器，再私信它配对码。',
    setup: '① 把机器人加到你的服务器', open: '② 打开机器人，发私信',
  },
}
const ORDER: ChannelKind[] = ['telegram', 'feishu', 'dingtalk', 'wecom', 'discord']

function AddChannel({ first, onAdded }: { first: boolean; onAdded: () => void }) {
  const targets = useTargets()
  const [open, setOpen] = useState(first)
  const [kind, setKind] = useState<ChannelKind>('telegram')
  const [fields, setFields] = useState<Record<string, string>>({ domain: 'feishu' })
  const [chat, setChat] = useState<number>(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => setOpen(o => o || first), [first])
  const meta = KINDS[kind]
  const target = chat || targets[0]?.id
  const ready = meta.fields.every(f => f.optional || fields[f.key]?.trim())
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setErr('')
    try {
      await api('/api/channels', { method: 'POST', body: { kind, fields, chat_id: target, public_url: location.origin } })
      setFields({ domain: 'feishu' }); setOpen(false); onAdded()
    } catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  if (!open) return <button className="btn" onClick={() => setOpen(true)}>＋ 连接聊天软件</button>
  return (
    <form className="ch-add" onSubmit={submit}>
      <h4>连接聊天软件</h4>
      <div className="seg">
        {ORDER.map(k => <button type="button" key={k} className={kind === k ? 'on' : ''}
          onClick={() => { setKind(k); setErr('') }}>{KINDS[k].label}</button>)}
      </div>
      <ol className="m-steps">{meta.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
      {meta.variants && <div className="seg">
        {meta.variants.map(([k, v]) => <button type="button" key={k}
          className={fields.domain === k ? 'on' : ''} onClick={() => setFields({ ...fields, domain: k })}>{v}</button>)}
      </div>}
      {meta.fields.map(f => (
        <label className="field" key={kind + f.key}>{f.label}
          <input type={f.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false} value={fields[f.key] || ''}
            placeholder={f.placeholder || ''} onChange={e => setFields({ ...fields, [f.key]: e.target.value })} />
        </label>))}
      <label className="field">和谁对话
        <select value={target} onChange={e => setChat(Number(e.target.value))}>
          {targets.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
      </label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" type="submit" disabled={!ready || busy}>{busy ? '连接中…' : '连接'}</button>
        {!first && <button className="btn" type="button" onClick={() => setOpen(false)}>取消</button>}
        <span className="muted small">{meta.note}</span>
      </div>
    </form>
  )
}
