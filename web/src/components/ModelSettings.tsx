import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { api, setSettings, useStore } from '../store'
import { Icon } from '../icons'
import type { ModelItem, ModelList, Preset, Provider, Role, Settings as S } from '../types'
import { money, PriceTag, Section, useToast } from './settingsKit'

// 设置 → 模型：先加模型服务（地址 + API Key），再给每项工作选模型

type RoleDef = { key: string; name: string; desc: string; vision?: boolean }
export const ROLES: RoleDef[] = [
  { key: 'router', name: '对话', desc: 'Bot 本身：回复你、决定要不要派任务。要看电脑屏幕，得选能看图的。', vision: true },
  { key: 'worker', name: '后台任务', desc: '真正干活的：操作电脑、调用应用、查资料。也要能看图。', vision: true },
  { key: 'reviewer', name: 'Heavy 汇总', desc: 'Heavy 模式里，把几个并行任务的结果汇总成一份。' },
  { key: 'filter', name: '整理和过滤', desc: '判断定时任务收到的事件要不要处理、把导入的记忆拆成条目。便宜的就行。' },
]
export const ROLE_NAMES: Record<string, string> = { ...Object.fromEntries(ROLES.map(r => [r.key, r.name])), embedding: '按意思检索', test: '测试模型' }

// ---------- 各个服务的模型列表：打开页面时拉一次，服务端缓存 10 分钟 ----------
type Lists = Record<string, ModelList | undefined>
const ListsCtx = createContext<{ lists: Lists; load: (name: string, refresh?: boolean) => Promise<void> }>({ lists: {}, load: async () => {} })

function useLists(providers: Provider[]) {
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
  ...(s.embedding.provider === name ? ['按意思检索'] : []),
]
const host = (url: string) => url.replace(/^https?:\/\//, '')

export function ProviderBadge({ preset, title, size = 34 }: { preset?: string; title?: string; size?: number }) {
  const presets = useStore(s => s.settings?.presets) || []
  const p = presets.find(x => x.id === preset)
  const custom = !p || p.id === 'custom'
  return (
    <span className="prov-badge" style={{ width: size, height: size, background: p?.color || '#5b5b63', fontSize: size * .44, borderRadius: size * .28 }}>
      {custom ? <Icon name="server" size={Math.round(size * .5)} /> : (p.title || title || '?').slice(0, 1).toUpperCase()}
    </span>
  )
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
          <span><b>{s.problem}，Bot 现在没法回复。</b>点下面的服务填上 API Key，或者添加别的服务，再在“各项工作用哪个模型”里换过去。</span></div>}
        <Section title="模型服务" desc="提供模型的平台。填好地址和 API Key 就能用它的模型，密钥加密存在保险库里。"
          action={<button className="btn" onClick={() => setEditing('new')}><Icon name="plus" size={14} />添加</button>}>
          <div className="card-list">{s.providers.map(p => <ProviderRow key={p.name} p={p} onEdit={() => setEditing(p.name)} />)}</div>
        </Section>
        <Section title="各项工作用哪个模型" desc="常做的事选便宜、快的，难的选强的。改了马上生效，点“测试”会真的调一次。">
          <div className="card-list">{[...ROLES.filter(r => s.roles[r.key]), ...extra].map(r => <RoleCard key={r.key} role={r} />)}</div>
        </Section>
        <Section title="按意思检索记忆和资料库" desc="Bot 找以前的记忆和页面时用。不选也能按关键词找（中文按两个字一组）；选一个 embedding 模型后，说法不同、意思相近的也能找到。">
          <EmbeddingCard />
        </Section>
      </>}
    </ListsCtx.Provider>
  )
}

// ---------- 模型服务 ----------
function keyText(p: Provider) {
  if (p.key_source === 'vault') return `API Key ${p.key_hint}`
  if (p.key_source === 'env') return `API Key 来自环境变量 ${p.key_env}`
  return p.needs_key ? '还没填 API Key' : '不用 API Key'
}

function ProviderRow({ p, onEdit }: { p: Provider; onEdit: () => void }) {
  const s = useStore(x => x.settings) as S
  const { lists } = useContext(ListsCtx)
  const l = lists[p.name]
  const used = usedBy(s, p.name)
  const [cls, text] = !l ? ['busy', '检查中…'] : l.ok ? ['ok', l.models.length ? `能用 · ${l.models.length} 个模型` : '能用']
    : [p.needs_key && !p.key_source ? 'warn' : 'bad', l.error]
  return (
    <button className="prov-row" onClick={onEdit}>
      <ProviderBadge preset={p.preset} title={p.title} />
      <span className="prov-main">
        <span className="prov-name">{p.title}{p.source === 'config' && <span className="tag" title="在 config.yaml 的 providers 里写的">config.yaml</span>}</span>
        <span className="prov-sub">{host(p.base_url)} · {keyText(p)}{used.length ? ` · ${used.join('、')}在用` : ''}</span>
      </span>
      <span className={'m-state ' + cls} title={text}><i />{text}</span>
      <Icon name="chevron" size={16} className="prov-go" />
    </button>
  )
}

type Check = { ok: boolean; models: string[]; error: string; note: string }

function ProviderForm({ name, onDone }: { name: string | null; onDone: (name?: string) => void }) {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const old = name ? s.providers.find(p => p.name === name) : undefined
  const custom = s.presets.find(x => x.id === 'custom')!
  const [preset, setPreset] = useState<Preset | null>(old ? s.presets.find(x => x.id === old.preset) || custom : null)
  const [title, setTitle] = useState(old?.title || '')
  const [url, setUrl] = useState(old?.base_url || '')
  const [key, setKey] = useState('')
  const [check, setCheck] = useState<Check | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const pick = (p: Preset) => { setPreset(p); setTitle(p.id === 'custom' ? '' : p.title); setUrl(p.base_url); setCheck(null); setErr('') }

  if (!preset) return (
    <div className="prov-form">
      <button className="back-link" onClick={() => onDone()}><Icon name="left" size={14} />模型</button>
      <h3>添加模型服务</h3>
      <p className="muted small">选一个服务商。列表里没有的选“其他”，任何兼容 OpenAI 接口的都能用。</p>
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
      toast(name ? '已保存' : `已添加 ${title.trim() || preset.title}`)
      onDone(r.name)
    } catch (e: any) { setErr(e.message); setBusy('') }
  }
  const remove = async () => {
    if (!confirm(`删除模型服务“${old!.title}”？它的 API Key 也一起删掉。`)) return
    try { const r = await api(`/api/providers/${name}`, { method: 'DELETE' }); setSettings(r.settings); toast('已删除'); onDone() }
    catch (e: any) { setErr(e.message) }
  }
  const placeholder = old?.key_source === 'vault' ? `已保存 ${old.key_hint}，不改就留空`
    : old?.key_source === 'env' ? `正在用环境变量 ${old.key_env}，在这里填了优先用这里的`
    : preset.no_key ? '本机的一般不用填' : 'sk-…'
  return (
    <div className="prov-form">
      <button className="back-link" onClick={() => (name ? onDone() : setPreset(null))}><Icon name="left" size={14} />{name ? '模型' : '换一个服务商'}</button>
      <div className="prov-form-head">
        <ProviderBadge preset={preset.id} title={preset.title} size={44} />
        <div><h3>{name ? old?.title : `添加 ${preset.title}`}</h3><span className="muted small">{preset.desc}</span></div>
      </div>
      <label className="field">名字<input value={title} placeholder={preset.id === 'custom' ? '比如：公司的 vLLM' : preset.title} onChange={e => setTitle(e.target.value)} /></label>
      <label className="field">接口地址
        <input value={url} placeholder="https://example.com/v1" onChange={e => { setUrl(e.target.value); setCheck(null) }} />
        {preset.note && <span className="muted small">{preset.note}</span>}
      </label>
      <label className="field">API Key
        <input type="password" autoComplete="off" value={key} placeholder={placeholder} onChange={e => { setKey(e.target.value); setCheck(null) }} />
      </label>
      <div className="field-links">
        {preset.key_url && <a href={preset.key_url} target="_blank" rel="noreferrer">去 {preset.title} 后台拿一个<Icon name="external" size={12} /></a>}
        {old?.source === 'config' && old.key_source === 'vault' && old.key_env &&
          <button className="link-btn" onClick={() => save({ clear_key: true })}>删掉这里填的，改回用环境变量 {old.key_env}</button>}
      </div>
      {check && (check.ok
        ? <div className="check-box ok"><b><Icon name="check" size={14} /> 连上了{check.models.length ? `，有 ${check.models.length} 个模型` : ''}</b>
            {check.note && <span className="muted small">{check.note}</span>}
            {!!check.models.length && <span className="model-chips">{check.models.slice(0, 10).map(m => <code key={m}>{m}</code>)}{check.models.length > 10 && <span className="muted small">…</span>}</span>}
          </div>
        : <div className="check-box bad"><b><Icon name="alert" size={14} /> {check.error}</b></div>)}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn" disabled={!url.trim() || !!busy} onClick={test}>{busy === 'test' ? '测试中…' : '测试连接'}</button>
        <button className="btn primary" disabled={!url.trim() || !!busy} onClick={() => save()}>{busy === 'save' ? '保存中…' : name ? '保存' : '添加'}</button>
        <span className="grow" />
        {old?.source === 'ui' && <button className="btn danger" onClick={remove}><Icon name="trash" size={14} />删除</button>}
      </div>
    </div>
  )
}

// ---------- 每项工作用哪个模型 ----------
type TestResult = { ok: boolean; error?: string; ms?: number; vision?: 'yes' | 'no' | 'unsure'; vision_error?: string; dim?: number }

function useTest(body: () => object, deps: unknown[]) {
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

const THINK: [string, string][] = [['', '思考：默认'], ['low', '思考：少一些'], ['high', '思考：多一些'], ['max', '思考：最多'], ['off', '不思考（快、省钱）']]

function RoleCard({ role }: { role: RoleDef }) {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const v = s.roles[role.key]
  const info = s.model_info[`${v.provider}/${v.model}`]
  const t = useTest(() => ({ provider: v.provider, model: v.model, role: role.key }), [v.provider, v.model, v.thinking, v.reasoning_effort])
  const save = async (patch: Partial<Role>) => {
    try { setSettings(await api('/api/settings', { method: 'PUT', body: { roles: { [role.key]: patch } } })); toast('已保存，马上生效') }
    catch (e: any) { toast(e.message, true) }
  }
  const vision = t.res?.ok ? t.res.vision : info?.vision === undefined ? undefined : info.vision ? 'yes' : 'no'
  const think = v.thinking === false ? 'off' : v.reasoning_effort || ''
  return (
    <div className="role-card">
      <div className="role-top">
        <div className="role-text"><b>{role.name}</b>{role.desc && <span>{role.desc}</span>}</div>
        <ModelPicker kind="chat" value={v} onChange={(provider, model) => save({ provider, model })} />
      </div>
      <div className="role-meta">
        <PriceTag info={info} />
        {vision === 'yes' && <span className="chip ok"><Icon name="eye" size={13} />能看图</span>}
        {vision === 'no' && <span className={'chip' + (role.vision ? ' bad' : '')}><Icon name="eye" size={13} />不能看图{role.vision ? '，用不了电脑' : ''}</span>}
        {vision === 'unsure' && <span className="chip warn" title="请求没报错，但模型没认出测试图片的颜色"><Icon name="eye" size={13} />可能不能看图</span>}
        <span className="grow" />
        <select className="think" value={think} title="思考模式：不是所有模型都支持，改了点“测试”确认一下"
          onChange={e => save(e.target.value === 'off' ? { thinking: false, reasoning_effort: '' } : { thinking: true, reasoning_effort: e.target.value })}>
          {THINK.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select>
        <TestButton t={t} />
      </div>
      {t.res && !t.res.ok && <div className="role-error"><Icon name="alert" size={14} />{t.res.error}</div>}
    </div>
  )
}

function TestButton({ t }: { t: ReturnType<typeof useTest> }) {
  return <>
    {t.res?.ok && <span className="test-ok"><Icon name="check" size={14} />能用 · {((t.res.ms || 0) / 1000).toFixed(1)} 秒{t.res.dim ? ` · ${t.res.dim} 维` : ''}</span>}
    <button className="btn tiny" disabled={t.busy} onClick={t.run}><Icon name="zap" size={13} />{t.busy ? '测试中…' : '测试'}</button>
  </>
}

function EmbeddingCard() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const e = s.embedding
  const info = s.model_info[`${e.provider}/${e.model}`]
  const [idx, setIdx] = useState<{ chunks: number; embedded: number; error: string; embedding: unknown } | null>(null)
  const t = useTest(() => ({ provider: e.provider, model: e.model, kind: 'embedding' }), [e.provider, e.model])
  useEffect(() => {
    const f = () => api('/api/search/info').then(setIdx).catch(() => {})
    f(); const timer = setInterval(f, 4000); return () => clearInterval(timer)
  }, [])
  const save = async (provider: string, model: string) => {
    try {
      setSettings(await api('/api/settings', { method: 'PUT', body: { embedding: { provider, model } } }))
      toast(provider ? '已保存，后台开始算向量' : '已关闭，只按关键词找')
    } catch (err: any) { toast(err.message, true) }
  }
  return (
    <div className="role-card">
      <div className="role-top">
        <div className="role-text"><b>embedding 模型</b><span>
          {idx ? `已索引 ${idx.chunks} 段${idx.embedding ? `，算好向量 ${idx.embedded} 段${idx.embedded < idx.chunks && !idx.error ? '（后台在算）' : ''}` : ''}` : ''}
        </span></div>
        <ModelPicker kind="embedding" allowNone value={e} onChange={save} />
      </div>
      {e.provider ? <div className="role-meta"><PriceTag info={info} /><span className="grow" /><TestButton t={t} /></div>
        : <p className="muted small hint">推荐：硅基流动的 <code>BAAI/bge-m3</code>（免费）、OpenAI 的 <code>text-embedding-3-small</code>、本机 Ollama 的 <code>nomic-embed-text</code>。先在上面添加对应的服务。</p>}
      {t.res && !t.res.ok && <div className="role-error"><Icon name="alert" size={14} />{t.res.error}</div>}
      {idx?.error && <div className="role-error"><Icon name="alert" size={14} />向量服务出错了，先只按关键词找：{idx.error}</div>}
    </div>
  )
}

// ---------- 选模型：按服务分组，可以搜，也可以直接填名字 ----------
function ModelPicker({ kind, value, onChange, allowNone }: {
  kind: 'chat' | 'embedding'; value: { provider: string; model: string }; onChange: (provider: string, model: string) => void; allowNone?: boolean
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
    const width = Math.min(460, innerWidth - 16)
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
  const groups = s.providers.map(p => {
    const l = lists[p.name]
    const items = (l?.items || []).filter(m => (fits(m) || (p.name === value.provider && m.id === value.model)) && (!lq || m.id.toLowerCase().includes(lq)))
    return { p, l, items, exact: (l?.items || []).some(m => m.id === query) }
  })
  const choose = (provider: string, model: string) => {
    setOpen(false); setQ('')
    if (provider !== value.provider || model !== value.model) onChange(provider, model)
  }
  const first = groups.find(g => g.items.length)
  const price = (m: ModelItem) => m.input === undefined ? '' : !m.input && !m.output ? '免费' : `${money(m.input, cur)} / ${money(m.output, cur)}`
  return (
    <div className="mp" ref={box}>
      <button className={'mp-btn' + (open ? ' on' : '')} onClick={() => setOpen(!open)}>
        {value.provider ? <>
          <ProviderBadge preset={prov?.preset} title={prov?.title} size={20} />
          <span className="mp-label"><span className="mp-prov">{prov?.title || value.provider}</span><span className="mp-model">{value.model}</span></span>
        </> : <span className="mp-label muted">不用（只按关键词找）</span>}
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <div className="mp-pop" ref={pop} style={pos}>
          <div className="mp-search">
            <Icon name="search" size={14} />
            <input autoFocus value={q} placeholder="搜索，或者直接填模型名字" onChange={e => setQ(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Escape') { e.stopPropagation(); setOpen(false) }
                if (e.key === 'Enter' && first) choose(first.p.name, first.items[0].id)
              }} />
          </div>
          <div className="mp-list">
            {allowNone && !lq && <button className={'mp-item' + (!value.provider ? ' on' : '')} onClick={() => choose('', '')}>
              <span className="mp-id">不用（只按关键词找）</span>{!value.provider && <Icon name="check" size={14} />}</button>}
            {groups.map(({ p, l, items, exact }) => (
              <div key={p.name} className="mp-group">
                <div className="mp-group-head">
                  <ProviderBadge preset={p.preset} title={p.title} size={16} /><span>{p.title}</span>
                  <span className="grow" />
                  {l && !l.ok && <span className="mp-err" title={l.error}>{l.error}</span>}
                  <button className="mp-refresh" title="重新拉模型列表" onClick={() => load(p.name, true)}><Icon name="refresh" size={12} /></button>
                </div>
                {!l && <div className="mp-empty">加载中…</div>}
                {items.map(m => {
                  const on = p.name === value.provider && m.id === value.model
                  return (
                    <button key={m.id} className={'mp-item' + (on ? ' on' : '')} onClick={() => choose(p.name, m.id)}>
                      <span className="mp-id">{m.id}</span>
                      {kind === 'chat' && m.vision && <span title="能看图"><Icon name="eye" size={13} className="mp-vision" /></span>}
                      <span className="mp-price">{price(m)}</span>
                      {on ? <Icon name="check" size={14} /> : <span className="mp-sp" />}
                    </button>)
                })}
                {l?.ok && !items.length && !lq && <div className="mp-empty">{kind === 'embedding' ? '没认出 embedding 模型，可以在上面直接填名字' : '没有列出模型，可以在上面直接填名字'}</div>}
                {query && !exact && <button className="mp-item mp-custom" onClick={() => choose(p.name, query)}>
                  <span className="mp-id">在 {p.title} 用 “{query}”</span></button>}
              </div>
            ))}
          </div>
          <div className="mp-foot muted">单价是每百万 token 的输入 / 输出价，<Icon name="eye" size={12} /> 表示能看图</div>
        </div>
      )}
    </div>
  )
}
