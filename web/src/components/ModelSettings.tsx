import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { api, setSettings, useStore } from '../store'
import { Icon } from '../icons'
import { lang, t, tc, tn } from '../i18n'
import type { ModelItem, ModelList, Preset, Provider, Role, Settings as S } from '../types'
import { money, PriceTag, Section, useToast } from './settingsKit'
import { BrandIcon, modelBrand, providerBrand } from './BrandIcon'
import { BRANDS } from '../brandIcons'
import { ask } from './Dialog'
import { imagePrice, type ImageModel } from './AvatarTools'
import { isEnter } from '../keys'

// 设置 → 模型：先加模型服务（地址 + API Key），再给每项工作选模型

type RoleDef = { key: string; name: string; desc: string; vision?: boolean }
export const ROLES: RoleDef[] = [
  { key: 'router', name: tc('role', '对话'), desc: t('Bot 本身：回复你、决定要不要派任务。要看电脑屏幕，得选能看图的。'), vision: true },
  { key: 'worker', name: tc('role', '后台任务'), desc: t('真正干活的：操作电脑、调用应用、查资料。也要能看图。'), vision: true },
  { key: 'reviewer', name: t('Heavy 汇总'), desc: t('Heavy 模式里，把几个并行任务的结果汇总成一份。') },
  { key: 'filter', name: t('整理和过滤'), desc: t('判断定时任务收到的事件要不要处理、把导入的记忆拆成条目。便宜的就行。') },
]
export const ROLE_NAMES: Record<string, string> = { ...Object.fromEntries(ROLES.map(r => [r.key, r.name])), embedding: t('按意思检索'), test: t('测试模型'), image: t('画图'), plugin: t('插件') }

// ---------- 各个服务的模型列表：打开页面时拉一次，服务端缓存 10 分钟 ----------
type Lists = Record<string, ModelList | undefined>
export const ListsCtx = createContext<{ lists: Lists; load: (name: string, refresh?: boolean) => Promise<void> }>({ lists: {}, load: async () => {} })

export function useLists(providers: Provider[]) {
  const [lists, setLists] = useState<Lists>({})
  const loading = useRef(new Set<string>())
  const load = async (name: string, refresh = false) => {
    if (loading.current.has(name)) return
    loading.current.add(name)
    if (refresh) setLists(l => ({ ...l, [name]: undefined }))
    try {
      const r = await api<ModelList>(`/api/providers/${name}/models${refresh ? '?refresh=true' : ''}`)
      setLists(l => ({ ...l, [name]: r }))
    } catch (e: any) {
      setLists(l => ({ ...l, [name]: { name, ok: false, error: e.message, note: '', models: [], items: [], ts: 0 } }))
    } finally { loading.current.delete(name) }
  }
  useEffect(() => { providers.forEach(p => { if (!lists[p.name]) load(p.name) }) }, [providers.map(p => p.name).join()])
  return { lists, load }
}

const usedBy = (s: S, name: string) => [
  ...Object.entries(s.roles).filter(([, v]) => v.provider === name).map(([k]) => ROLE_NAMES[k] || k),
  ...(s.embedding.provider === name ? [t('按意思检索')] : []),
  ...(s.image_model?.provider === name ? [t('画图')] : []),
]
const host = (url: string) => url.replace(/^https?:\/\//, '')

// 模型服务的图标：认得出品牌就用品牌图标（自定义的按接口地址认），认不出用服务商的颜色和首字
export function ProviderBadge({ preset, url, title, size = 34 }: { preset?: string; url?: string; title?: string; size?: number }) {
  const presets = useStore(s => s.settings?.presets) || []
  const p = presets.find(x => x.id === preset)
  const brand = providerBrand(preset, url)
  const custom = !p || p.id === 'custom'
  return <BrandIcon brand={brand} color={p?.color} icon={custom ? 'server' : undefined} letter={p?.title || title} size={size} title={title} />
}

// 模型的图标：按名字认是哪家的（DeepSeek、通义千问、Claude……），认不出用模型服务的图标
export function ModelIcon({ prov, model, size = 20 }: { prov?: Provider; model: string; size?: number }) {
  const brand = modelBrand(model)
  return brand ? <BrandIcon brand={brand} size={size} title={BRANDS[brand]?.name} />
    : <ProviderBadge preset={prov?.preset} url={prov?.base_url} title={prov?.title} size={size} />
}

export default function ModelSettings() {
  const s = useStore(x => x.settings) as S
  const ctx = useLists(s.providers)
  const [editing, setEditing] = useState<string | null>(null)   // 'new' 或者服务的名字
  const done = (name?: string) => { setEditing(null); if (name) ctx.load(name, true) }
  const extra = Object.keys(s.roles).filter(k => !ROLES.some(r => r.key === k)).map(k => ({ key: k, name: k, desc: '' }))
  return (
    <ListsCtx.Provider value={ctx}>
      {editing ? <ProviderForm name={editing === 'new' ? null : editing} onDone={done} /> : <>
        {s.problem && <div className="set-alert"><Icon name="alert" size={16} />
          <span><b>{t('{problem}，Bot 现在没法回复。', { problem: s.problem })}</b>{t('点下面的服务填上 API Key，或者添加别的服务，再在“各项工作用哪个模型”里换过去。')}</span></div>}
        <Section title={t('模型服务')} desc={t('提供模型的平台。填好地址和 API Key 就能用它的模型，密钥加密存在保险库里。')}
          action={<button className="btn" onClick={() => setEditing('new')}><Icon name="plus" size={14} />{t('添加')}</button>}>
          <div className="card-list">{s.providers.map(p => <ProviderRow key={p.name} p={p} onEdit={() => setEditing(p.name)} />)}</div>
        </Section>
        <Section title={t('各项工作用哪个模型')} desc={t('常做的事选便宜、快的，难的选强的。改了马上生效，点“测试”会真的调一次。')}>
          <div className="card-list">{[...ROLES.filter(r => s.roles[r.key]), ...extra].map(r => <RoleCard key={r.key} role={r} />)}</div>
        </Section>
        <Section title={t('按意思检索记忆和资料库')} desc={t('Bot 找以前的记忆和页面时用。不选也能按关键词找（中文按两个字一组）；选一个 embedding 模型后，说法不同、意思相近的也能找到。')}>
          <EmbeddingCard />
        </Section>
        <Section title={t('画图')} desc={t('你让 Bot 画图、生成头像时用。每次画之前都会先问你（卡片上写着价格），可以点“总是允许”。')}>
          <ImageModelCard />
        </Section>
      </>}
    </ListsCtx.Provider>
  )
}

// ---------- 模型服务 ----------
function keyText(p: Provider) {
  if (p.key_source === 'vault') return `API Key ${p.key_hint}`
  if (p.key_source === 'env') return t('API Key 来自环境变量 {env}', { env: p.key_env })
  return p.needs_key ? t('还没填 API Key') : t('不用 API Key')
}

function ProviderRow({ p, onEdit }: { p: Provider; onEdit: () => void }) {
  const s = useStore(x => x.settings) as S
  const { lists } = useContext(ListsCtx)
  const l = lists[p.name]
  const used = usedBy(s, p.name)
  const [cls, text] = !l ? ['busy', t('检查中…')] : l.ok ? ['ok', l.models.length ? t('能用 · {n} 个模型', { n: l.models.length }) : t('能用')]
    : [p.needs_key && !p.key_source ? 'warn' : 'bad', l.error]
  return (
    <button className="prov-row" onClick={onEdit}>
      <ProviderBadge preset={p.preset} url={p.base_url} title={p.title} />
      <span className="prov-main">
        <span className="prov-name">{p.title}{p.source === 'config' && <span className="tag" title={t('在 config.yaml 的 providers 里写的')}>config.yaml</span>}</span>
        <span className="prov-sub">{host(p.base_url)} · {keyText(p)}{used.length ? ' · ' + t('{roles}在用', { roles: used.join(lang === 'en' ? ', ' : '、') }) : ''}</span>
      </span>
      <span className={'m-state ' + cls} title={text}><i />{text}</span>
      <Icon name="chevron" size={16} className="prov-go" />
    </button>
  )
}

type Check = { ok: boolean; models: string[]; error: string; note: string }

// presetId：已经选好服务商（引导里用）；onBack、backLabel：返回到哪里，不传就是回到设置页的模型
export function ProviderForm({ name, onDone, presetId, onBack, backLabel }: {
  name: string | null; onDone: (name?: string) => void; presetId?: string; onBack?: () => void; backLabel?: string
}) {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const old = name ? s.providers.find(p => p.name === name) : undefined
  const custom = s.presets.find(x => x.id === 'custom')!
  const [preset, setPreset] = useState<Preset | null>(old ? s.presets.find(x => x.id === old.preset) || custom : s.presets.find(x => x.id === presetId) || null)
  const [title, setTitle] = useState(old?.title || (preset && preset.id !== 'custom' ? preset.title : ''))
  const [url, setUrl] = useState(old?.base_url || preset?.base_url || '')
  const [key, setKey] = useState('')
  const [check, setCheck] = useState<Check | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const pick = (p: Preset) => { setPreset(p); setTitle(p.id === 'custom' ? '' : p.title); setUrl(p.base_url); setCheck(null); setErr('') }

  if (!preset) return (
    <div className="prov-form">
      <button className="back-link" onClick={() => onDone()}><Icon name="left" size={14} />{t('模型')}</button>
      <h3>{t('添加模型服务')}</h3>
      <p className="muted small">{t('选一个服务商。列表里没有的选“其他”，任何兼容 OpenAI 接口的都能用。')}</p>
      <div className="preset-cards">
        {s.presets.map(p => (
          <button key={p.id} className="preset-card" onClick={() => pick(p)}>
            <ProviderBadge preset={p.id} title={p.title} size={36} />
            <span><b>{p.title}</b><span>{p.desc}</span></span>
          </button>))}
      </div>
    </div>
  )

  const body = (extra: object = {}) => ({ preset: preset.id, title: title.trim(), base_url: url.trim(), api_key: key.trim(), ...extra })
  const test = async () => {
    setBusy('test'); setErr(''); setCheck(null)
    try { setCheck(await api<Check>('/api/providers/check', { method: 'POST', body: { name, base_url: url.trim(), api_key: key.trim() } })) }
    catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  const save = async (extra: object = {}) => {
    setBusy('save'); setErr('')
    try {
      const r = name ? await api(`/api/providers/${name}`, { method: 'PUT', body: body(extra) }) : await api('/api/providers', { method: 'POST', body: body() })
      setSettings(r.settings)
      toast(name ? t('已保存') : t('已添加 {name}', { name: title.trim() || preset.title }))
      onDone(r.name)
    } catch (e: any) { setErr(e.message); setBusy('') }
  }
  const remove = async () => {
    if (!(await ask(t('删除模型服务“{name}”？它的 API Key 也一起删掉。', { name: old!.title }), { ok: t('删除'), danger: true }))) return
    try { const r = await api(`/api/providers/${name}`, { method: 'DELETE' }); setSettings(r.settings); toast(t('已删除')); onDone() }
    catch (e: any) { setErr(e.message) }
  }
  const placeholder = old?.key_source === 'vault' ? t('已保存 {hint}，不改就留空', { hint: old.key_hint })
    : old?.key_source === 'env' ? t('正在用环境变量 {env}，在这里填了优先用这里的', { env: old.key_env })
    : preset.no_key ? t('本机的一般不用填') : 'sk-…'
  return (
    <div className="prov-form">
      <button className="back-link" onClick={() => (onBack ? onBack() : name ? onDone() : setPreset(null))}><Icon name="left" size={14} />{backLabel || (name ? t('模型') : t('换一个服务商'))}</button>
      <div className="prov-form-head">
        <ProviderBadge preset={preset.id} url={url} title={title || preset.title} size={44} />
        <div><h3>{name ? old?.title : t('添加 {name}', { name: preset.title })}</h3><span className="muted small">{preset.desc}</span></div>
      </div>
      <label className="field">{t('名字')}<input value={title} placeholder={preset.id === 'custom' ? t('比如：公司的 vLLM') : preset.title} onChange={e => setTitle(e.target.value)} /></label>
      <label className="field">{t('接口地址')}
        <input value={url} placeholder="https://example.com/v1" onChange={e => { setUrl(e.target.value); setCheck(null) }} />
        {preset.note && <span className="muted small">{preset.note}</span>}
      </label>
      <label className="field">API Key
        <input type="password" autoComplete="off" value={key} placeholder={placeholder} onChange={e => { setKey(e.target.value); setCheck(null) }} />
      </label>
      <div className="field-links">
        {preset.key_url && <a href={preset.key_url} target="_blank" rel="noreferrer">{t('去 {name} 后台拿一个', { name: preset.title })}<Icon name="external" size={12} /></a>}
        {old?.source === 'config' && old.key_source === 'vault' && old.key_env &&
          <button className="link-btn" onClick={() => save({ clear_key: true })}>{t('删掉这里填的，改回用环境变量 {env}', { env: old.key_env })}</button>}
      </div>
      {check && (check.ok
        ? <div className="check-box ok"><b><Icon name="check" size={14} /> {check.models.length ? t('连上了，有 {n} 个模型', { n: check.models.length }) : t('连上了')}</b>
            {check.note && <span className="muted small">{check.note}</span>}
            {!!check.models.length && <span className="model-chips">{check.models.slice(0, 10).map(m => <code key={m}>{m}</code>)}{check.models.length > 10 && <span className="muted small">…</span>}</span>}
          </div>
        : <div className="check-box bad"><b><Icon name="alert" size={14} /> {check.error}</b></div>)}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn" disabled={!url.trim() || !!busy} onClick={test}>{busy === 'test' ? t('测试中…') : t('测试连接')}</button>
        <button className="btn primary" disabled={!url.trim() || !!busy} onClick={() => save()}>{busy === 'save' ? t('保存中…') : name ? t('保存') : t('添加')}</button>
        <span className="grow" />
        {old?.source === 'ui' && <button className="btn danger" onClick={remove}><Icon name="trash" size={14} />{t('删除')}</button>}
      </div>
    </div>
  )
}

// ---------- 每项工作用哪个模型 ----------
type TestResult = { ok: boolean; error?: string; ms?: number; vision?: 'yes' | 'no' | 'unsure'; vision_error?: string; dim?: number }

export function useTest(body: () => object, deps: unknown[]) {
  const [res, setRes] = useState<TestResult | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => setRes(null), deps)
  const run = async () => {
    setBusy(true); setRes(null)
    try { setRes(await api<TestResult>('/api/models/test', { method: 'POST', body: body() })) }
    catch (e: any) { setRes({ ok: false, error: e.message }) }
    setBusy(false)
  }
  return { res, busy, run }
}

const THINK: [string, string][] = [['', t('思考：默认')], ['low', t('思考：少一些')], ['high', t('思考：多一些')], ['max', t('思考：最多')], ['off', t('不思考（快、省钱）')]]

function RoleCard({ role }: { role: RoleDef }) {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const v = s.roles[role.key]
  const info = s.model_info[`${v.provider}/${v.model}`]
  const test = useTest(() => ({ provider: v.provider, model: v.model, role: role.key }), [v.provider, v.model, v.thinking, v.reasoning_effort])
  const save = async (patch: Partial<Role>) => {
    try { setSettings(await api('/api/settings', { method: 'PUT', body: { roles: { [role.key]: patch } } })); toast(t('已保存，马上生效')) }
    catch (e: any) { toast(e.message, true) }
  }
  const vision = test.res?.ok ? test.res.vision : info?.vision === undefined ? undefined : info.vision ? 'yes' : 'no'
  const think = v.thinking === false ? 'off' : v.reasoning_effort || ''
  return (
    <div className="role-card">
      <div className="role-top">
        <div className="role-text"><b>{role.name}</b>{role.desc && <span>{role.desc}</span>}</div>
        <ModelPicker kind="chat" value={v} onChange={(provider, model) => save({ provider, model })} />
      </div>
      <div className="role-meta">
        <PriceTag info={info} />
        {vision === 'yes' && <span className="chip ok"><Icon name="eye" size={13} />{t('能看图')}</span>}
        {vision === 'no' && <span className={'chip' + (role.vision ? ' bad' : '')}><Icon name="eye" size={13} />{role.vision ? t('不能看图，用不了电脑') : t('不能看图')}</span>}
        {vision === 'unsure' && <span className="chip warn" title={t('请求没报错，但模型没认出测试图片的颜色')}><Icon name="eye" size={13} />{t('可能不能看图')}</span>}
        <span className="grow" />
        <select className="think" value={think} title={t('思考模式：不是所有模型都支持，改了点“测试”确认一下')}
          onChange={e => save(e.target.value === 'off' ? { thinking: false, reasoning_effort: '' } : { thinking: true, reasoning_effort: e.target.value })}>
          {THINK.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <TestButton t={test} />
      </div>
      {test.res && !test.res.ok && <div className="role-error"><Icon name="alert" size={14} />{test.res.error}</div>}
    </div>
  )
}

// 属性名 t 是引导页也在用的接口，组件里改叫 test，不挡住翻译函数 t
export function TestButton({ t: test }: { t: ReturnType<typeof useTest> }) {
  const sec = ((test.res?.ms || 0) / 1000).toFixed(1)
  return <>
    {test.res?.ok && <span className="test-ok"><Icon name="check" size={14} />{test.res.dim ? t('能用 · {s} 秒 · {dim} 维', { s: sec, dim: test.res.dim }) : t('能用 · {s} 秒', { s: sec })}</span>}
    <button className="btn tiny" disabled={test.busy} onClick={test.run}><Icon name="zap" size={13} />{test.busy ? t('测试中…') : t('测试')}</button>
  </>
}

function ImageModelCard() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const [list, setList] = useState<ImageModel[] | null>(null)
  const [auto, setAuto] = useState<ImageModel | null>(null)
  const load = () => api('/api/avatars/models').then(r => { setList(r.models); setAuto(r.models[0] || null) }).catch(() => setList([]))
  useEffect(() => { load() }, [s.providers.map(p => p.name).join()])
  const key = (m: { provider: string; model: string }) => m.provider + '\n' + m.model
  const cur = s.image_model?.provider ? key(s.image_model) : ''
  const save = async (v: string) => {
    const [provider, model] = v ? v.split('\n') : ['', '']
    try {
      setSettings(await api('/api/settings', { method: 'PUT', body: { image_model: { provider, model } } }))
      toast(t('已保存'))
    } catch (err: any) { toast(err.message, true) }
  }
  const chosen = list?.find(m => key(m) === cur) || (!cur ? auto : null)
  return (
    <div className="role-card">
      <div className="role-top">
        <div className="role-text"><b>{t('画图模型')}</b><span>{list === null ? t('正在找能画图的模型…') : list.length ? t('{n} 个能画图的模型', { n: list.length }) : ''}</span></div>
        {list && list.length > 0 && (
          <select value={cur} onChange={e => save(e.target.value)} title={t('用哪个模型画')}>
            <option value="">{auto ? t('自动（{model}）', { model: auto.model }) : t('自动')}</option>
            {list.map(m => <option key={key(m)} value={key(m)}>{m.model} · {m.title}</option>)}
            {cur && !list.some(m => key(m) === cur) && <option value={cur}>{s.image_model.model}</option>}
          </select>)}
      </div>
      {chosen && <div className="role-meta"><span className="chip" title={t('按张的价格写在 config.yaml 的 pricing.images 里')}>{imagePrice(chosen.price, s.prices.currency)}</span></div>}
      {list && !list.length && <p className="muted small hint">{t('还没有能画图的模型。添加硅基流动（Kolors 免费）、OpenAI、智谱这类有画图模型的服务就能用。')}</p>}
    </div>
  )
}

function EmbeddingCard() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const e = s.embedding
  const info = s.model_info[`${e.provider}/${e.model}`]
  const [idx, setIdx] = useState<{ chunks: number; embedded: number; error: string; embedding: unknown } | null>(null)
  const test = useTest(() => ({ provider: e.provider, model: e.model, kind: 'embedding' }), [e.provider, e.model])
  useEffect(() => {
    const f = () => api('/api/search/info').then(setIdx).catch(() => {})
    f(); const timer = setInterval(f, 4000); return () => clearInterval(timer)
  }, [])
  const save = async (provider: string, model: string) => {
    try {
      setSettings(await api('/api/settings', { method: 'PUT', body: { embedding: { provider, model } } }))
      toast(provider ? t('已保存，后台开始算向量') : t('已关闭，只按关键词找'))
    } catch (err: any) { toast(err.message, true) }
  }
  return (
    <div className="role-card">
      <div className="role-top">
        <div className="role-text"><b>{t('embedding 模型')}</b><span>
          {idx ? t('已索引 {n} 段', { n: idx.chunks }) + (idx.embedding ? t('，算好向量 {n} 段', { n: idx.embedded }) + (idx.embedded < idx.chunks && !idx.error ? t('（后台在算）') : '') : '') : ''}
        </span></div>
        <ModelPicker kind="embedding" allowNone value={e} onChange={save} />
      </div>
      {e.provider ? <div className="role-meta"><PriceTag info={info} /><span className="grow" /><TestButton t={test} /></div>
        : <p className="muted small hint">{tn('推荐：硅基流动的 {a}（免费）、OpenAI 的 {b}、本机 Ollama 的 {c}。先在上面添加对应的服务。', {
            a: <code>BAAI/bge-m3</code>, b: <code>text-embedding-3-small</code>, c: <code>nomic-embed-text</code> })}</p>}
      {test.res && !test.res.ok && <div className="role-error"><Icon name="alert" size={14} />{test.res.error}</div>}
      {idx?.error && <div className="role-error"><Icon name="alert" size={14} />{t('向量服务出错了，先只按关键词找：{error}', { error: idx.error })}</div>}
    </div>
  )
}

// ---------- 选模型：按服务分组，可以搜，也可以直接填名字 ----------
export function ModelPicker({ kind, value, onChange, allowNone, only }: {
  kind: 'chat' | 'embedding'; value: { provider: string; model: string }; onChange: (provider: string, model: string) => void; allowNone?: boolean
  only?: string   // 只列这个服务的模型
}) {
  const s = useStore(x => x.settings) as S
  const cur = s.prices.currency
  const { lists, load } = useContext(ListsCtx)
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [pos, setPos] = useState<CSSProperties>({})
  const box = useRef<HTMLDivElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  // 弹出框用 fixed 定位，不被设置页的滚动区域裁掉；下面放不下就往上开
  useLayoutEffect(() => {
    if (!open || !box.current) return
    const r = box.current.getBoundingClientRect()
    const width = Math.min(Math.max(460, r.width), innerWidth - 16)   // 按钮比弹出框宽时（引导里）和按钮一样宽
    const below = innerHeight - r.bottom - 12, above = r.top - 12
    const up = below < 320 && above > below
    setPos({ position: 'fixed', width, left: Math.max(8, Math.min(r.right - width, innerWidth - width - 8)), maxHeight: Math.min(480, up ? above : below),
      ...(up ? { bottom: innerHeight - r.top + 6 } : { top: r.bottom + 6 }) })
  }, [open])
  useEffect(() => {
    if (!open) return
    const h = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false) }
    const scroll = (e: Event) => { if (!pop.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', h)
    document.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', scroll)
    return () => { document.removeEventListener('mousedown', h); document.removeEventListener('scroll', scroll, true); window.removeEventListener('resize', scroll) }
  }, [open])
  const prov = s.providers.find(p => p.name === value.provider)
  const query = q.trim()
  const lq = query.toLowerCase()
  const fits = (m: ModelItem) => (kind === 'embedding' ? m.kind === 'embedding' : m.kind === 'chat')
  const groups = s.providers.filter(p => !only || p.name === only).map(p => {
    const l = lists[p.name]
    const items = (l?.items || []).filter(m => (fits(m) || (p.name === value.provider && m.id === value.model)) && (!lq || m.id.toLowerCase().includes(lq)))
    return { p, l, items, exact: (l?.items || []).some(m => m.id === query) }
  })
  const choose = (provider: string, model: string) => {
    setOpen(false); setQ('')
    if (provider !== value.provider || model !== value.model) onChange(provider, model)
  }
  const first = groups.find(g => g.items.length)
  const price = (m: ModelItem) => m.input === undefined ? '' : !m.input && !m.output ? t('免费') : `${money(m.input, cur)} / ${money(m.output, cur)}`
  return (
    <div className="mp" ref={box}>
      <button className={'mp-btn' + (open ? ' on' : '')} onClick={() => setOpen(!open)}>
        {value.provider && value.model ? <>
          <ModelIcon prov={prov} model={value.model} size={22} />
          <span className="mp-label"><span className="mp-prov">{prov?.title || value.provider}</span><span className="mp-model">{value.model}</span></span>
        </> : <span className="mp-label muted">{allowNone ? t('不用（只按关键词找）') : t('选一个模型')}</span>}
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <div className="mp-pop" ref={pop} style={pos}>
          <div className="mp-search">
            <Icon name="search" size={14} />
            <input autoFocus value={q} placeholder={t('搜索，或者直接填模型名字')} onChange={e => setQ(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) }
                if (isEnter(e) && first) choose(first.p.name, first.items[0].id)
              }} />
          </div>
          <div className="mp-list">
            {allowNone && !lq && <button className={'mp-item' + (!value.provider ? ' on' : '')} onClick={() => choose('', '')}>
              <span className="mp-id">{t('不用（只按关键词找）')}</span>{!value.provider && <Icon name="check" size={14} />}</button>}
            {groups.map(({ p, l, items, exact }) => (
              <div key={p.name} className="mp-group">
                <div className="mp-group-head">
                  <ProviderBadge preset={p.preset} url={p.base_url} title={p.title} size={18} /><span>{p.title}</span>
                  <span className="grow" />
                  {l && !l.ok && <span className="mp-err" title={l.error}>{l.error}</span>}
                  <button className="mp-refresh" title={t('重新拉模型列表')} onClick={() => load(p.name, true)}><Icon name="refresh" size={12} /></button>
                </div>
                {!l && <div className="mp-empty">{t('加载中…')}</div>}
                {items.map(m => {
                  const on = p.name === value.provider && m.id === value.model
                  return (
                    <button key={m.id} className={'mp-item' + (on ? ' on' : '')} onClick={() => choose(p.name, m.id)}>
                      <ModelIcon prov={p} model={m.id} size={18} />
                      <span className="mp-id">{m.id}</span>
                      {kind === 'chat' && m.vision && <span title={t('能看图')}><Icon name="eye" size={13} className="mp-vision" /></span>}
                      <span className="mp-price">{price(m)}</span>
                      {on ? <Icon name="check" size={14} /> : <span className="mp-sp" />}
                    </button>)
                })}
                {l?.ok && !items.length && !lq && <div className="mp-empty">{kind === 'embedding' ? t('没认出 embedding 模型，可以在上面直接填名字') : t('没有列出模型，可以在上面直接填名字')}</div>}
                {query && !exact && <button className="mp-item mp-custom" onClick={() => choose(p.name, query)}>
                  <ModelIcon prov={p} model={query} size={18} /><span className="mp-id">{t('在 {provider} 用 “{model}”', { provider: p.title, model: query })}</span></button>}
              </div>
            ))}
          </div>
          <div className="mp-foot muted">{tn('单价是每百万 token 的输入 / 输出价，{eye} 表示能看图', { eye: <Icon name="eye" size={12} /> })}</div>
        </div>
      )}
    </div>
  )
}
