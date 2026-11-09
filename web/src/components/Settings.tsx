import { useEffect, useMemo, useRef, useState } from 'react'
import { encode } from 'uqr'
import { api, setSettings, useStore } from '../store'
import { desktop, phone } from '../desktop'
import { Icon } from '../icons'
import { lang, locale, syncLang, t, tc, tn } from '../i18n'
import type { Routine, Rule, Settings as S } from '../types'
import Backup from './Backup'
import BotAvatar from './BotAvatar'
import { describe, STATUS, when } from './Routines'
import Channels from './Channels'
import Plugins from './Plugins'
import ModelSettings, { ROLE_NAMES } from './ModelSettings'
import { priceSource, priceText, ToastHost, useToast } from './settingsKit'
import { ask } from './Dialog'
import { useBack } from '../swipe'

// 设置：左边按类分组的导航，右边是这一页
const GROUPS: { name: string; items: [string, string, string][] }[] = [
  { name: t('模型与费用'), items: [['models', t('模型'), 'sparkles'], ['usage', t('费用'), 'coins'], ['limits', t('限制与预算'), 'gauge']] },
  { name: t('安全'), items: [['rules', t('确认规则'), 'shield'], ['vault', t('保险库'), 'key']] },
  { name: tc('nav', '连接'), items: [['channels', t('聊天软件'), 'message'], ['notify', t('通知'), 'bell'], ['mobile', t('手机 App'), 'smartphone']] },
  { name: t('自动化'), items: [['triggers', t('定时任务'), 'clock'], ['events', t('事件'), 'zap']] },
  { name: t('数据'), items: [['backup', t('备份与迁移'), 'archive']] },
  // 插件是给开发者扩展 osa 自己的（预览），平常加能力用应用和技能，所以放在最后的“高级”里
  { name: t('高级'), items: [['plugins', t('开发者插件（预览）'), 'puzzle']] },
]
const TITLES = Object.fromEntries(GROUPS.flatMap(g => g.items.map(([k, v]) => [k, v])))
const DESC: Record<string, string> = {
  models: t('Bot 用哪些大模型。先添加模型服务，再给每项工作选一个模型。'),
  usage: t('花了多少钱：按天、按用途、按任务统计。'),
  limits: t('防止任务跑太久、花太多。改完离开输入框就保存。'),
  triggers: t('所有 Bot 的定时任务，在这里就能开关。'),
  events: t('所有 Bot 的事件：某件事发生时（收到 webhook、订阅源有新文章、网页变了……）Bot 处理一次。在这里就能开关。'),
  plugins: t('给开发者扩展 osa 自己用的 Python 代码，在服务进程里运行：订阅事件、碰 osa 内部的状态。还在预览，写法以后可能会变。'),
}
const LIMIT_NAMES: Record<string, [string, string]> = {
  daily_token_budget: [t('每天最多用多少 token'), t('超过以后不再派新的任务，第二天恢复')],
  worker_max_steps: [t('每个任务最多走几步'), t('一步 = 一次模型调用。电脑操作一步一个动作，要多给一些')],
  task_timeout_s: [t('任务最长跑多久（秒）'), t('超时后 Watchdog 自动取消')],
  approval_timeout_s: [t('后台任务等确认多久（秒）'), t('定时任务等你确认的时限，超时按拒绝处理')],
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
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  const title = TITLES[tab] ? tab : 'models'
  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal set-modal" onMouseDown={e => e.stopPropagation()}>
        <ToastHost>
          <nav className="set-nav">
            <div className="set-nav-title">{t('设置')}</div>
            {GROUPS.map(g => (
              <div key={g.name} className="set-group">
                <div className="set-group-name">{g.name}</div>
                {g.items.map(([k, v, icon]) => (
                  <button key={k} className={'set-item' + (title === k ? ' on' : '')} onClick={() => setTab(k)}><Icon name={icon} />{v}</button>))}
              </div>))}
            <span className="grow" />
            <button className="set-item" onClick={() => window.dispatchEvent(new CustomEvent('osa-onboarding'))}><Icon name="sparkles" />{t('新手引导')}</button>
            <LangSwitch />
            {auth && <form method="post" action="/logout"><button className="set-item" type="submit"><Icon name="logout" />{t('退出登录')}</button></form>}
          </nav>
          <div className="set-main">
            <div className="set-head">
              <div><h2>{TITLES[title]}</h2>{DESC[title] && <p>{DESC[title]}</p>}</div>
              <button className="icon-btn" title={t('关闭（Esc）')} onClick={onClose}><Icon name="x" size={18} /></button>
            </div>
            <div className="set-body">
              {title === 'models' && <ModelSettings />}
              {title === 'limits' && <Limits />}
              {title === 'usage' && <Usage />}
              {title === 'rules' && <Rules />}
              {title === 'vault' && <Vault />}
              {title === 'triggers' && <Triggers key="schedule" category="schedule" onClose={onClose} />}
              {title === 'events' && <Triggers key="event" category="event" onClose={onClose} />}
              {title === 'channels' && <Channels />}
              {title === 'notify' && (desktop?.push ? <PhoneNotifications /> : <Notifications />)}
              {title === 'mobile' && <MobileApp />}
              {title === 'backup' && <Backup />}
              {title === 'plugins' && <Plugins />}
            </div>
          </div>
        </ToastHost>
      </div>
    </div>
  )
}

// 切换界面语言：按钮上写另一种语言的名字。服务端的报错、卡片、推送也跟着换
function LangSwitch() {
  const toast = useToast()
  const other = lang === 'zh' ? 'en' : 'zh'
  const change = () => api('/api/settings', { method: 'PUT', body: { language: other } })
    .then(r => syncLang(r.language)).catch(e => toast(e.message, true))
  return <button className="set-item" title={t('界面语言')} onClick={change}><Icon name="globe" />{other === 'en' ? 'English' : '中文'}</button>   // i18n-ignore：语言自己的名字
}

function Limits() {
  const s = useStore(x => x.settings) as S
  const toast = useToast()
  const [lim, setLim] = useState(s.limits)
  const [heavy, setHeavy] = useState(s.heavy_workers)
  const [pcs, setPcs] = useState(s.computer_parallel)
  const demo = !!s.security?.demo   // 演示服务器：用量上限不让改
  const save = async (body: object) => {
    try { setSettings(await api('/api/settings', { method: 'PUT', body })); toast(t('已保存，马上生效')) }
    catch (e: any) { toast(e.message, true) }
  }
  return (
    <div className="set-rows">
      {demo && <p className="muted small">{t('演示服务器上不能改用量上限')}</p>}
      {Object.entries(LIMIT_NAMES).map(([k, [name, hint]]) => (
        <label key={k} className="set-row">
          <span><b>{name}</b><span>{hint}</span></span>
          <input type="number" min={1} disabled={demo} value={lim[k] ?? ''} onChange={e => setLim({ ...lim, [k]: Number(e.target.value) })}
            onBlur={() => { if (lim[k] && lim[k] !== s.limits[k]) save({ limits: { [k]: lim[k] } }) }} />
        </label>))}
      <label className="set-row">
        <span><b>{t('Heavy 模式同时跑几个任务')}</b><span>{t('同一个问题交给几个后台任务分头做，再汇总。越多越全面，也越贵（1–8）')}</span></span>
        <input type="number" min={1} max={8} disabled={demo} value={heavy} onChange={e => setHeavy(Number(e.target.value))}
          onBlur={() => { if (heavy && heavy !== s.heavy_workers) save({ heavy_workers: heavy }) }} />
      </label>
      <label className="set-row">
        <span><b>{t('同时用电脑的任务')}</b><span>{t('几个后台任务可以同时用电脑，每个开一个浏览器窗口，登录状态共用；多了排队。电脑内存小就调小（1–8）')}</span></span>
        <input type="number" min={1} max={8} disabled={demo} value={pcs} onChange={e => setPcs(Number(e.target.value))}
          onBlur={() => { if (pcs && pcs !== s.computer_parallel) save({ computer_parallel: pcs }) }} />
      </label>
    </div>
  )
}

function Usage() {
  const [u, setU] = useState<any>(null)
  const bots = useStore(x => x.bots)
  useEffect(() => { api('/api/usage').then(setU); const timer = setInterval(() => api('/api/usage').then(setU), 8000); return () => clearInterval(timer) }, [])
  if (!u) return <div className="muted">{t('加载中…')}</div>
  const f = (v: number) => u.currency + v.toFixed(v < 1 ? 3 : 2)
  const max = Math.max(...u.by_day.map((d: any) => d.cost), 1e-9)
  return (
    <div className="usage">
      <div className="stat-row">
        <div className="stat"><div className="muted small">{t('今天')}</div><div className="big">{f(u.today.cost)}</div><div className="muted small">{t('{n} 次调用 · 缓存命中 {rate}%', { n: u.today.calls, rate: (u.today.cache_rate * 100).toFixed(0) })}</div></div>
        <div className="stat"><div className="muted small">{t('近 7 天')}</div><div className="big">{f(u.week.cost)}</div></div>
        <div className="stat"><div className="muted small">{t('近 30 天')}</div><div className="big">{f(u.month.cost)}</div></div>
      </div>
      <h4>{t('近 30 天每日')}</h4>
      <div className="bars">{u.by_day.map((d: any) => <div key={d.day} title={`${d.day}  ${f(d.cost)}`} style={{ height: `${Math.max(2, (d.cost / max) * 100)}%` }} />)}</div>
      <div className="two-col">
        <div><h4>{t('按用途')}</h4>{u.by_role.map((r: any) => <div className="kv" key={r.role + r.model}><span>{ROLE_NAMES[r.role] || (r.role.startsWith('plugin:') ? t('插件 {name}', { name: r.role.slice(7) }) : r.role)} · {r.model}{r.role === 'image' ? ' · ' + t('{n} 张', { n: r.calls }) : ''}</span><span>{f(r.cost)}</span></div>)}</div>
        <div><h4>{t('按任务（最近 20 个）')}</h4>{u.by_task.map((tk: any) => <div className="kv" key={tk.id}><span>#{tk.id} {tk.goal}</span><span>{f(tk.cost)}</span></div>)}</div>
      </div>
      <Prices />
      <p className="muted small">{t('{n} 个 Bot 共用一个每日预算，在“限制与预算”里改。', { n: bots.length })}</p>
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
    try { const r = await api('/api/prices/refresh', { method: 'POST' }); setSettings(r.settings); toast(r.error ? t('更新失败：{error}', { error: r.error }) : t('单价已更新'), !!r.error) }
    catch (e: any) { toast(e.message, true) }
    setBusy(false)
  }
  const when = p.updated ? new Date(p.updated * 1000).toLocaleString(locale, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : tc('price', '还没有')
  return (
    <div>
      <h4>{t('现在用的模型单价')}</h4>
      {rows.map(([k, prov, model]) => {
        const info = s.model_info[`${prov}/${model}`]
        return <div className="kv" key={k} title={priceSource(info)}><span>{ROLE_NAMES[k] || k} · {model}</span>
          <span className={priceText(info, p.currency) ? '' : 'muted'}>{priceText(info, p.currency) || t('没有单价，按 0 计')}</span></div>
      })}
      <p className="muted small">{tn('单价自动从 {link} 获取，每天更新一次（上次：{when}），美元按 1 : {rate} 换成人民币；{config} 的 {pricing} 里写了的以它为准，本机的模型按 0 计。', {
          link: <a href="https://models.dev" target="_blank" rel="noreferrer">models.dev</a>, when, rate: p.rate,
          config: <code>config.yaml</code>, pricing: <code>pricing</code> })}
        {p.error && <span className="error-line"> {t('上次更新失败：{error}', { error: p.error })}</span>}{' '}
        <button className="link-btn" disabled={busy} onClick={refresh}>{busy ? t('更新中…') : t('现在更新')}</button></p>
    </div>
  )
}

// 所有 Bot 的定时任务（或事件），按 Bot 分组，每个都能直接开关；点“管理”去那个 Bot 的右栏“定时”“事件”里改
function Triggers({ onClose, category }: { onClose: () => void; category: 'schedule' | 'event' }) {
  const [list, setList] = useState<Routine[] | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const bots = useStore(x => x.bots)
  const toast = useToast()
  const ev = category === 'event'
  const load = () => api<Routine[]>('/api/routines').then(rs => setList(rs.filter(r => r.category === category))).catch((e: any) => toast(e.message, true))
  useEffect(() => {
    load()
    window.addEventListener('osa-routines', load)
    return () => window.removeEventListener('osa-routines', load)
  }, [])
  const toggle = async (r: Routine, on: boolean) => {
    setBusy(r.id)
    try {
      const n: Routine = await api(`/api/routines/${r.id}/${on ? 'resume' : 'pause'}`, { method: 'POST' })
      setList(l => l && l.map(x => x.id === n.id ? n : x))
      toast(on ? t('已开启「{name}」', { name: r.name }) : t('已暂停「{name}」', { name: r.name }))
    } catch (e: any) { toast(e.message, true) }
    setBusy(null)
  }
  const manage = (botId: number) => { onClose(); window.dispatchEvent(new CustomEvent('osa-open-bot', { detail: { botId, tab: ev ? 'events' : 'routines' } })) }
  if (!list) return <div className="muted">{t('加载中…')}</div>
  const on = list.filter(r => r.enabled).length
  const groups = bots.map(b => ({ bot: b, items: list.filter(r => r.bot_id === b.id) })).filter(g => g.items.length)
  return (
    <div>
      <p className="muted small">{!list.length ? '' : ev ? t('{n} 个事件，{on} 个开着。关掉的事件来了也不处理。', { n: list.length, on })
        : t('{n} 个定时任务，{on} 个开着。关掉的不会再按时运行，打开后从下一个时间点继续。', { n: list.length, on })}</p>
      {groups.length ? groups.map(({ bot, items }) => (
        <div key={bot.id} className="rt-group">
          <div className="rt-group-head">
            <BotAvatar avatar={bot.avatar} size={22} /><b>{bot.name}</b>
            <span className="grow" />
            <button className="link-btn" onClick={() => manage(bot.id)}>{t('管理')}</button>
          </div>
          <div className="set-rows">
            {items.map(r => (
              <div key={r.id} className={'set-row rt-row' + (r.enabled ? '' : ' off')}>
                <span>
                  <b>{r.name}</b>
                  <span>{describe(r.trigger, r.describe)}
                    {!ev && r.enabled && r.next_run ? ' · ' + t('下次 {when}', { when: when(r.next_run) }) : ''}
                    {r.event?.error ? <> · <span className="run-failed">{t('出错：{error}', { error: r.event.error.slice(0, 60) })}</span></> : ''}
                    {r.running && ' · ' + t('运行中')}
                    {r.last_run && <> · <span className={'run-' + r.last_run.status}>{t('上次 {when} {status}', { when: when(r.last_run.ts), status: STATUS[r.last_run.status] || r.last_run.status })}</span></>}
                  </span>
                </span>
                <label className="switch" title={r.enabled ? t('暂停') : t('开启')}>
                  <input type="checkbox" checked={r.enabled} disabled={busy === r.id} onChange={e => toggle(r, e.target.checked)} /><span />
                </label>
              </div>
            ))}
          </div>
        </div>
      )) : <div className="muted small">{t('暂无')}</div>}
      <p className="muted small">{ev ? t('事件属于各个 Bot：在对话里说“这个网页有变化就告诉我”，或者在右栏 Bot 详情的“事件”里新建和管理。')
        : t('定时任务属于各个 Bot：在对话里说“每天 9 点帮我……”，或者在右栏 Bot 详情的“定时”里新建和管理。')}</p>
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
      if (await Notification.requestPermission() !== 'granted') throw new Error(t('没有通知权限，请在浏览器或系统设置里允许这个网站发通知'))
      const reg = await navigator.serviceWorker.register('/sw.js')
      await navigator.serviceWorker.ready
      const s = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(info!.key) })
      const r = await api('/api/push/subscribe', { method: 'POST', body: { subscription: s.toJSON() } })
      setSub(s); setInfo({ ...info!, count: r.count }); localStorage.setItem('osa.push', '1'); setMsg(t('已开启'))
    } catch (e: any) { setMsg(e.message) }
    setBusy(false)
  }
  const off = async () => {
    if (!sub) return
    const r = await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } })
    await sub.unsubscribe().catch(() => {})
    setSub(null); setInfo({ ...info!, count: r.count }); localStorage.removeItem('osa.push'); setMsg(t('已关闭'))
  }
  const test = async () => {
    setMsg('')
    try { const r = await api('/api/push/test', { method: 'POST' }); setMsg(r.results.every((x: string) => x === 'ok') ? t('已发出，几秒内应该收到') : r.results.join(lang === 'en' ? '; ' : '；')) }
    catch (e: any) { setMsg(e.message) }
  }
  return (
    <div>
      <p className="muted">{t('没开着这个页面时（比如手机锁屏），需要你确认、请你帮忙、任务做完和 Bot 的回复会推送到开启了推送的设备。页面开着时不推。')}</p>
      {!isSecureContext ? <p className="error-line">{t('推送需要 https 访问（本机 localhost 也行）。手机上建议用 Tailscale 的 https 地址，见文档“远程访问”。')}</p>
        : !supported ? <p className="error-line">{ios ? t('这个浏览器不支持推送。') : t('这个浏览器不支持推送。请换用 Chrome、Edge、Firefox 或 Safari。')}</p>
        : ios && !standalone ? <p className="muted">{t('iPhone / iPad：先在 Safari 里点“分享 → 添加到主屏幕”，从主屏幕打开后再来这里开启。')}</p>
        : <div className="row-actions">
            {sub ? <><span className="pill ok">{t('这台设备已开启')}</span><button className="btn" onClick={test}>{t('发一条测试')}</button><button className="btn" onClick={off}>{tc('push', '关闭')}</button></>
              : <button className="btn primary" disabled={busy || !info} onClick={on}>{busy ? t('开启中…') : t('在这台设备上开启推送')}</button>}
          </div>}
      {info && <p className="muted small">{t('已开启推送的设备：{n} 台', { n: info.count })}</p>}
      {msg && <p className="muted small">{msg}</p>}
      <h4>{t('添加到主屏幕')}</h4>
      <p className="muted small">{t('手机上可以把它当应用用：Safari 点“分享 → 添加到主屏幕”，Chrome 点菜单里的“添加到主屏幕”或“安装应用”。')}</p>
    </div>
  )
}

// 手机 App 里的推送：由 App 向推送中转登记，服务器把它当成又一个推送订阅
function PhoneNotifications() {
  const [key, setKey] = useState('')
  const [sub, setSub] = useState<{ endpoint: string } | null>(null)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const push = desktop!.push!
  useEffect(() => {
    api('/api/push').then(async r => { setKey(r.key); setSub(await push.current(r.key)) }).catch((e: any) => setMsg(e.message))
  }, [])
  const on = async () => {
    setBusy(true); setMsg('')
    try {
      const s = await push.subscribe(key)
      await api('/api/push/subscribe', { method: 'POST', body: { subscription: s } })
      setSub(s); setMsg(t('已开启'))
    } catch (e: any) { setMsg(e.message || String(e)) }
    setBusy(false)
  }
  const off = async () => {
    if (!sub) return
    await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } }).catch(() => {})
    await push.unsubscribe(key).catch(() => {})
    setSub(null); setMsg(t('已关闭'))
  }
  const test = async () => {
    setMsg('')
    try { const r = await api('/api/push/test', { method: 'POST' }); setMsg(r.results.every((x: string) => x === 'ok') ? t('已发出，几秒内应该收到') : r.results.join(lang === 'en' ? '; ' : '；')) }
    catch (e: any) { setMsg(e.message) }
  }
  return (
    <div>
      <p className="muted">{t('App 不在前台时（比如手机锁屏），需要你确认、请你帮忙、任务做完和 Bot 的回复会推送到这台手机。电脑上开着这个页面时不推。')}</p>
      <div className="row-actions">
        {sub ? <><span className="pill ok">{t('这台手机已开启')}</span><button className="btn" onClick={test}>{t('发一条测试')}</button><button className="btn" onClick={off}>{tc('push', '关闭')}</button></>
          : <button className="btn primary" disabled={busy || !key} onClick={on}>{busy ? t('开启中…') : t('在这台手机上开启推送')}</button>}
      </div>
      {msg && <p className="muted small">{msg}</p>}
      <p className="muted small">{t('推送经过 Open Super Agent 的推送中转发给苹果，内容在服务器上加密，只有这台手机能解开，中转看不到。')}</p>
    </div>
  )
}

// 手机 App：电脑上显示二维码，用 iPhone 相机一扫就连上这台服务器并登录；App 里显示连着哪台服务器
function MobileApp() {
  const [pair, setPair] = useState<{ ticket: string; expires_in: number } | null>(null)
  const [err, setErr] = useState('')
  const local = /^(localhost|127\.|\[::1\])|\.localhost$/.test(location.hostname)
  const refresh = () => { setErr(''); api('/api/pair', { method: 'POST' }).then(setPair).catch((e: any) => setErr(e.message)) }
  useEffect(() => { if (!phone && !local) refresh() }, [])
  useEffect(() => {   // 票过期前换一张
    if (!pair?.expires_in) return
    const timer = setTimeout(refresh, (pair.expires_in - 10) * 1000)
    return () => clearTimeout(timer)
  }, [pair])
  const link = pair && `opensuperagent://pair?u=${encodeURIComponent(location.origin)}${pair.ticket ? '&t=' + pair.ticket : ''}`
  if (phone) return (
    <div>
      <p className="muted">{t('这台手机连着 {server}。', { server: location.host })}</p>
      <div className="row-actions"><button className="btn" onClick={() => desktop!.switchServer?.()}>{t('切换服务器')}</button></div>
    </div>
  )
  return (
    <div>
      <p className="muted">{t('在 iPhone 上装好 Open Super Agent App，用系统相机扫下面的二维码，就能连上这台服务器并自动登录。也可以在 App 里手动填这个地址：{server}', { server: location.origin })}</p>
      {local ? <p className="error-line">{t('手机打不开 localhost。请用手机能打开的地址（局域网 IP、Tailscale 或域名）打开这个页面，再来这里扫码。')}</p>
        : err ? <p className="error-line">{err}</p>
        : link && <QrCode text={link} />}
      {pair && <p className="muted small">{pair.ticket ? t('二维码 5 分钟内有效，只能用一次，相当于一次登录，别发给别人。') : t('这台服务器没开登录，扫码直接连上。')}
        {pair.ticket && <> <button className="link-btn" onClick={refresh}>{t('换一张')}</button></>}</p>}
    </div>
  )
}

function QrCode({ text }: { text: string }) {
  const path = useMemo(() => {
    const qr = encode(text, { ecc: 'M', border: 2 })
    let d = ''
    qr.data.forEach((row, y) => row.forEach((on, x) => { if (on) d += `M${x},${y}h1v1h-1z` }))
    return { d, size: qr.size }
  }, [text])
  return <svg className="qr" viewBox={`0 0 ${path.size} ${path.size}`} shapeRendering="crispEdges" role="img" aria-label={t('配对二维码')}>
    <rect width={path.size} height={path.size} fill="#fff" /><path d={path.d} fill="#000" /></svg>
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
  const tools = apps.flatMap(a => a.tools.map(tool => `${a.name}__${tool}`))
  const group = (act: 'allow' | 'ask', title: string, hint: string) => (
    <div>
      <h4>{title}</h4>
      <p className="muted small">{hint}</p>
      {rules.filter(r => r.action === act).map(r => (
        <div className="kv" key={r.id}><span><code>{r.pattern}</code> <span className="muted small">{r.note}</span></span>
          <button className="ghost-btn" onClick={() => del(r.id)}>{t('删除')}</button></div>))}
      {!rules.some(r => r.action === act) && <div className="muted small">{t('无')}</div>}
    </div>
  )
  return (
    <div>
      <p className="muted">{tn('大多数操作直接执行；写操作默认先问你。规则按工具全名匹配，可以用 {star} 通配，例如 {example}。两类规则都命中时，“先问我”优先。', { star: <code>*</code>, example: <code>filesystem__*</code> })}</p>
      <p className="muted small">{tn('也可以按参数匹配：{form}，冒号后面和工具的第一个参数比（终端是命令），例如 {shell}、{path}。本机终端只认这种带命令前缀的规则，命令里带 ; | $( 重定向这类符号的照样每次问。', { form: <code>{t('工具:参数')}</code>, shell: <code>computer_shell:git *</code>, path: <code>filesystem__write_file:/data/workspace/*</code> })}</p>
      {group('ask', t('先问我'), t('命中的操作一定弹确认卡片'))}
      {group('allow', t('自动允许'), t('命中的操作直接执行。确认卡片上点“总是允许”会加到这里'))}
      <h4>{t('添加规则')}</h4>
      <datalist id="tool-names">{tools.map(name => <option key={name} value={name} />)}{apps.map(a => <option key={a.name} value={`${a.name}__*`} />)}</datalist>
      <div className="env-row">
        <input list="tool-names" value={pattern} onChange={e => setPattern(e.target.value)} placeholder="filesystem__write_file" />
        <select value={action} onChange={e => setAction(e.target.value as any)}><option value="allow">{t('自动允许')}</option><option value="ask">{t('先问我')}</option></select>
        <button className="btn primary" disabled={!pattern.trim()} onClick={add}>{t('添加')}</button>
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
      <p className="muted">{tn('密钥加密保存在本地数据库，模型和聊天记录里只出现名字。应用的环境变量写 {ref} 来引用。主密钥取环境变量 {env}，没设置时自动生成在 {file}。', {
        ref: <code>{t('vault:名字')}</code>, env: <code>OSA_VAULT_KEY</code>, file: <code>data/vault.key</code> })}</p>
      {items.map(v => (
        <div className="kv" key={v.name}><span><code>{v.name}</code> {!v.ok && <span className="pill bad">{t('主密钥变了，解不开')}</span>}</span>
          <button className="ghost-btn" onClick={async () => { if (await ask(t('删除密钥 {name}？', { name: v.name }), { ok: t('删除'), danger: true })) { await api(`/api/vault/${v.name}`, { method: 'DELETE' }); load() } }}>{t('删除')}</button></div>))}
      {!items.length && <div className="muted small">{t('还没有密钥')}</div>}
      <h4>{t('添加 / 覆盖')}</h4>
      <div className="env-row">
        <input value={name} onChange={e => setName(e.target.value)} placeholder={t('名字，例如 GITHUB_TOKEN')} />
        <input type="password" autoComplete="off" value={value} onChange={e => setValue(e.target.value)} placeholder={t('值')} />
        <button className="btn primary" disabled={!name || !value} onClick={add}>{t('保存')}</button>
      </div>
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}
