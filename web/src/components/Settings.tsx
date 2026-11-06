import { useEffect, useState } from 'react'
import { api, setSettings, useStore } from '../store'
import { Icon } from '../icons'
import type { Rule, Settings as S } from '../types'
import Backup from './Backup'
import Channels from './Channels'
import ModelSettings, { ROLE_NAMES } from './ModelSettings'
import { priceSource, priceText, ToastHost, useToast } from './settingsKit'

// 设置：左边按类分组的导航，右边是这一页
const GROUPS: { name: string; items: [string, string, string][] }[] = [
  { name: '模型与费用', items: [['models', '模型', 'sparkles'], ['usage', '费用', 'coins'], ['limits', '限制与预算', 'gauge']] },
  { name: '安全', items: [['rules', '确认规则', 'shield'], ['vault', '保险库', 'key']] },
  { name: '连接', items: [['channels', '聊天软件', 'message'], ['notify', '通知', 'bell']] },
  { name: '数据', items: [['triggers', '定时任务', 'clock'], ['backup', '备份与迁移', 'archive']] },
]
const TITLES = Object.fromEntries(GROUPS.flatMap(g => g.items.map(([k, v]) => [k, v])))
const DESC: Record<string, string> = {
  models: 'Bot 用哪些大模型。先添加模型服务，再给每项工作选一个模型。',
  usage: '花了多少钱：按天、按用途、按任务统计。',
  limits: '防止任务跑太久、花太多。改完离开输入框就保存。',
  triggers: '所有 Bot 的定时任务一览。',
}
const LIMIT_NAMES: Record<string, [string, string]> = {
  daily_token_budget: ['每天最多用多少 token', '超过以后不再派新的任务，第二天恢复'],
  worker_max_steps: ['每个任务最多走几步', '一步 = 一次模型调用。电脑操作一步一个动作，要多给一些'],
  task_timeout_s: ['任务最长跑多久（秒）', '超时后 Watchdog 自动取消'],
  approval_timeout_s: ['后台任务等确认多久（秒）', '定时任务等你确认的时限，超时按拒绝处理'],
}

export default function Settings({ onClose, initial }: { onClose: () => void; initial?: string }) {
  const [tab, setTab] = useState(() => initial || localStorage.getItem('osa.settings') || 'models')
  const auth = useStore(s => s.auth)
  useEffect(() => localStorage.setItem('osa.settings', tab), [tab])
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', h)
    return () => document.removeEventListener('keydown', h)
  }, [onClose])
  const title = TITLES[tab] ? tab : 'models'
  return (
    <div className="modal-bg" onMouseDown={onClose}>
      <div className="modal set-modal" onMouseDown={e => e.stopPropagation()}>
        <ToastHost>
          <nav className="set-nav">
            <div className="set-nav-title">设置</div>
            {GROUPS.map(g => (
              <div key={g.name} className="set-group">
                <div className="set-group-name">{g.name}</div>
                {g.items.map(([k, v, icon]) => (
                  <button key={k} className={'set-item' + (title === k ? ' on' : '')} onClick={() => setTab(k)}><Icon name={icon} />{v}</button>))}
              </div>))}
            <span className="grow" />
            <button className="set-item" onClick={() => window.dispatchEvent(new CustomEvent('osa-onboarding'))}><Icon name="sparkles" />新手引导</button>
            {auth && <form method="post" action="/logout"><button className="set-item" type="submit"><Icon name="logout" />退出登录</button></form>}
          </nav>
          <div className="set-main">
            <div className="set-head">
              <div><h2>{TITLES[title]}</h2>{DESC[title] && <p>{DESC[title]}</p>}</div>
              <button className="icon-btn" title="关闭（Esc）" onClick={onClose}><Icon name="x" size={18} /></button>
            </div>
            <div className="set-body">
              {title === 'models' && <ModelSettings />}
              {title === 'limits' && <Limits />}
              {title === 'usage' && <Usage />}
              {title === 'rules' && <Rules />}
              {title === 'vault' && <Vault />}
              {title === 'triggers' && <Triggers />}
              {title === 'channels' && <Channels />}
              {title === 'notify' && <Notifications />}
              {title === 'backup' && <Backup />}
            </div>
          </div>
        </ToastHost>
      </div>
    </div>
  )
}

function Limits() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const [lim, setLim] = useState(s.limits)
  const [heavy, setHeavy] = useState(s.heavy_workers)
  const [pcs, setPcs] = useState(s.computer_parallel)
  const save = async (body: object) => {
    try { setSettings(await api('/api/settings', { method: 'PUT', body })); toast('已保存，马上生效') }
    catch (e: any) { toast(e.message, true) }
  }
  return (
    <div className="set-rows">
      {Object.entries(LIMIT_NAMES).map(([k, [name, hint]]) => (
        <label key={k} className="set-row">
          <span><b>{name}</b><span>{hint}</span></span>
          <input type="number" min={1} value={lim[k] ?? ''} onChange={e => setLim({ ...lim, [k]: Number(e.target.value) })}
            onBlur={() => { if (lim[k] && lim[k] !== s.limits[k]) save({ limits: { [k]: lim[k] } }) }} />
        </label>))}
      <label className="set-row">
        <span><b>Heavy 模式同时跑几个任务</b><span>同一个问题交给几个后台任务分头做，再汇总。越多越全面，也越贵（1–8）</span></span>
        <input type="number" min={1} max={8} value={heavy} onChange={e => setHeavy(Number(e.target.value))}
          onBlur={() => { if (heavy && heavy !== s.heavy_workers) save({ heavy_workers: heavy }) }} />
      </label>
      <label className="set-row">
        <span><b>同时用电脑的任务</b><span>几个后台任务可以同时用电脑，每个开一个浏览器窗口，登录状态共用；多了排队。电脑内存小就调小（1–8）</span></span>
        <input type="number" min={1} max={8} value={pcs} onChange={e => setPcs(Number(e.target.value))}
          onBlur={() => { if (pcs && pcs !== s.computer_parallel) save({ computer_parallel: pcs }) }} />
      </label>
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
        <div><h4>按用途</h4>{u.by_role.map((r: any) => <div className="kv" key={r.role + r.model}><span>{ROLE_NAMES[r.role] || r.role} · {r.model}</span><span>{f(r.cost)}</span></div>)}</div>
        <div><h4>按任务（最近 20 个）</h4>{u.by_task.map((t: any) => <div className="kv" key={t.id}><span>#{t.id} {t.goal}</span><span>{f(t.cost)}</span></div>)}</div>
      </div>
      <Prices />
      <p className="muted small">{bots.length} 个 Bot 共用一个每日预算，在“限制与预算”里改。</p>
    </div>
  )
}

// 现在用的模型的单价，从哪来的；models.dev 每天自动更新
function Prices() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const p = s.prices
  const rows = [...Object.entries(s.roles).map(([k, v]) => [k, v.provider, v.model]),
    ...(s.embedding.provider ? [['embedding', s.embedding.provider, s.embedding.model]] : [])]
  const refresh = async () => {
    setBusy(true)
    try { const r = await api('/api/prices/refresh', { method: 'POST' }); setSettings(r.settings); toast(r.error ? `更新失败：${r.error}` : '单价已更新', !!r.error) }
    catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  const when = p.updated ? new Date(p.updated * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '还没有'
  return (
    <div>
      <h4>现在用的模型单价</h4>
      {rows.map(([k, prov, model]) => {
        const info = s.model_info[`${prov}/${model}`]
        return <div className="kv" key={k} title={priceSource(info)}><span>{ROLE_NAMES[k] || k} · {model}</span>
          <span className={priceText(info, p.currency) ? '' : 'muted'}>{priceText(info, p.currency) || '没有单价，按 0 计'}</span></div>
      })}
      <p className="muted small">单价自动从 <a href="https://models.dev" target="_blank" rel="noreferrer">models.dev</a> 获取，每天更新一次（上次：{when}），
        美元按 1 : {p.rate} 换成人民币；<code>config.yaml</code> 的 <code>pricing</code> 里写了的以它为准，本机的模型按 0 计。
        {p.error && <span className="error-line"> 上次更新失败：{p.error}</span>}{' '}
        <button className="link-btn" disabled={busy} onClick={refresh}>{busy ? '更新中…' : '现在更新'}</button></p>
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
