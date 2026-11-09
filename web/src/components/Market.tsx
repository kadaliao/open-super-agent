import { useEffect, useMemo, useRef, useState } from 'react'
import { api, refreshApps, useStore } from '../store'
import type { App, CatalogItem } from '../types'
import { BrandIcon } from './BrandIcon'
import { t, tc, tn } from '../i18n'
import { ask } from './Dialog'
import { desktop } from '../desktop'
import { useBack } from '../swipe'

const HOW: Record<string, string> = { oauth: t('登录即用'), token: t('填一个密钥'), none: t('免配置') }
// 登录授权的应用也可能要先填几项（比如自己建的 OAuth 客户端），填好再登录
const needsFields = (c: CatalogItem) => c.fields.some(f => !f.optional)
export const howOf = (c: CatalogItem) => c.how || (c.kind === 'oauth' && needsFields(c) ? t('填好后登录') : HOW[c.kind])
const STATE: Record<string, [string, string]> = {
  connected: [t('已连接'), 'ok'], connecting: [t('连接中…'), 'busy'], login: [t('等你授权…'), 'busy'],
  needs_login: [t('需要重新登录'), 'warn'], off: [t('已停用'), ''], error: [t('没连上'), 'bad'],
}
const CONFIRM_NAMES: [string, string][] = [['auto', t('写操作先问我')], ['all', t('每次都问')], ['none', t('都不问')]]

// 图标写在应用清单里（osa/catalog/apps/*.yaml）：品牌图形 svg，或线条图标 glyph；自定义应用用插头
export function AppIcon({ item, name, size = 44 }: { item?: CatalogItem; name?: string; size?: number }) {
  const spec = item?.icon
  return <BrandIcon color={spec?.color} d={spec?.svg} glyph={spec?.glyph || (item ? '' : 'plug')} letter={item?.title || name} size={size} />
}

export function StateTag({ app }: { app: App }) {
  const [label, cls] = STATE[app.state] || STATE.error
  return <span className={'m-state ' + cls}><i />{label}</span>
}

// ---------- 连接 ----------
const standalone = () => matchMedia('(display-mode: standalone)').matches || (navigator as any).standalone

// 登录授权：在弹窗里打开对方的授权页，关掉弹窗后看结果。fresh：这次是新添加的，没连上就撤掉。
// before：打开授权页之前要先做的事（保存配置之类）。弹窗必须在点击时同步打开，不然会被浏览器拦掉
async function oauthConnect(name: string, fresh: boolean, done: (err?: string) => void, before?: () => Promise<void>) {
  const url = `/oauth/start/${encodeURIComponent(name)}`
  // 手机 App 里没有弹窗：在 App 弹出的一页里授权，那一页关掉（授权完成后会自己关）时看结果
  const sheet = desktop?.openSheet
  const w = standalone() || sheet ? null : window.open(before ? '' : url, 'osa-oauth', 'width=540,height=720')
  if (before) {
    try { await before() } catch (e: any) { w?.close(); return done(e.message) }
  }
  if (sheet) { await sheet(url).catch(() => {}); return check(name, fresh, done) }
  if (!w) { location.href = url; return }
  if (before) w.location.href = url
  // 授权完成页先发 osa-app 过来，再自己关掉。桌面 App 里弹窗不一定能自己关，所以收到消息也算完成
  let finished = false
  const finish = async () => {
    if (finished) return
    finished = true
    clearInterval(timer); window.removeEventListener('message', onMessage)
    setTimeout(() => { try { if (!w.closed) w.close() } catch { /* 已经关了 */ } }, 1500)
    await check(name, fresh, done)
  }
  const onMessage = (e: MessageEvent) => { if (e.origin === location.origin && e.data?.type === 'osa-app') finish() }
  window.addEventListener('message', onMessage)
  const timer = setInterval(() => { if (w.closed) finish() }, 500)
}

// 授权窗口关掉以后：连上了就好；没连上的撤掉登录，新添加的整个删掉
async function check(name: string, fresh: boolean, done: (err?: string) => void) {
  await new Promise(r => setTimeout(r, 600))
  const a: App | undefined = (await refreshApps()).apps.find((x: App) => x.name === name)
  if (!a || a.connected || a.state === 'connecting') return done()
  if (a.state === 'login') await api(`/api/apps/${name}/cancel`, { method: 'POST' }).catch(() => {})
  if (fresh) await api(`/api/apps/${name}`, { method: 'DELETE' }).catch(() => {})
  // 取消了、超时没完成的，统一说“没有完成授权”（服务端的状态文字可能是中文或英文）
  done(a.status && !/取消|没有完成|Authorization canceled|not completed/.test(a.status) ? a.status : t('没有完成授权'))   // i18n-ignore：匹配服务端的状态文字
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
  const editor = useRef<HTMLElement>(null)
  useBack(() => setCustom(null), editor, !!custom)
  const demo = useStore(s => !!s.settings?.security?.demo)   // 演示服务器：只能用应用市场里的应用
  useEffect(() => { refreshApps() }, [])
  const byName = useMemo(() => Object.fromEntries(apps.map(a => [a.name, a])), [apps])
  const itemOf = (a: App) => catalog.find(c => c.id === (a.catalog || a.name))
  const list = catalog.filter(c => (cat === 'all' || c.category === cat) &&
    (!q || (c.title + c.desc + c.id + (catName[c.category] || '')).toLowerCase().includes(q.toLowerCase())))

  const quick = (c: CatalogItem) => {
    setQuickErr('')
    if (c.kind === 'oauth' && !needsFields(c)) oauthConnect(c.id, true, err => { if (err) { setQuickErr(err); setOpen(c.id) } })
    else setOpen(c.id)
  }

  if (custom) return (
    <main className="chat market" ref={editor}><header className="chat-head"><span className="head-name">{t('应用')}</span></header>
      <div className="scroller"><div className="m-body"><CustomEditor app={custom.app} onDone={() => setCustom(null)} /></div></div>
    </main>
  )
  const openApp = open ? byName[open] : undefined
  const openItem = open ? catalog.find(c => c.id === (openApp?.catalog || open)) : undefined
  return (
    <main className="chat market">
      <header className="chat-head">
        <span className="head-name">{t('应用')}</span>
        <input className="m-search" type="search" placeholder={t('搜索应用')} value={q} onChange={e => setQ(e.target.value)} />
      </header>
      <div className="scroller"><div className="m-body">
        <section className="m-hero">
          <h2>{t('让 Bot 用上你的应用')}</h2>
          <p className="muted">{tn('连上以后，Bot 能替你查资料、记笔记、整理文件、发邮件。聊天时输入 {at} 可以指定用哪个。', { at: <code>@</code> })}</p>
        </section>

        {apps.length > 0 && <section>
          <h4 className="m-h">{t('我的应用')}</h4>
          <div className="m-mine">
            {apps.map(a => (
              <button key={a.name} className="m-mine-item" onClick={() => setOpen(a.name)}>
                <AppIcon item={itemOf(a)} name={a.name} size={32} />
                <span className="m-mine-text"><b>{itemOf(a)?.title || '@' + a.name}</b><StateTag app={a} /></span>
              </button>))}
          </div>
        </section>}

        <div className="m-cats">
          {[['all', t('全部')], ...categories].map(([k, v]) => <button key={k} className={cat === k ? 'on' : ''} onClick={() => setCat(k)}>{v}</button>)}
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
                  <span className="m-how">{howOf(c)}</span>
                  {a ? <StateTag app={a} />
                    : <button className="btn small m-get" onClick={e => { e.stopPropagation(); quick(c) }}>{t('连接')}</button>}
                </div>
              </div>)
          })}
          {!list.length && <div className="muted m-none">{demo ? t('没有找到“{q}”。', { q }) : t('没有找到“{q}”。可以添加自定义应用。', { q })}</div>}
        </div>
        {!demo && <p className="muted small m-foot">
          {tn('找不到想要的？任何 MCP server 都能接：{add}', { add: <button className="link-btn" onClick={() => setCustom({})}>{t('添加自定义应用')}</button> })}
        </p>}
      </div></div>
      {open && (openItem || openApp) && <Sheet item={openItem} app={openApp} initErr={quickErr} onClose={() => { setOpen(null); setQuickErr('') }}
        onEdit={() => { setOpen(null); setCustom({ app: openApp }) }} />}
    </main>
  )
}

// ---------- 详情 ----------
export function Sheet({ item, app, initErr, onClose, onEdit }: { item?: CatalogItem; app?: App; initErr: string; onClose: () => void; onEdit: () => void }) {
  const name = app?.name || item!.id
  const categories = useStore(s => s.categories)
  const catName = useMemo(() => Object.fromEntries(categories), [categories])
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState(initErr)
  const [rekey, setRekey] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  // 填的内容放在这里：登录失败时应用会被撤掉、表单重新出现，不用再粘贴一遍
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries((item?.fields || []).map(f => [f.key, f.default || ''])))
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    addEventListener('keydown', h)
    return () => removeEventListener('keydown', h)
  }, [])
  const run = async (key: string, f: () => Promise<any>) => {
    setBusy(key); setErr('')
    try { await f(); await refreshApps() } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  // values：先把填的内容存好（换内容时先删掉旧的），再打开授权弹窗；没登录成功就撤掉
  const login = (fresh: boolean, values?: Record<string, string>) => {
    setBusy('login'); setErr('')
    const withHint = (e: string) => item?.client && e === t('没有完成授权')
      ? t('{err}。如果对方页面说回调地址（redirect_uri）不对，把上面的回调地址原样填到对方那里再试', { err: e }) : e
    oauthConnect(name, fresh, e => { setBusy(''); setErr(e ? withHint(e) : ''); if (!e) setRekey(false) }, values && (async () => {
      if (app) await api(`/api/apps/${name}`, { method: 'DELETE' })
      await api(`/api/market/${item!.id}`, { method: 'POST', body: { values } })
    }))
  }
  const remove = async () => await ask(t('断开 {name}？保存的密钥和授权会一起删掉。', { name: item?.title || name }), { ok: t('断开'), danger: true }) &&
    run('remove', () => api(`/api/apps/${name}`, { method: 'DELETE' }))
  const showForm = !app || rekey
  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal m-sheet" onMouseDown={e => e.stopPropagation()}>
        <div className="m-sheet-head">
          <AppIcon item={item} name={name} size={56} />
          <div className="m-sheet-title">
            <h3>{item?.title || '@' + name}</h3>
            <span className="muted small">{item ? `${catName[item.category] || ''} · ${item.by}` : t('自定义应用')}</span>
          </div>
          <button className="icon-btn" onClick={onClose}>✕</button>
        </div>
        <div className="m-sheet-body">
          {app && <div className="m-status">
            <StateTag app={app} />
            <span className="muted small">{app.connected ? t('{n} 个工具 · 聊天里用 @{name}', { n: app.tools.length, name: app.name }) : app.status}</span>
          </div>}

          {item && app && item.kind === 'oauth' && !app.oauth && !app.catalog && <div className="m-connect">
            <p>{t('这是之前用 token 接的 {title}。现在可以直接用账号登录，以后不用再管 token。', { title: item.title })}</p>
            <button className="btn primary m-big" disabled={!!busy} onClick={() => {
              setBusy('login'); setErr('')
              oauthConnect(name, true, e => { setBusy(''); setErr(e || '') }, () => api(`/api/apps/${name}`, { method: 'DELETE' }))
            }}>{busy === 'login' ? t('等你在弹窗里授权…') : t('改用 {title} 账号登录', { title: item.title })}</button>
          </div>}

          {item && showForm && <ConnectForm item={item} busy={busy} rekey={rekey} values={values} setValues={setValues}
            onLogin={values => values ? login(true, values) : login(!app)}
            onSubmit={values => run('install', async () => {
              if (app) await api(`/api/apps/${name}`, { method: 'DELETE' })
              await api(`/api/market/${item.id}`, { method: 'POST', body: { values } })
              setRekey(false)
            })} />}
          {err && <div className="error-line">{err}</div>}

          {app && <div className="m-actions">
            {app.oauth && app.state === 'needs_login' &&
              <button className="btn primary" disabled={!!busy} onClick={() => login(false)}>{busy === 'login' ? t('等你授权…') : t('重新登录')}</button>}
            {app.state === 'error' &&
              <button className="btn primary" disabled={!!busy} onClick={() => run('re', () => api(`/api/apps/${name}/reconnect`, { method: 'POST' }))}>{busy === 're' ? t('连接中…') : t('重试')}</button>}
            {app.state === 'login' && <button className="btn" onClick={() => run('cancel', () => api(`/api/apps/${name}/cancel`, { method: 'POST' }))}>{t('取消授权')}</button>}
            <label className="m-toggle"><span className="switch"><input type="checkbox" checked={app.enabled} disabled={!!busy}
              onChange={e => run('toggle', () => api(`/api/apps/${name}/${e.target.checked ? 'enable' : 'disable'}`, { method: 'POST' }))} /><span /></span>
              {busy === 'toggle' ? '…' : app.enabled ? t('启用中') : t('已停用')}</label>
            <span className="grow" />
            {item?.kind === 'token' && !rekey && <button className="ghost-btn" onClick={() => setRekey(true)}>{t('换个密钥')}</button>}
            {item?.kind === 'oauth' && needsFields(item) && !rekey && <button className="ghost-btn" onClick={() => setRekey(true)}>{t('重新填写')}</button>}
            <button className="ghost-btn danger-text" disabled={!!busy} onClick={remove}>{t('断开')}</button>
          </div>}

          {item && <>
            <p className="m-about">{item.about || item.desc}</p>
            {item.can.length > 0 && <><h4 className="m-h">{t('能做什么')}</h4><ul className="m-can">{item.can.map(x => <li key={x}>{x}</li>)}</ul></>}
            {item.example && <><h4 className="m-h">{t('试试这样说')}</h4><div className="m-example">{item.example}</div></>}
          </>}

          {app && <Advanced app={app} onEdit={onEdit} />}
        </div>
      </div>
    </div>
  )
}

function ConnectForm({ item, busy, rekey, values, setValues, onLogin, onSubmit }: {
  item: CatalogItem; busy: string; rekey: boolean; values: Record<string, string>; setValues: (v: Record<string, string>) => void
  onLogin: (v?: Record<string, string>) => void; onSubmit: (v: Record<string, string>) => void
}) {
  const first = useRef<HTMLInputElement>(null)
  useEffect(() => { first.current?.focus() }, [])
  const oauth = item.kind === 'oauth'
  if (oauth && !needsFields(item)) return (
    <div className="m-connect">
      <button className="btn primary m-big" disabled={!!busy} onClick={() => onLogin()}>{busy === 'login' ? t('等你在弹窗里授权…') : t('用 {title} 账号登录', { title: item.title })}</button>
      <p className="muted small">{t('会打开 {title} 的授权页面，选好允许访问的内容就连上了，不用复制任何密钥。', { title: item.title })}</p>
      {item.note && <p className="m-note">{item.note}</p>}
    </div>
  )
  const ready = item.fields.every(f => f.optional || values[f.key]?.trim())
  return (
    <form className="m-connect" onSubmit={e => { e.preventDefault(); ready && (oauth ? onLogin(values) : onSubmit(values)) }}>
      {item.steps.length > 0 && <ol className="m-steps">{item.steps.map((s, i) => <li key={i}>{s}</li>)}</ol>}
      {item.link && <a className="btn m-link" href={item.link} target="_blank" rel="noreferrer">{item.link_label || t('打开 {title} 设置', { title: item.title })} ↗</a>}
      {item.copy && <CopyBox label={item.copy.label} text={item.copy.text} rows={8} />}
      {item.client && <Callback />}
      {item.fields.map((f, i) => (
        <label className="field" key={f.key}>{f.label}
          <input ref={i === 0 ? first : undefined} type={f.secret ? 'password' : 'text'} autoComplete="off" spellCheck={false}
            value={values[f.key] || ''} placeholder={f.placeholder} onChange={e => setValues({ ...values, [f.key]: e.target.value })} />
        </label>))}
      {item.note && <p className="m-note">{item.note}</p>}
      <button className="btn primary m-big" type="submit" disabled={!ready || !!busy}>
        {oauth ? (busy === 'login' ? t('等你在弹窗里授权…') : t('填好了，去 {title} 登录', { title: item.title }))
          : busy === 'install' ? (item.remote ? t('连接中…') : t('连接中…第一次要下载，可能要一两分钟')) : rekey ? t('换好了，重新连接') : item.kind === 'none' ? t('添加') : t('连接')}
      </button>
      {item.fields.some(f => f.secret) && <p className="muted small">{t('密钥加密存在你自己的服务器上，不会出现在聊天记录里。')}</p>}
    </form>
  )
}

// 只读的一段文字加“复制”按钮。text 里的 {callback} 换成回调地址
function CopyBox({ label, text, rows }: { label: string; text: string; rows?: number }) {
  const callback = useStore(s => s.oauthCallback)
  const value = text.split('{callback}').join(callback)
  const ref = useRef<HTMLInputElement & HTMLTextAreaElement>(null)
  const [copied, setCopied] = useState(false)
  const copy = () => {
    ref.current?.select()
    const ok = navigator.clipboard ? navigator.clipboard.writeText(value) : Promise.resolve(document.execCommand('copy'))
    ok.then(() => setCopied(true), () => {})
  }
  const btn = <button type="button" className="btn" onClick={copy}>{copied ? t('已复制') : t('复制')}</button>
  return (
    <div className="field">{label}
      {rows ? <><textarea ref={ref} readOnly rows={rows} value={value} className="m-copy" onFocus={e => e.target.select()} />{btn}</>
        : <span className="env-row"><input ref={ref} readOnly value={value} onFocus={e => e.target.select()} />{btn}</span>}
    </div>
  )
}

// 自己建 OAuth 客户端时，要把这个地址填到对方的“回调地址 / Redirect URL”里
function Callback() {
  const url = useStore(s => s.oauthCallback)
  return <>
    <CopyBox label={t('回调地址（Redirect URL）')} text="{callback}" />
    {/^http:/.test(url) && <span className="m-note">{t('这是 http 地址，不少服务（比如 Slack）只接受 https 的回调地址。先给服务配好 https（见部署文档），再从 https 地址打开这一页。')}</span>}
  </>
}

function Advanced({ app, onEdit }: { app: App; onEdit: () => void }) {
  const demo = useStore(s => !!s.settings?.security?.demo)
  const [deny, setDeny] = useState(app.deny.join(', '))
  const [msg, setMsg] = useState('')
  const patch = async (body: any) => {
    setMsg('')
    try { await api(`/api/apps/${app.name}`, { method: 'PATCH', body }); await refreshApps(); setMsg(t('已保存')) }
    catch (e: any) { setMsg(e.message) }
  }
  const confirmRule = typeof app.confirm === 'string' ? app.confirm : 'auto'
  return (
    <details className="m-adv">
      <summary>{t('高级设置')}</summary>
      <div className="field">{t('要不要先问我')}
        <div className="seg">{CONFIRM_NAMES.map(([k, v]) =>
          <button key={k} className={confirmRule === k ? 'on' : ''} onClick={() => patch({ confirm: k })}>{v}</button>)}</div>
        <span className="muted small">{t('“写操作”指新建、修改、删除、发送这类会改东西的操作。单个工具的规则在 设置 → 确认规则 里改。')}</span>
      </div>
      {app.tools.length > 0 && <div className="field">{t('工具（{n} 个，带 * 的默认先问）', { n: app.tools.length })}
        <div className="m-tools">{app.tools.map(x => <code key={x}>{x}{app.confirm_tools.includes(x) ? '*' : ''}</code>)}</div>
      </div>}
      <label className="field">{t('屏蔽的工具（名字里含这些词的工具不给 Bot 用，逗号分隔）')}
        <span className="env-row"><input value={deny} onChange={e => setDeny(e.target.value)} placeholder="delete, archive" />
          <button className="btn" onClick={() => patch({ deny: deny.split(/[,，\s]+/).filter(Boolean) })}>{t('保存')}</button></span>
      </label>
      {msg && <span className="muted small">{msg}</span>}
      {!demo && <button className="link-btn small" onClick={onEdit}>{t('查看或修改连接配置')}</button>}
    </details>
  )
}

// ---------- 自定义应用：任意 MCP server ----------
function CustomEditor({ app, onDone }: { app?: App; onDone: () => void }) {
  const isNew = !app
  // 服务器锁（security.apps: market）：本地应用的命令改不了，也不能新加。旧版服务没有 apps，按 local_exec 推
  const locked = useStore(s => (s.settings?.security?.apps ?? (s.settings?.security?.local_exec === false ? 'market' : 'any')) === 'market')
  const [name, setName] = useState(app?.name || '')
  const [type, setType] = useState<'stdio' | 'http' | 'sse'>(app?.type || 'http')
  const remote = type !== 'stdio'
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
    const body: any = remote
      ? { type, url: url.trim(), oauth: authKind === 'oauth', ...(authKind === 'token' && token ? { token } : {}),
          ...(authKind !== 'token' ? { headers: {} } : {}), enabled: true }
      : { type, command, args: args.split('\n').map(x => x.trim()).filter(Boolean), env: Object.fromEntries(env.filter(([k]) => k.trim())), enabled: true }
    const put = () => api(`/api/apps/${n}`, { method: 'PUT', body })
    if (remote && authKind === 'oauth') {   // 保存后在弹窗里登录授权
      return oauthConnect(n, false, async e => { setBusy(false); await refreshApps(); e ? setErr(e) : onDone() }, put)
    }
    try {
      const r: App = await put()
      await refreshApps()
      if (!r.connected) { setErr(r.status || t('没连上')); return }
      onDone()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const remove = async () => {
    if (!app || !(await ask(t('删除应用 @{name}？', { name: app.name }), { ok: t('删除'), danger: true }))) return
    await api(`/api/apps/${app.name}`, { method: 'DELETE' }); await refreshApps(); onDone()
  }
  return (
    <div className="app-editor">
      <button className="ghost-btn back" onClick={onDone}>← {t('返回应用市场')}</button>
      <h3>{isNew ? t('添加自定义应用') : `@${app.name}`}</h3>
      <p className="muted small">{t('应用就是 MCP server。远程的填地址，本机的填启动命令。')}{app?.status && <><br />{t('状态：{status}', { status: app.status })}</>}</p>
      <label className="field">{t('名字（聊天里用 @{name} 指定）', { name: name || tc('placeholder', '名字') })}
        <input value={name} disabled={!isNew} onChange={e => setName(e.target.value.toLowerCase())} placeholder={t('例如 mytools')} /></label>
      <div className="field">{t('连接方式')}
        <div className="seg"><button className={remote ? 'on' : ''} onClick={() => setType('http')}>{t('远程地址')}</button>
          {!(locked && isNew) && <button className={type === 'stdio' ? 'on' : ''} onClick={() => setType('stdio')}>{t('本机命令')}</button>}</div>
        {locked && <span className="muted small">{t('这台服务器不允许从网页上添加本机命令的应用，也不能改它们的启动命令、参数和环境变量')}</span>}
      </div>
      {remote ? <>
        <label className="field">{t('地址')}<input value={url} onChange={e => setUrl(e.target.value)} placeholder={type === 'sse' ? 'https://example.com/sse' : 'https://example.com/mcp'} /></label>
        <div className="field">{t('协议')}
          <div className="seg"><button className={type === 'http' ? 'on' : ''} onClick={() => setType('http')}>Streamable HTTP</button>
            <button className={type === 'sse' ? 'on' : ''} onClick={() => setType('sse')}>{t('SSE（旧版）')}</button></div>
          <span className="muted small">{t('对方文档里的地址以 /sse 结尾的，一般选 SSE。')}</span>
        </div>
        <div className="field">{t('登录方式')}
          <div className="seg">{[['none', t('不用')], ['oauth', t('登录授权（OAuth）')], ['token', 'Token']].map(([k, v]) =>
            <button key={k} className={authKind === k ? 'on' : ''} onClick={() => setAuthKind(k)}>{v}</button>)}</div>
        </div>
        {authKind === 'token' && <label className="field">{t('Token（放进 Authorization: Bearer 请求头）')}
          <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}
            placeholder={app?.headers.includes('Authorization') ? t('已保存在保险库，留空则不修改') : t('加密保存到保险库')} /></label>}
      </> : <>
        <label className="field">{t('启动命令')}<input value={command} disabled={locked} onChange={e => setCommand(e.target.value)} placeholder={t('npx / uvx / 可执行文件路径')} /></label>
        <label className="field">{t('参数（每行一个）')}<textarea rows={3} value={args} disabled={locked} onChange={e => setArgs(e.target.value)} /></label>
        <div className="field">{t('环境变量')} {!locked && <span className="muted small">{tn('值可以写 {vault}（取保险库）或 {env}（取服务的环境变量）', {
            vault: <code>vault:{tc('placeholder', '名字')}</code>, env: <code>env:{tc('placeholder', '名字')}</code> })}</span>}
          {env.map(([k, v], i) => (
            <div className="env-row" key={i}>
              <input value={k} disabled={locked} placeholder={t('变量名')} onChange={e => setEnv(env.map((x, j) => j === i ? [e.target.value, x[1]] : x))} />
              <input value={v} disabled={locked} placeholder={t('值')} onChange={e => setEnv(env.map((x, j) => j === i ? [x[0], e.target.value] : x))} />
              {!locked && <button className="icon-btn" onClick={() => setEnv(env.filter((_, j) => j !== i))}>✕</button>}
            </div>))}
          {!locked && <button className="btn small-btn" onClick={() => setEnv([...env, ['', '']])}>+ {t('添加变量')}</button>}
        </div>
      </>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        {!isNew && <button className="btn danger" onClick={remove}>{t('删除')}</button>}
        <span className="grow" />
        <button className="btn primary" disabled={busy || !name || (remote ? !url : !command || locked)} onClick={save}>
          {busy ? t('连接中…') : t('保存并连接')}</button>
      </div>
    </div>
  )
}
