import { useEffect, useMemo, useRef, useState } from 'react'
import { api, refreshApps, useStore } from '../store'
import type { App, CatalogItem } from '../types'
import { GLYPHS } from '../appIcons'

const HOW: Record<string, string> = { oauth: '登录即用', token: '填一个密钥', none: '免配置' }
const STATE: Record<string, [string, string]> = {
  connected: ['已连接', 'ok'], connecting: ['连接中…', 'busy'], login: ['等你授权…', 'busy'],
  needs_login: ['需要重新登录', 'warn'], off: ['已停用', ''], error: ['没连上', 'bad'],
}
const CONFIRM_NAMES: [string, string][] = [['auto', '写操作先问我'], ['all', '每次都问'], ['none', '都不问']]

// ---------- 图标：品牌色底 + 白色（或深色）图形 ----------
const lum = (hex: string) => {
  const n = parseInt(hex.replace('#', ''), 16)
  const c = [n >> 16, (n >> 8) & 255, n & 255].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4 })
  return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
}

// 图标写在应用清单里（osa/catalog/apps/*.yaml）：品牌图形 svg，或线条图标 glyph；自定义应用用插头
export function AppIcon({ item, name, size = 44 }: { item?: CatalogItem; name?: string; size?: number }) {
  const spec = item?.icon
  const glyph = GLYPHS[spec?.glyph || (item ? '' : 'plug')]
  const hex = spec?.color || '#5b5b63'
  const dark = lum(hex) < .02           // 黑色品牌（Notion、GitHub）用白底黑图
  const bg = dark ? '#fff' : hex
  const fg = dark ? hex : lum(hex) > .55 ? '#141414' : '#fff'
  return (
    <span className="app-icon" style={{ width: size, height: size, background: bg, color: fg, borderRadius: size * .26, fontSize: size * .44 }}>
      {spec?.svg ? <svg viewBox="0 0 24 24" width={size * .56} height={size * .56}><path d={spec.svg} fill="currentColor" /></svg>
        : glyph ? <svg viewBox="0 0 24 24" width={size * .56} height={size * .56} fill="none" stroke="currentColor" strokeWidth={2}
            strokeLinecap="round" strokeLinejoin="round" dangerouslySetInnerHTML={{ __html: glyph }} />
        : (item?.title || name || '?').slice(0, 1).toUpperCase()}
    </span>
  )
}

function StateTag({ app }: { app: App }) {
  const [label, cls] = STATE[app.state] || STATE.error
  return <span className={'m-state ' + cls}><i />{label}</span>
}

// ---------- 连接 ----------
const standalone = () => matchMedia('(display-mode: standalone)').matches || (navigator as any).standalone

// 登录授权：在弹窗里打开对方的授权页，关掉弹窗后看结果。fresh：这次是新添加的，没连上就撤掉。
// before：打开授权页之前要先做的事（保存配置之类）。弹窗必须在点击时同步打开，不然会被浏览器拦掉
async function oauthConnect(name: string, fresh: boolean, done: (err?: string) => void, before?: () => Promise<void>) {
  const url = `/oauth/start/${encodeURIComponent(name)}`
  const w = standalone() ? null : window.open(before ? '' : url, 'osa-oauth', 'width=540,height=720')
  if (before) {
    try { await before() } catch (e: any) { w?.close(); return done(e.message) }
  }
  if (!w) { location.href = url; return }
  if (before) w.location.href = url
  const timer = setInterval(async () => {
    if (!w.closed) return
    clearInterval(timer)
    await new Promise(r => setTimeout(r, 600))
    const a: App | undefined = (await refreshApps()).apps.find((x: App) => x.name === name)
    if (!a || a.connected || a.state === 'connecting') return done()
    if (a.state === 'login') await api(`/api/apps/${name}/cancel`, { method: 'POST' }).catch(() => {})
    if (fresh) await api(`/api/apps/${name}`, { method: 'DELETE' }).catch(() => {})
    done(a.status && !/取消|没有完成/.test(a.status) ? a.status : '没有完成授权')
  }, 500)
}

// ---------- 页面 ----------
export default function Market() {
  const apps = useStore(s => s.apps)
  const catalog = useStore(s => s.catalog)
  const categories = useStore(s => s.categories)
  const catName = useMemo(() => Object.fromEntries(categories), [categories])
  const [cat, setCat] = useState('all')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<string | null>(null)        // 打开详情的应用（市场 id 或应用名）
  const [custom, setCustom] = useState<{ app?: App } | null>(null)
  const [quickErr, setQuickErr] = useState('')
  useEffect(() => { refreshApps() }, [])
  const byName = useMemo(() => Object.fromEntries(apps.map(a => [a.name, a])), [apps])
  const itemOf = (a: App) => catalog.find(c => c.id === (a.catalog || a.name))
  const list = catalog.filter(c => (cat === 'all' || c.category === cat) &&
    (!q || (c.title + c.desc + c.id + (catName[c.category] || '')).toLowerCase().includes(q.toLowerCase())))

  const quick = (c: CatalogItem) => {
    setQuickErr('')
    if (c.kind === 'oauth') oauthConnect(c.id, true, err => { if (err) { setQuickErr(err); setOpen(c.id) } })
    else setOpen(c.id)
  }

  if (custom) return (
    <main className="chat market"><header className="chat-head"><span className="head-name">应用</span></header>
      <div className="scroller"><div className="m-body"><CustomEditor app={custom.app} onDone={() => setCustom(null)} /></div></div>
    </main>
  )
  const openApp = open ? byName[open] : undefined
  const openItem = open ? catalog.find(c => c.id === (openApp?.catalog || open)) : undefined
  return (
    <main className="chat market">
      <header className="chat-head">
        <span className="head-name">应用</span>
        <input className="m-search" type="search" placeholder="搜索应用" value={q} onChange={e => setQ(e.target.value)} />
      </header>
      <div className="scroller"><div className="m-body">
        <section className="m-hero">
          <h2>让 Bot 用上你的应用</h2>
          <p className="muted">连上以后，Bot 能替你查资料、记笔记、整理文件、发邮件。聊天时输入 <code>@</code> 可以指定用哪个。</p>
        </section>

        {apps.length > 0 && <section>
          <h4 className="m-h">我的应用</h4>
          <div className="m-mine">
            {apps.map(a => (
              <button key={a.name} className="m-mine-item" onClick={() => setOpen(a.name)}>
                <AppIcon item={itemOf(a)} name={a.name} size={32} />
                <span className="m-mine-text"><b>{itemOf(a)?.title || '@' + a.name}</b><StateTag app={a} /></span>
              </button>))}
          </div>
        </section>}

        <div className="m-cats">
          {[['all', '全部'], ...categories].map(([k, v]) => <button key={k} className={cat === k ? 'on' : ''} onClick={() => setCat(k)}>{v}</button>)}
        </div>
        <div className="m-grid">
          {list.map(c => {
            const a = byName[c.id]
            return (
              <div key={c.id} className="m-card" role="button" tabIndex={0} onClick={() => setOpen(c.id)}
                onKeyDown={e => e.key === 'Enter' && setOpen(c.id)}>
                <div className="m-card-top">
                  <AppIcon item={c} />
                  <div className="m-card-name"><b>{c.title}</b><span>{c.by}</span></div>
                </div>
                <p className="m-card-desc">{c.desc}</p>
                <div className="m-card-foot">
                  <span className="m-how">{c.how || HOW[c.kind]}</span>
                  {a ? <StateTag app={a} />
                    : <button className="btn small m-get" onClick={e => { e.stopPropagation(); quick(c) }}>连接</button>}
                </div>
              </div>)
          })}
          {!list.length && <div className="muted m-none">没有找到“{q}”。可以添加自定义应用。</div>}
        </div>
        <p className="muted small m-foot">
          找不到想要的？任何 MCP server 都能接：<button className="link-btn" onClick={() => setCustom({})}>添加自定义应用</button>
        </p>
      </div></div>
      {open && (openItem || openApp) && <Sheet item={openItem} app={openApp} initErr={quickErr} onClose={() => { setOpen(null); setQuickErr('') }}
        onEdit={() => { setOpen(null); setCustom({ app: openApp }) }} />}
    </main>
  )
}

// ---------- 详情 ----------
function Sheet({ item, app, initErr, onClose, onEdit }: { item?: CatalogItem; app?: App; initErr: string; onClose: () => void; onEdit: () => void }) {
  const name = app?.name || item!.id
  const categories = useStore(s => s.categories)
  const catName = useMemo(() => Object.fromEntries(categories), [categories])
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState(initErr)
  const [rekey, setRekey] = useState(false)
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    addEventListener('keydown', h)
    return () => removeEventListener('keydown', h)
  }, [])
  const run = async (key: string, f: () => Promise<any>) => {
    setBusy(key); setErr('')
    try { await f(); await refreshApps() } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const login = (fresh: boolean) => { setBusy('login'); setErr(''); oauthConnect(name, fresh, e => { setBusy(''); setErr(e || '') }) }
  const remove = () => confirm(`断开 ${item?.title || name}？保存的密钥和授权会一起删掉。`) &&
    run('remove', () => api(`/api/apps/${name}`, { method: 'DELETE' }))
  const showForm = !app || rekey
  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal m-sheet" onMouseDown={e => e.stopPropagation()}>
        <div className="m-sheet-head">
          <AppIcon item={item} name={name} size={56} />
          <div className="m-sheet-title">
            <h3>{item?.title || '@' + name}</h3>
            <span className="muted small">{item ? `${catName[item.category] || ''} · ${item.by}` : '自定义应用'}</span>
          </div>
          <button className="icon-btn" onClick={onClose}>✕</button>
        </div>
        <div className="m-sheet-body">
          {app && <div className="m-status">
            <StateTag app={app} />
            <span className="muted small">{app.connected ? `${app.tools.length} 个工具 · 聊天里用 @${app.name}` : app.status}</span>
          </div>}

          {item && app && item.kind === 'oauth' && !app.oauth && !app.catalog && <div className="m-connect">
            <p>这是之前用 token 接的 {item.title}。现在可以直接用账号登录，以后不用再管 token。</p>
            <button className="btn primary m-big" disabled={!!busy} onClick={() => {
              setBusy('login'); setErr('')
              oauthConnect(name, true, e => { setBusy(''); setErr(e || '') }, () => api(`/api/apps/${name}`, { method: 'DELETE' }))
            }}>{busy === 'login' ? '等你在弹窗里授权…' : `改用 ${item.title} 账号登录`}</button>
          </div>}

          {item && showForm && <ConnectForm item={item} busy={busy} rekey={rekey}
            onLogin={() => login(!app)}
            onSubmit={values => run('install', async () => {
              if (app) await api(`/api/apps/${name}`, { method: 'DELETE' })
              await api(`/api/market/${item.id}`, { method: 'POST', body: { values } })
              setRekey(false)
            })} />}
          {err && <div className="error-line">{err}</div>}

          {app && <div className="m-actions">
            {app.oauth && app.state === 'needs_login' &&
              <button className="btn primary" disabled={!!busy} onClick={() => login(false)}>{busy === 'login' ? '等你授权…' : '重新登录'}</button>}
            {app.state === 'error' &&
              <button className="btn primary" disabled={!!busy} onClick={() => run('re', () => api(`/api/apps/${name}/reconnect`, { method: 'POST' }))}>{busy === 're' ? '连接中…' : '重试'}</button>}
            {app.state === 'login' && <button className="btn" onClick={() => run('cancel', () => api(`/api/apps/${name}/cancel`, { method: 'POST' }))}>取消授权</button>}
            <label className="m-toggle"><span className="switch"><input type="checkbox" checked={app.enabled} disabled={!!busy}
              onChange={e => run('toggle', () => api(`/api/apps/${name}/${e.target.checked ? 'enable' : 'disable'}`, { method: 'POST' }))} /><span /></span>
              {busy === 'toggle' ? '…' : app.enabled ? '启用中' : '已停用'}</label>
            <span className="grow" />
            {item?.kind === 'token' && !rekey && <button className="ghost-btn" onClick={() => setRekey(true)}>换个密钥</button>}
            <button className="ghost-btn danger-text" disabled={!!busy} onClick={remove}>断开</button>
          </div>}

          {item && <>
            <p className="m-about">{item.about || item.desc}</p>
            {item.can.length > 0 && <><h4 className="m-h">能做什么</h4><ul className="m-can">{item.can.map(x => <li key={x}>{x}</li>)}</ul></>}
            {item.example && <><h4 className="m-h">试试这样说</h4><div className="m-example">{item.example}</div></>}
          </>}

          {app && <Advanced app={app} onEdit={onEdit} />}
        </div>
      </div>
    </div>
  )
}

function ConnectForm({ item, busy, rekey, onLogin, onSubmit }: {
  item: CatalogItem; busy: string; rekey: boolean; onLogin: () => void; onSubmit: (v: Record<string, string>) => void
}) {
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(item.fields.map(f => [f.key, f.default || ''])))
  const first = useRef<HTMLInputElement>(null)
  useEffect(() => { first.current?.focus() }, [])
  if (item.kind === 'oauth') return (
    <div className="m-connect">
      <button className="btn primary m-big" disabled={!!busy} onClick={onLogin}>{busy === 'login' ? '等你在弹窗里授权…' : `用 ${item.title} 账号登录`}</button>
      <p className="muted small">会打开 {item.title} 的授权页面，选好允许访问的内容就连上了，不用复制任何密钥。</p>
      {item.note && <p className="m-note">{item.note}</p>}
    </div>
  )
  const ready = item.fields.every(f => f.optional || values[f.key]?.trim())
  return (
    <form className="m-connect" onSubmit={e => { e.preventDefault(); ready && onSubmit(values) }}>
      {item.steps.length > 0 && <ol className="m-steps">{item.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
      {item.link && <a className="btn m-link" href={item.link} target="_blank" rel="noreferrer">{item.link_label || `打开 ${item.title} 设置`} ↗</a>}
      {item.fields.map((f, i) => (
        <label className="field" key={f.key}>{f.label}
          <input ref={i === 0 ? first : undefined} type={f.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
            value={values[f.key] || ''} placeholder={f.placeholder} onChange={e => setValues({ ...values, [f.key]: e.target.value })} />
        </label>))}
      {item.note && <p className="m-note">{item.note}</p>}
      <button className="btn primary m-big" type="submit" disabled={!ready || !!busy}>
        {busy === 'install' ? (item.remote ? '连接中…' : '连接中…第一次要下载，可能要一两分钟') : rekey ? '换好了，重新连接' : item.kind === 'none' ? '添加' : '连接'}
      </button>
      {item.fields.some(f => f.secret) && <p className="muted small">密钥加密存在你自己的服务器上，不会出现在聊天记录里。</p>}
    </form>
  )
}

function Advanced({ app, onEdit }: { app: App; onEdit: () => void }) {
  const [deny, setDeny] = useState(app.deny.join(', '))
  const [msg, setMsg] = useState('')
  const patch = async (body: any) => {
    setMsg('')
    try { await api(`/api/apps/${app.name}`, { method: 'PATCH', body }); await refreshApps(); setMsg('已保存') }
    catch (e: any) { setMsg(e.message) }
  }
  const confirmRule = typeof app.confirm === 'string' ? app.confirm : 'auto'
  return (
    <details className="m-adv">
      <summary>高级设置</summary>
      <div className="field">要不要先问我
        <div className="seg">{CONFIRM_NAMES.map(([k, v]) =>
          <button key={k} className={confirmRule === k ? 'on' : ''} onClick={() => patch({ confirm: k })}>{v}</button>)}</div>
        <span className="muted small">“写操作”指新建、修改、删除、发送这类会改东西的操作。单个工具的规则在 设置 → 确认规则 里改。</span>
      </div>
      {app.tools.length > 0 && <div className="field">工具（{app.tools.length} 个，带 * 的默认先问）
        <div className="m-tools">{app.tools.map(t => <code key={t}>{t}{app.confirm_tools.includes(t) ? '*' : ''}</code>)}</div>
      </div>}
      <label className="field">屏蔽的工具（名字里含这些词的工具不给 Bot 用，逗号分隔）
        <span className="env-row"><input value={deny} onChange={e => setDeny(e.target.value)} placeholder="delete, archive" />
          <button className="btn" onClick={() => patch({ deny: deny.split(/[,，\s]+/).filter(Boolean) })}>保存</button></span>
      </label>
      {msg && <span className="muted small">{msg}</span>}
      <button className="link-btn small" onClick={onEdit}>查看或修改连接配置</button>
    </details>
  )
}

// ---------- 自定义应用：任意 MCP server ----------
function CustomEditor({ app, onDone }: { app?: App; onDone: () => void }) {
  const isNew = !app
  const [name, setName] = useState(app?.name || '')
  const [type, setType] = useState<'stdio' | 'http'>(app?.type || 'http')
  const [url, setUrl] = useState(app?.url || '')
  const [authKind, setAuthKind] = useState(app?.oauth ? 'oauth' : app?.headers.includes('Authorization') ? 'token' : 'none')
  const [token, setToken] = useState('')
  const [command, setCommand] = useState(app?.command || '')
  const [args, setArgs] = useState((app?.args || []).join('\n'))
  const [env, setEnv] = useState<[string, string][]>(Object.entries(app?.env || {}))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const save = async () => {
    setBusy(true); setErr('')
    const n = name.trim()
    const body: any = type === 'http'
      ? { type, url: url.trim(), oauth: authKind === 'oauth', ...(authKind === 'token' && token ? { token } : {}),
          ...(authKind !== 'token' ? { headers: {} } : {}), enabled: true }
      : { type, command, args: args.split('\n').map(x => x.trim()).filter(Boolean), env: Object.fromEntries(env.filter(([k]) => k.trim())), enabled: true }
    const put = () => api(`/api/apps/${n}`, { method: 'PUT', body })
    if (type === 'http' && authKind === 'oauth') {   // 保存后在弹窗里登录授权
      return oauthConnect(n, false, async e => { setBusy(false); await refreshApps(); e ? setErr(e) : onDone() }, put)
    }
    try {
      const r: App = await put()
      await refreshApps()
      if (!r.connected) { setErr(r.status || '没连上'); return }
      onDone()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const remove = async () => {
    if (!app || !confirm(`删除应用 @${app.name}？`)) return
    await api(`/api/apps/${app.name}`, { method: 'DELETE' }); await refreshApps(); onDone()
  }
  return (
    <div className="app-editor">
      <button className="ghost-btn back" onClick={onDone}>← 返回应用市场</button>
      <h3>{isNew ? '添加自定义应用' : `@${app.name}`}</h3>
      <p className="muted small">应用就是 MCP server。远程的填地址，本机的填启动命令。{app?.status && <><br />状态：{app.status}</>}</p>
      <label className="field">名字（聊天里用 @{name || '名字'} 指定）
        <input value={name} disabled={!isNew} onChange={e => setName(e.target.value.toLowerCase())} placeholder="例如 mytools" /></label>
      <div className="field">连接方式
        <div className="seg"><button className={type === 'http' ? 'on' : ''} onClick={() => setType('http')}>远程地址</button>
          <button className={type === 'stdio' ? 'on' : ''} onClick={() => setType('stdio')}>本机命令</button></div>
      </div>
      {type === 'http' ? <>
        <label className="field">地址（Streamable HTTP）<input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/mcp" /></label>
        <div className="field">登录方式
          <div className="seg">{[['none', '不用'], ['oauth', '登录授权（OAuth）'], ['token', 'Token']].map(([k, v]) =>
            <button key={k} className={authKind === k ? 'on' : ''} onClick={() => setAuthKind(k)}>{v}</button>)}</div>
        </div>
        {authKind === 'token' && <label className="field">Token（放进 Authorization: Bearer 请求头）
          <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}
            placeholder={app?.headers.includes('Authorization') ? '已保存在保险库，留空则不修改' : '加密保存到保险库'} /></label>}
      </> : <>
        <label className="field">启动命令<input value={command} onChange={e => setCommand(e.target.value)} placeholder="npx / uvx / 可执行文件路径" /></label>
        <label className="field">参数（每行一个）<textarea rows={3} value={args} onChange={e => setArgs(e.target.value)} /></label>
        <div className="field">环境变量 <span className="muted small">值可以写 <code>vault:名字</code>（取保险库）或 <code>env:名字</code>（取服务的环境变量）</span>
          {env.map(([k, v], i) => (
            <div className="env-row" key={i}>
              <input value={k} placeholder="变量名" onChange={e => setEnv(env.map((x, j) => j === i ? [e.target.value, x[1]] : x))} />
              <input value={v} placeholder="值" onChange={e => setEnv(env.map((x, j) => j === i ? [x[0], e.target.value] : x))} />
              <button className="icon-btn" onClick={() => setEnv(env.filter((_, j) => j !== i))}>✕</button>
            </div>))}
          <button className="btn small-btn" onClick={() => setEnv([...env, ['', '']])}>+ 添加变量</button>
        </div>
      </>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        {!isNew && <button className="btn danger" onClick={remove}>删除</button>}
        <span className="grow" />
        <button className="btn primary" disabled={busy || !name || (type === 'http' ? !url : !command)} onClick={save}>
          {busy ? '连接中…' : '保存并连接'}</button>
      </div>
    </div>
  )
}
