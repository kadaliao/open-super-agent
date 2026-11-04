import { useEffect, useState } from 'react'
import { api, refreshApps, refreshLists, useStore } from '../store'
import type { App, Preset, Rule, Settings as S } from '../types'

const ROLE_NAMES: Record<string, string> = {
  router: '对话（Bot 本身）', worker: '后台任务', reviewer: 'Heavy 汇总审稿', filter: '定时任务的事件过滤',
}
const order = (r: string) => { const i = Object.keys(ROLE_NAMES).indexOf(r); return i < 0 ? 99 : i }
const LIMIT_NAMES: Record<string, [string, string]> = {
  daily_token_budget: ['每日 token 上限', '超过后不再派发新任务'],
  worker_max_steps: ['每个任务最多步数', '一步 = 一次模型调用'],
  task_timeout_s: ['任务超时（秒）', 'Watchdog 超时取消'],
  approval_timeout_s: ['确认等待时限（秒）', '超时按拒绝处理'],
}

export default function Settings({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState('models')
  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal wide" onMouseDown={e => e.stopPropagation()}>
        <div className="modal-tabs">
          {[['models', '模型'], ['limits', '限制与预算'], ['usage', '费用'], ['apps', '应用'], ['rules', '确认规则'], ['vault', '保险库'], ['triggers', '定时任务'], ['notify', '通知']].map(([k, v]) =>
            <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{v}</button>)}
          <span className="grow" /><button className="icon-btn" onClick={onClose}>✕</button>
        </div>
        <div className="modal-content">
          {tab === 'models' && <Models />}
          {tab === 'limits' && <Limits />}
          {tab === 'usage' && <Usage />}
          {tab === 'apps' && <Apps />}
          {tab === 'rules' && <Rules />}
          {tab === 'vault' && <Vault />}
          {tab === 'triggers' && <Triggers />}
          {tab === 'notify' && <Notifications />}
        </div>
      </div>
    </div>
  )
}

function useSave() {
  const [msg, setMsg] = useState('')
  const save = async (body: any) => {
    try { await api('/api/settings', { method: 'PUT', body }); await refreshLists(); setMsg('已保存，立即生效') }
    catch (e: any) { setMsg(e.message) }
    setTimeout(() => setMsg(''), 2000)
  }
  return { msg, save }
}

function Models() {
  const s = useStore(x => x.settings) as S
  const [roles, setRoles] = useState(s.roles)
  const [heavy, setHeavy] = useState(s.heavy_workers)
  const { msg, save } = useSave()
  const upd = (r: string, k: string, v: any) => setRoles({ ...roles, [r]: { ...roles[r], [k]: v } })
  return (
    <div>
      <p className="muted">每个角色可以用不同的模型。任何 OpenAI 兼容接口都行，提供方在 <code>config.yaml</code> 的 <code>providers</code> 里添加。</p>
      <div className="providers">
        {s.providers.map(p => <span key={p.name} className={'pill' + (p.key_set ? ' ok' : ' bad')} title={p.base_url}>
          {p.name} · {p.key_set ? '密钥已设置' : `缺少 ${p.key_env}`}</span>)}
      </div>
      <datalist id="models">{s.known_models.map(m => <option key={m} value={m} />)}</datalist>
      <table className="grid">
        <thead><tr><th>角色</th><th>提供方</th><th>模型</th><th>思考模式</th><th>思考强度</th></tr></thead>
        <tbody>{Object.entries(roles).sort(([a], [b]) => order(a) - order(b)).map(([r, v]) => (
          <tr key={r}>
            <td>{ROLE_NAMES[r] || r}</td>
            <td><select value={v.provider} onChange={e => upd(r, 'provider', e.target.value)}>{s.providers.map(p => <option key={p.name}>{p.name}</option>)}</select></td>
            <td><input list="models" value={v.model} onChange={e => upd(r, 'model', e.target.value)} /></td>
            <td><label className="switch"><input type="checkbox" checked={v.thinking !== false} onChange={e => upd(r, 'thinking', e.target.checked)} /><span /></label></td>
            <td><select value={v.reasoning_effort || ''} disabled={v.thinking === false} onChange={e => upd(r, 'reasoning_effort', e.target.value)}>
              <option value="">默认</option><option value="low">low</option><option value="high">high</option><option value="max">max</option></select></td>
          </tr>))}
        </tbody>
      </table>
      <label className="field inline">Heavy 模式并行数<input type="number" min={1} max={8} value={heavy} onChange={e => setHeavy(Number(e.target.value))} /></label>
      <div className="row-actions"><button className="btn primary" onClick={() => save({ roles, heavy_workers: heavy })}>保存</button><span className="muted small">{msg}</span></div>
    </div>
  )
}

function Limits() {
  const s = useStore(x => x.settings) as S
  const [lim, setLim] = useState(s.limits)
  const { msg, save } = useSave()
  return (
    <div>
      {Object.entries(LIMIT_NAMES).map(([k, [name, hint]]) => (
        <label key={k} className="field inline">{name}<input type="number" value={lim[k] ?? ''} onChange={e => setLim({ ...lim, [k]: Number(e.target.value) })} /><span className="muted small">{hint}</span></label>
      ))}
      <div className="row-actions"><button className="btn primary" onClick={() => save({ limits: lim })}>保存</button><span className="muted small">{msg}</span></div>
    </div>
  )
}

function Usage() {
  const [u, setU] = useState<any>(null)
  const bots = useStore(x => x.bots)
  useEffect(() => { api('/api/usage').then(setU); const t = setInterval(() => api('/api/usage').then(setU), 8000); return () => clearInterval(t) }, [])
  if (!u) return <div className="muted">加载中…</div>
  const f = (v: number) => u.currency + v.toFixed(v < 1 ? 3 : 2)
  const max = Math.max(...u.by_day.map((d: any) => d.cost), 1e-9)
  return (
    <div className="usage">
      <div className="stat-row">
        <div className="stat"><div className="muted small">今天</div><div className="big">{f(u.today.cost)}</div><div className="muted small">{u.today.calls} 次调用 · 缓存命中 {(u.today.cache_rate * 100).toFixed(0)}%</div></div>
        <div className="stat"><div className="muted small">近 7 天</div><div className="big">{f(u.week.cost)}</div></div>
        <div className="stat"><div className="muted small">近 30 天</div><div className="big">{f(u.month.cost)}</div></div>
      </div>
      <h4>近 30 天每日</h4>
      <div className="bars">{u.by_day.map((d: any) => <div key={d.day} title={`${d.day}  ${f(d.cost)}`} style={{ height: `${Math.max(2, (d.cost / max) * 100)}%` }} />)}</div>
      <div className="two-col">
        <div><h4>按角色</h4>{u.by_role.map((r: any) => <div className="kv" key={r.role + r.model}><span>{ROLE_NAMES[r.role] || r.role} · {r.model}</span><span>{f(r.cost)}</span></div>)}</div>
        <div><h4>按任务（最近 20 个）</h4>{u.by_task.map((t: any) => <div className="kv" key={t.id}><span>#{t.id} {t.goal}</span><span>{f(t.cost)}</span></div>)}</div>
      </div>
      <p className="muted small">按 DeepSeek 官网的高峰和空闲两档价格计算，单价在 config.yaml 的 pricing 里。{bots.length} 个 Bot 共用一个预算。</p>
    </div>
  )
}

function Triggers() {
  const [s, setS] = useState<any>(null)
  const bots = useStore(x => x.bots)
  useEffect(() => { api('/api/status').then(setS) }, [])
  if (!s) return <div className="muted">加载中…</div>
  return (
    <div>
      <h4>所有定时任务</h4>
      {s.routines.length ? s.routines.map((r: any) => (
        <div className="kv" key={r.id}><span>{bots.find(b => b.id === r.bot_id)?.name} · {r.name}（{r.trigger}）</span>
          <span className="muted">{r.enabled ? '运行中' : '已暂停'}</span></div>
      )) : <div className="muted small">暂无</div>}
      <p className="muted small">定时任务属于各个 Bot：在对话里说“每天 9 点帮我……”，或者在右栏 Bot 详情的“定时”里新建和管理。
        webhook 类的地址是 POST /hooks/名字。</p>
    </div>
  )
}

// 推送：在这台设备上订阅，没开着页面时，需要你处理的事推送过来
const b64ToBytes = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0))

function Notifications() {
  const [info, setInfo] = useState<{ key: string; count: number } | null>(null)
  const [sub, setSub] = useState<PushSubscription | null>(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
  const ios = /iPhone|iPad/.test(navigator.userAgent)
  const standalone = matchMedia('(display-mode: standalone)').matches || (navigator as any).standalone
  useEffect(() => {
    api('/api/push').then(setInfo).catch((e: any) => setMsg(e.message))
    if (supported && isSecureContext) navigator.serviceWorker.ready.then(r => r.pushManager.getSubscription()).then(setSub)
  }, [])
  const on = async () => {
    setBusy(true); setMsg('')
    try {
      if (await Notification.requestPermission() !== 'granted') throw new Error('没有通知权限，请在浏览器或系统设置里允许这个网站发通知')
      const reg = await navigator.serviceWorker.register('/sw.js')
      await navigator.serviceWorker.ready
      const s = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(info!.key) })
      const r = await api('/api/push/subscribe', { method: 'POST', body: { subscription: s.toJSON() } })
      setSub(s); setInfo({ ...info!, count: r.count }); localStorage.setItem('osa.push', '1'); setMsg('已开启')
    } catch (e: any) { setMsg(e.message) }
    setBusy(false)
  }
  const off = async () => {
    if (!sub) return
    const r = await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } })
    await sub.unsubscribe().catch(() => {})
    setSub(null); setInfo({ ...info!, count: r.count }); localStorage.removeItem('osa.push'); setMsg('已关闭')
  }
  const test = async () => {
    setMsg('')
    try { const r = await api('/api/push/test', { method: 'POST' }); setMsg(r.results.every((x: string) => x === 'ok') ? '已发出，几秒内应该收到' : r.results.join('；')) }
    catch (e: any) { setMsg(e.message) }
  }
  return (
    <div>
      <p className="muted">没开着这个页面时（比如手机锁屏），需要你确认、请你帮忙、任务做完和 Bot 的回复会推送到开启了推送的设备。页面开着时不推。</p>
      {!isSecureContext ? <p className="error-line">推送需要 https 访问（本机 localhost 也行）。手机上建议用 Tailscale 的 https 地址，见文档“远程访问”。</p>
        : !supported ? <p className="error-line">这个浏览器不支持推送。{ios ? '' : '请换用 Chrome、Edge、Firefox 或 Safari。'}</p>
        : ios && !standalone ? <p className="muted">iPhone / iPad：先在 Safari 里点“分享 → 添加到主屏幕”，从主屏幕打开后再来这里开启。</p>
        : <div className="row-actions">
            {sub ? <><span className="pill ok">这台设备已开启</span><button className="btn" onClick={test}>发一条测试</button><button className="btn" onClick={off}>关闭</button></>
              : <button className="btn primary" disabled={busy || !info} onClick={on}>{busy ? '开启中…' : '在这台设备上开启推送'}</button>}
          </div>}
      {info && <p className="muted small">已开启推送的设备：{info.count} 台</p>}
      {msg && <p className="muted small">{msg}</p>}
      <h4>添加到主屏幕</h4>
      <p className="muted small">手机上可以把它当应用用：Safari 点“分享 → 添加到主屏幕”，Chrome 点菜单里的“添加到主屏幕”或“安装应用”。</p>
    </div>
  )
}

const CONFIRM_NAMES: Record<string, string> = { auto: '自动（写操作先问）', all: '全部先问', none: '全部不问' }

function Apps() {
  const apps = useStore(x => x.apps)
  const [presets, setPresets] = useState<Record<string, Preset>>({})
  const [edit, setEdit] = useState<{ name: string; preset?: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { refreshApps().then(r => setPresets(r.presets)) }, [])
  const act = async (name: string, action: string) => {
    setBusy(name); setErr('')
    try {
      if (action === 'delete') { if (!confirm(`删除应用 ${name}？`)) return; await api(`/api/apps/${name}`, { method: 'DELETE' }) }
      else await api(`/api/apps/${name}/${action}`, { method: 'POST' })
      await refreshApps()
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  if (edit) return <AppEditor app={apps.find(a => a.name === edit.name)} name={edit.name} preset={edit.preset ? presets[edit.preset] : undefined}
    onDone={() => { setEdit(null); refreshApps() }} />
  const have = new Set(apps.map(a => a.name))
  return (
    <div>
      <p className="muted">应用就是 MCP server。连接后，Bot 派的后台任务就能用它的工具；聊天里输入 <code>@应用名</code> 指定用哪个。</p>
      <h4>已添加</h4>
      {apps.length === 0 && <div className="muted small">还没有应用，从下面挑一个。</div>}
      {apps.map(a => (
        <div className="app-row" key={a.name}>
          <div className="app-main">
            <div><b>@{a.name}</b> <span className={'pill' + (a.connected ? ' ok' : a.status.includes('失败') || a.status.includes('缺少') ? ' bad' : '')}>{a.connected ? '已连接' : a.enabled ? '未连接' : '已停用'}</span></div>
            <div className="muted small">{a.status || '—'}</div>
            {a.connected && <div className="muted small app-tools" title={a.tools.join(', ')}>工具：{a.tools.map(t => a.confirm_tools.includes(t) ? `${t}*` : t).join('、')}（* 默认先问）</div>}
          </div>
          <div className="app-actions">
            {a.enabled
              ? <><button className="btn" disabled={busy === a.name} onClick={() => act(a.name, 'reconnect')}>{busy === a.name ? '连接中…' : '重连'}</button>
                  <button className="btn" disabled={busy === a.name} onClick={() => act(a.name, 'disable')}>停用</button></>
              : <button className="btn primary" disabled={busy === a.name} onClick={() => act(a.name, 'enable')}>{busy === a.name ? '连接中…' : '启用'}</button>}
            <button className="btn" onClick={() => setEdit({ name: a.name })}>配置</button>
            <button className="btn danger" onClick={() => act(a.name, 'delete')}>删除</button>
          </div>
        </div>
      ))}
      {err && <div className="error-line">{err}</div>}
      <h4>添加应用</h4>
      <div className="preset-grid">
        {Object.entries(presets).map(([k, p]) => (
          <button key={k} className="preset" disabled={have.has(k)} onClick={() => setEdit({ name: k, preset: k })}>
            <b>{p.title}</b><span className="muted small">{have.has(k) ? '已添加' : p.desc}</span>
          </button>))}
        <button className="preset" onClick={() => setEdit({ name: '' })}><b>自定义</b><span className="muted small">任意 stdio MCP server</span></button>
      </div>
    </div>
  )
}

function AppEditor({ app, name: initName, preset, onDone }: { app?: App; name: string; preset?: Preset; onDone: () => void }) {
  const isNew = !app
  const [name, setName] = useState(initName)
  const [command, setCommand] = useState(app?.command || preset?.command || '')
  const presetArgs = [...(preset?.args || []), ...(preset?.fields.filter(f => f.kind === 'arg').map(f => f.default || '') || [])]
  const [args, setArgs] = useState((app?.args || presetArgs).join('\n'))
  const [env, setEnv] = useState<[string, string][]>(Object.entries(app?.env || {}))
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [deny, setDeny] = useState((app?.deny || preset?.deny || []).join(', '))
  const [confirmRule, setConfirmRule] = useState(typeof app?.confirm === 'string' ? app.confirm : 'auto')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const secretFields = preset?.fields.filter(f => f.kind === 'env') || []
  const save = async () => {
    setBusy(true); setErr('')
    try {
      const r: App = await api(`/api/apps/${name.trim()}`, { method: 'PUT', body: {
        command, args: args.split('\n').map(x => x.trim()).filter(Boolean),
        env: Object.fromEntries(env.filter(([k]) => k.trim())), secrets, enabled: true,
        deny: deny.split(/[,，\s]+/).filter(Boolean), confirm: confirmRule } })
      if (!r.connected) { setErr(r.status || '没连上'); return }
      onDone()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="app-editor">
      <h4>{isNew ? `添加${preset ? ' ' + preset.title : '应用'}` : `配置 @${name}`}</h4>
      <label className="field">应用名（聊天里用 @{name || '名字'} 指定）<input value={name} disabled={!isNew} onChange={e => setName(e.target.value.toLowerCase())} placeholder="例如 files" /></label>
      {secretFields.map(f => (
        <label className="field" key={f.key}>{f.label}
          <input type="password" autoComplete="off" value={secrets[f.key] || ''} onChange={e => setSecrets({ ...secrets, [f.key]: e.target.value })}
            placeholder={app?.env[f.key] ? '已保存在保险库，留空则不修改' : '加密保存到保险库，不会进入聊天记录'} />
        </label>))}
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
      <label className="field">屏蔽的工具（工具名包含这些词就不开放，逗号分隔）<input value={deny} onChange={e => setDeny(e.target.value)} placeholder="delete, archive" /></label>
      <label className="field">默认确认策略
        <select value={confirmRule} onChange={e => setConfirmRule(e.target.value)}>{Object.entries(CONFIRM_NAMES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
      </label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn" onClick={onDone}>返回</button>
        <button className="btn primary" disabled={busy || !name || !command} onClick={save}>{busy ? '连接中…（第一次可能要下载）' : '保存并连接'}</button>
      </div>
    </div>
  )
}

function Rules() {
  const [rules, setRules] = useState<Rule[]>([])
  const apps = useStore(x => x.apps)
  const [pattern, setPattern] = useState('')
  const [action, setAction] = useState<'allow' | 'ask'>('allow')
  const [err, setErr] = useState('')
  const load = () => api('/api/rules').then(setRules)
  useEffect(() => { load() }, [])
  const add = async () => {
    setErr('')
    try { await api('/api/rules', { method: 'POST', body: { pattern, action } }); setPattern(''); load() } catch (e: any) { setErr(e.message) }
  }
  const del = async (id: number) => { await api(`/api/rules/${id}`, { method: 'DELETE' }); load() }
  const tools = apps.flatMap(a => a.tools.map(t => `${a.name}__${t}`))
  const group = (act: 'allow' | 'ask', title: string, hint: string) => (
    <div>
      <h4>{title}</h4>
      <p className="muted small">{hint}</p>
      {rules.filter(r => r.action === act).map(r => (
        <div className="kv" key={r.id}><span><code>{r.pattern}</code> <span className="muted small">{r.note}</span></span>
          <button className="ghost-btn" onClick={() => del(r.id)}>删除</button></div>))}
      {!rules.some(r => r.action === act) && <div className="muted small">无</div>}
    </div>
  )
  return (
    <div>
      <p className="muted">大多数操作直接执行；写操作默认先问你。规则按工具全名匹配，可以用 <code>*</code> 通配，例如 <code>filesystem__*</code>。两类规则都命中时，“先问我”优先。</p>
      {group('ask', '先问我', '命中的操作一定弹确认卡片')}
      {group('allow', '自动允许', '命中的操作直接执行。确认卡片上点“总是允许”会加到这里')}
      <h4>添加规则</h4>
      <datalist id="tool-names">{tools.map(t => <option key={t} value={t} />)}{apps.map(a => <option key={a.name} value={`${a.name}__*`} />)}</datalist>
      <div className="env-row">
        <input list="tool-names" value={pattern} onChange={e => setPattern(e.target.value)} placeholder="filesystem__write_file" />
        <select value={action} onChange={e => setAction(e.target.value as any)}><option value="allow">自动允许</option><option value="ask">先问我</option></select>
        <button className="btn primary" disabled={!pattern.trim()} onClick={add}>添加</button>
      </div>
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function Vault() {
  const [items, setItems] = useState<{ name: string; created: number; ok: boolean }[]>([])
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [err, setErr] = useState('')
  const load = () => api('/api/vault').then(setItems)
  useEffect(() => { load() }, [])
  const add = async () => {
    setErr('')
    try { await api(`/api/vault/${name}`, { method: 'PUT', body: { value } }); setName(''); setValue(''); load() } catch (e: any) { setErr(e.message) }
  }
  return (
    <div>
      <p className="muted">密钥加密保存在本地数据库，模型和聊天记录里只出现名字。应用的环境变量写 <code>vault:名字</code> 来引用。主密钥取环境变量 <code>OSA_VAULT_KEY</code>，没设置时自动生成在 <code>data/vault.key</code>。</p>
      {items.map(v => (
        <div className="kv" key={v.name}><span><code>{v.name}</code> {!v.ok && <span className="pill bad">主密钥变了，解不开</span>}</span>
          <button className="ghost-btn" onClick={async () => { if (confirm(`删除密钥 ${v.name}？`)) { await api(`/api/vault/${v.name}`, { method: 'DELETE' }); load() } }}>删除</button></div>))}
      {!items.length && <div className="muted small">还没有密钥</div>}
      <h4>添加 / 覆盖</h4>
      <div className="env-row">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="名字，例如 GITHUB_TOKEN" />
        <input type="password" autoComplete="off" value={value} onChange={e => setValue(e.target.value)} placeholder="值" />
        <button className="btn primary" disabled={!name || !value} onClick={add}>保存</button>
      </div>
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}
