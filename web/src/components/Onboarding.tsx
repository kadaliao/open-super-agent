import { useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { api, openApps, openChat, refreshLists, setSettings, useStore } from '../store'
import { Icon } from '../icons'
import { lang, syncLang, t, tc, tn } from '../i18n'
import { BRANDS } from '../brandIcons'
import type { Bot, CatalogItem, Preset, Settings as S } from '../types'
import BotAvatar from './BotAvatar'
import { AvatarPicker } from './BotEditor'
import { botPrompt } from './AvatarTools'
import { BrandIcon } from './BrandIcon'
import { AppIcon, howOf, Sheet, StateTag } from './Market'
import { ListsCtx, ModelIcon, ModelPicker, ProviderBadge, ProviderForm, TestButton, useLists, useTest } from './ModelSettings'
import { money, ToastHost, useToast } from './settingsKit'
import { TemplateInstall, TemplatePicker, type TemplatePreview } from './Templates'

// 第一次打开时的引导：接上模型 → 你的 Bot → 连一个应用 → 可以开始了。
// 走完或跳过以后不再自动弹出（记在服务端，换台设备也不弹）；设置里可以再打开
const STEPS = [t('接上模型'), t('你的 Bot'), t('连一个应用')]
const PICKS = ['notion', 'flomo', 'dida', 'tencent-docs', 'train', 'exa']   // 推荐的应用：登录就能用，或者免配置
const CHANNELS = ['telegram', 'feishu', 'dingtalk', 'wecom', 'discord']
const SEP = lang === 'en' ? ', ' : '、'

export default function Onboarding({ onClose }: { onClose: () => void }) {
  const primary = useStore(s => s.bots.find(b => b.is_primary) || s.bots[0])
  const [step, setStep] = useState(0)
  const [bot, setBot] = useState<Bot | undefined>(undefined)       // 第二步选定的 Bot（没选就是主 Bot）
  const [tpl, setTpl] = useState<TemplatePreview | null>(null)     // 装了模板：它要用的应用排在推荐前面
  const cur = bot || primary
  const finish = async (then?: () => void) => {
    await api('/api/onboarding/done', { method: 'POST' }).catch(() => {})
    onClose()
    then?.()
  }
  const start = (text = '') => finish(() => {
    if (cur?.chat_id) openChat(cur.chat_id)
    if (text) setTimeout(() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: text })), 120)
  })
  return (
    <div className="modal-bg onb-bg">
      <div className="modal onb">
        <ToastHost>
          <header className="onb-head">
            <span className="onb-logo">◆</span>
            <ol className="onb-steps">
              {STEPS.map((name, i) => (
                <li key={name} className={i === step ? 'on' : i < step ? 'done' : ''}>
                  <i>{i < step ? <Icon name="check" size={12} /> : i + 1}</i><span>{name}</span>
                </li>))}
            </ol>
            <span className="grow" />
            {step === 0 && <LangSwitch />}
            {step < 3 && <button className="link-btn small onb-skip" onClick={() => finish()}>{t('跳过引导')}</button>}
          </header>
          <div className="onb-body">
            {step === 0 && <ModelStep onNext={() => setStep(1)} />}
            {step === 1 && cur && <BotStep key={cur.id} bot={cur} onBack={() => setStep(0)}
              onNext={(b, tp) => { setBot(b); if (tp) setTpl(tp); setStep(2) }} />}
            {step === 2 && <AppStep tpl={tpl} onBack={() => setStep(1)} onNext={() => setStep(3)} onMarket={() => finish(openApps)} />}
            {step === 3 && cur && <Done bot={cur} onStart={start} onModel={() => setStep(0)}
              onChannels={() => finish(() => window.dispatchEvent(new CustomEvent('osa-open-settings', { detail: 'channels' })))} />}
          </div>
        </ToastHost>
      </div>
    </div>
  )
}

function Foot({ children }: { children: ReactNode }) {
  return <div className="onb-foot">{children}</div>
}

// 换界面语言：按钮上写另一种语言的名字。换完页面会重新加载，引导没走完会自己再打开（回到第一步）
function LangSwitch() {
  const toast = useToast()
  const other = lang === 'zh' ? 'en' : 'zh'
  const change = () => api('/api/settings', { method: 'PUT', body: { language: other } })
    .then(r => syncLang(r.language)).catch(e => toast(e.message, true))
  return <button className="ghost-btn" title={t('界面语言')} onClick={change}><Icon name="globe" size={13} /> {other === 'en' ? 'English' : '中文'}</button>   // i18n-ignore：语言自己的名字
}

// ---------- 第一步：接上模型 ----------
function ModelStep({ onNext }: { onNext: () => void }) {
  const s = useStore(x => x.settings) as S
  const r = s.roles.router
  const prov = s.providers.find(p => p.name === r.provider)
  const [mode, setMode] = useState<'ok' | 'pick' | 'form' | 'model'>(s.problem ? 'pick' : 'ok')
  const [form, setForm] = useState<{ name: string | null; preset: string }>({ name: null, preset: '' })
  const [chosen, setChosen] = useState('')   // 刚接上的服务
  const pick = (p: Preset) => {
    // 已经有这家的（比如 config.yaml 里写了 DeepSeek，只差 API Key）：直接改它，不再加一个
    const have = p.id !== 'custom' ? s.providers.find(x => x.preset === p.id) : undefined
    setForm({ name: have?.name || null, preset: p.id }); setMode('form')
  }

  if (mode === 'form') return (
    <ProviderForm key={form.name || form.preset} name={form.name} presetId={form.preset} backLabel={t('换一个服务商')} onBack={() => setMode('pick')}
      onDone={name => { if (name) { setChosen(name); setMode('model') } else setMode('pick') }} />
  )
  if (mode === 'model') return (
    <ModelChoose name={chosen} onDone={onNext}
      onBack={() => { setForm({ name: chosen, preset: s.providers.find(p => p.name === chosen)?.preset || 'custom' }); setMode('form') }} />
  )
  if (mode === 'ok') return <>
    <h2>{t('欢迎使用 Super Agent')}</h2>
    <p className="muted">{t('Bot 靠大模型思考和干活。模型已经接好了，下面两步建好你的 Bot、连上常用的应用就能开始。')}</p>
    <div className="onb-card">
      <ModelIcon prov={prov} model={r.model} size={40} />
      <span className="onb-card-text"><b>{r.model}</b><span>{prov?.title} · {t('对话和后台任务都用它')}</span></span>
      <span className="chip ok"><Icon name="check" size={13} />{t('能用')}</span>
    </div>
    <p className="muted small">{t('想换成别的服务（OpenAI、通义千问、Kimi……），点“换一个”。以后在 设置 → 模型 里也能改。')}</p>
    <Foot><button className="btn" onClick={() => setMode('pick')}>{t('换一个')}</button><span className="grow" />
      <button className="btn primary" onClick={onNext}>{t('下一步')}</button></Foot>
  </>
  return <>
    <h2>{s.problem ? t('欢迎！先接上一个大模型') : t('换一个模型服务')}</h2>
    <p className="muted">{t('Bot 靠大模型思考和干活。选一个你有账号的服务，填上它的 API Key 就行，密钥加密存在你自己的服务器上。')} {t('拿不准就选 DeepSeek：便宜，中文好。')}</p>
    <div className="preset-cards">
      {s.presets.map(p => (
        <button key={p.id} className="preset-card" onClick={() => pick(p)}>
          <ProviderBadge preset={p.id} title={p.title} size={36} />
          <span><b>{p.title}</b><span>{p.desc}</span></span>
        </button>))}
    </div>
    <Foot>
      {!s.problem && <button className="btn" onClick={() => setMode('ok')}>{t('返回')}</button>}
      <span className="grow" />
      {s.problem && <button className="link-btn small" onClick={onNext}>{t('先跳过这一步（Bot 暂时没法回复）')}</button>}
    </Foot>
  </>
}

// 刚接上的服务：选一个模型，对话、后台任务、汇总、整理都先用它
function ModelChoose({ name, onBack, onDone }: { name: string; onBack: () => void; onDone: () => void }) {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const prov = s.providers.find(p => p.name === name)
  const preset = s.presets.find(p => p.id === prov?.preset)
  const ctx = useLists(s.providers.filter(p => p.name === name))
  const l = ctx.lists[name]
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const test = useTest(() => ({ provider: name, model, role: 'router' }), [model])
  // 模型列表拉回来后先选一个：原来就在用这家的就用原来的，不然用推荐的
  useEffect(() => {
    if (!l || model) return
    const ids = new Set(l.items.map(m => m.id))
    const r = s.roles.router
    // 自己搭的转发（“其他”）列出了别家推荐的模型，也用上
    const rec = [...(preset?.models || []), ...s.presets.flatMap(p => p.models || [])].find(m => ids.has(m))
    setModel(r.provider === name && r.model ? r.model : rec || '')
  }, [l])
  const item = l?.items.find(m => m.id === model)
  const vision = test.res?.ok ? test.res.vision : item?.vision === undefined ? undefined : item.vision ? 'yes' : 'no'
  const cur = s.prices.currency
  const price = !item || item.input === undefined ? '' : !item.input && !item.output ? t('免费')
    : t('输入 {input} · 输出 {output} / 百万 token', { input: money(item.input, cur), output: money(item.output, cur) })
  const save = async () => {
    setBusy(true)
    const v = { provider: name, model }
    try { setSettings(await api('/api/settings', { method: 'PUT', body: { roles: { router: v, worker: v, reviewer: v, filter: v } } })); onDone() }
    catch (e: any) { toast(e.message, true); setBusy(false) }
  }
  return <>
    <h2>{t('选一个模型')}</h2>
    <p className="muted">{t('对话、后台任务、汇总和整理都先用这一个，以后可以在 设置 → 模型 里分别换。')} {tn('Bot 要看电脑屏幕，选能看图（{icon}）的。', { icon: <Icon name="eye" size={13} /> })}</p>
    {!l ? <div className="check-box"><b>{t('正在连接 {name}…', { name: prov?.title || '' })}</b></div>
      : !l.ok ? <div className="check-box bad"><b><Icon name="alert" size={14} /> {l.error}</b>
          <span className="muted small">{t('回上一步检查 API Key 和地址。')}</span></div>
      : <div className="check-box ok"><b><Icon name="check" size={14} /> {l.models.length ? t('连上了，有 {n} 个模型', { n: l.models.length }) : t('连上了')}</b>
          {l.note && <span className="muted small">{l.note}</span>}</div>}
    <ListsCtx.Provider value={ctx}>
      <div className="onb-picker"><ModelPicker kind="chat" only={name} value={{ provider: name, model }} onChange={(_, m) => setModel(m)} /></div>
    </ListsCtx.Provider>
    {model && <div className="role-meta">
      {price && <span className="chip">{price}</span>}
      {vision === 'yes' && <span className="chip ok"><Icon name="eye" size={13} />{t('能看图')}</span>}
      {vision === 'no' && <span className="chip bad"><Icon name="eye" size={13} />{t('不能看图，Bot 用不了电脑')}</span>}
      {vision === 'unsure' && <span className="chip warn"><Icon name="eye" size={13} />{t('可能不能看图')}</span>}
      <span className="grow" />
      <TestButton t={test} />
    </div>}
    {test.res && !test.res.ok && <div className="role-error"><Icon name="alert" size={14} />{test.res.error}</div>}
    <Foot><button className="btn" onClick={onBack}>{t('上一步')}</button><span className="grow" />
      <button className="btn primary" disabled={!model || busy} onClick={save}>{busy ? t('保存中…') : t('用这个模型，下一步')}</button></Foot>
  </>
}

// ---------- 第二步：你的 Bot ----------
function BotStep({ bot, onBack, onNext }: { bot: Bot; onBack: () => void; onNext: (b: Bot, tp?: TemplatePreview) => void }) {
  const [name, setName] = useState(bot.name)
  const [instr, setInstr] = useState(bot.instructions)
  const [avatar, setAvatar] = useState(bot.avatar)
  const [editAvatar, setEditAvatar] = useState(false)
  const [preview, setPreview] = useState<TemplatePreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  if (preview) return <TemplateInstall p={preview} onBack={() => setPreview(null)}
    onInstalled={async b => { await refreshLists(); onNext(b, preview) }} />
  const dirty = name.trim() !== bot.name || instr !== bot.instructions || JSON.stringify(avatar) !== JSON.stringify(bot.avatar)
  const next = async () => {
    if (!dirty) return onNext(bot)
    setBusy(true); setErr('')
    try { onNext(await api(`/api/bots/${bot.id}`, { method: 'PATCH', body: { name: name.trim() || bot.name, instructions: instr, avatar } })) }
    catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return <>
    <h2>{t('认识一下你的 Bot')}</h2>
    <p className="muted">{t('已经建好了一个 Bot。给它起个名字，说说主要请它帮你做什么；不写也行，直接在对话里交代。')}</p>
    <div className="onb-bot">
      <button className="panel-avatar" title={t('换个头像')} onClick={() => setEditAvatar(!editAvatar)}><BotAvatar avatar={avatar} size={64} /></button>
      <div className="onb-bot-fields">
        <label className="field">{t('名字')}<input value={name} maxLength={40} onChange={e => setName(e.target.value)} /></label>
        <label className="field">{t('主要帮你做什么')}
          <textarea rows={3} value={instr} onChange={e => setInstr(e.target.value)}
            placeholder={t('比如：每天早上整理 AI 领域的新闻；盯着我关注的几个 GitHub 项目；回邮件前先把草稿给我看')} />
        </label>
      </div>
    </div>
    {editAvatar && <AvatarPicker avatar={avatar} onChange={setAvatar} prompt={botPrompt(name, instr)} />}
    <h4>{t('或者从模板装一个现成的')}</h4>
    <p className="muted small">{t('模板带好了技能和定时任务，装上就能干活。{name} 也会留着。', { name: bot.name })}</p>
    <TemplatePicker compact onPreview={setPreview} />
    {err && <div className="error-line">{err}</div>}
    <Foot><button className="btn" onClick={onBack}>{t('上一步')}</button><span className="grow" />
      <button className="btn primary" disabled={busy} onClick={next}>{busy ? t('保存中…') : t('下一步')}</button></Foot>
  </>
}

// ---------- 第三步：连一个应用 ----------
function AppStep({ tpl, onBack, onNext, onMarket }: { tpl: TemplatePreview | null; onBack: () => void; onNext: () => void; onMarket: () => void }) {
  const apps = useStore(s => s.apps)
  const catalog = useStore(s => s.catalog)
  const [open, setOpen] = useState<string | null>(null)
  const byId = (id: string) => catalog.find(c => c.id === id)
  const need = (tpl?.apps || []).filter(a => a.in_market).map(a => a.name)
  const picks = PICKS.filter(id => !need.includes(id))
  const list = (ids: string[]) => ids.map(byId).filter((c): c is CatalogItem => !!c)
    .map(c => <AppCard key={c.id} c={c} onOpen={() => setOpen(c.id)} />)
  const any = apps.some(a => a.connected)
  return <>
    <h2>{t('让 Bot 用上你的应用')}</h2>
    <p className="muted">{t('连上以后，Bot 能替你记笔记、查资料、整理文件、发邮件。先连一个常用的试试，以后在侧栏“应用”里还能连更多。')}</p>
    {need.length > 0 && <><h4>{t('「{name}」要用', { name: tpl!.name })}</h4><div className="onb-apps">{list(need)}</div></>}
    <h4>{need.length ? t('其他常用的') : t('常用的')}</h4>
    <div className="onb-apps">{list(picks)}</div>
    <p className="muted small">{tn('应用市场里一共有 {n} 个，也能接任何 MCP server：{link}', { n: catalog.length, link: <button className="link-btn" onClick={onMarket}>{t('去应用市场看看')}</button> })}</p>
    <Foot><button className="btn" onClick={onBack}>{t('上一步')}</button><span className="grow" />
      <button className={'btn' + (any ? ' primary' : '')} onClick={onNext}>{any ? t('下一步') : t('先不连，下一步')}</button></Foot>
    {open && createPortal(<Sheet item={byId(open)} app={apps.find(a => a.name === open)} initErr="" onClose={() => setOpen(null)}
      onEdit={() => { setOpen(null); onMarket() }} />, document.body)}
  </>
}

function AppCard({ c, onOpen }: { c: CatalogItem; onOpen: () => void }) {
  const app = useStore(s => s.apps.find(a => a.name === c.id))
  return (
    <button className="onb-app" onClick={onOpen}>
      <AppIcon item={c} size={38} />
      <span className="onb-card-text"><b>{c.title}</b><span>{c.desc}</span></span>
      {app ? <StateTag app={app} /> : <span className="m-how">{howOf(c)}</span>}
    </button>
  )
}

// ---------- 好了 ----------
function Done({ bot, onStart, onModel, onChannels }: { bot: Bot; onStart: (text?: string) => void; onModel: () => void; onChannels: () => void }) {
  const s = useStore(x => x.settings) as S
  const apps = useStore(x => x.apps).filter(a => a.connected)
  const catalog = useStore(x => x.catalog)
  const r = s.roles.router
  const prov = s.providers.find(p => p.name === r.provider)
  const items = apps.map(a => catalog.find(c => c.id === (a.catalog || a.name))).filter((c): c is CatalogItem => !!c)
  const examples = [
    ...items.map(c => c.example).filter(Boolean).slice(0, 2),
    t('今天 AI 领域有什么值得关注的新闻？挑 5 条，附上链接'),
    t('每个工作日早上 9 点，把 Hacker News 上最热的 5 条整理给我'),
  ].slice(0, 3)
  return <>
    <h2>{t('好了，可以开始了')}</h2>
    <div className="onb-summary">
      <div className="onb-card">
        <ModelIcon prov={prov} model={r.model} size={36} />
        <span className="onb-card-text"><b>{tc('onb', '模型')}</b><span>{s.problem ? s.problem : `${prov?.title} · ${r.model}`}</span></span>
        {s.problem ? <button className="btn small" onClick={onModel}>{t('去接上')}</button> : <Icon name="check" size={18} className="onb-ok" />}
      </div>
      <div className="onb-card">
        <BotAvatar avatar={bot.avatar} size={36} />
        <span className="onb-card-text"><b>Bot</b><span>{bot.name}</span></span>
        <Icon name="check" size={18} className="onb-ok" />
      </div>
      <div className="onb-card">
        {items[0] ? <AppIcon item={items[0]} size={36} /> : <BrandIcon glyph="plug" size={36} />}
        <span className="onb-card-text"><b>{t('应用')}</b>
          <span>{items.length ? items.map(c => c.title).join(SEP) : apps.length ? tc('apps', '{n} 个', { n: apps.length }) : t('还没连，以后在侧栏“应用”里连')}</span></span>
        {apps.length > 0 && <Icon name="check" size={18} className="onb-ok" />}
      </div>
    </div>
    <h4>{t('试着这样跟 {name} 说', { name: bot.name })}</h4>
    <div className="onb-examples">
      {examples.map(x => <button key={x} onClick={() => onStart(x)}><span>{x}</span><Icon name="chevron" size={14} /></button>)}
    </div>
    <div className="onb-mobile">
      <span className="onb-brands">{CHANNELS.map(k => <BrandIcon key={k} brand={k} size={26} title={BRANDS[k].name} />)}</span>
      <span className="muted small">{t('在手机上也能用：连上聊天软件，就能在 Telegram、飞书、钉钉、企业微信、Discord 里跟 Bot 说话。')}</span>
      <button className="btn" onClick={onChannels}>{t('去连接')}</button>
    </div>
    <Foot><span className="grow" /><button className="btn primary" onClick={() => onStart()}>{t('开始对话')}</button></Foot>
  </>
}
