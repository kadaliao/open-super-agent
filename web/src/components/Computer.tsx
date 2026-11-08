import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { api, useStore, setComputer, setSettings, setTeachDraft } from '../store'
import type { ComputerState, DesktopState, Settings, Teach } from '../types'
import { lang, t } from '../i18n'
import BotAvatar from './BotAvatar'
import { ask, notice } from './Dialog'
import { isEnter } from '../keys'
import { desktop, phone, type LinkState } from '../desktop'

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`
const touch = typeof matchMedia !== 'undefined' && matchMedia('(pointer: coarse)').matches
const SEP = lang === 'en' ? ', ' : '、'

/** 演示录制中：计时、完成（生成技能草稿）、放弃。 */
function TeachBar({ teach }: { teach: Teach }) {
  const [now, setNow] = useState(Date.now())
  const [busy, setBusy] = useState('')
  const bot = useStore(s => s.bots.find(b => b.id === teach.bot_id))
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(i) }, [])
  const act = async (a: 'stop' | 'cancel') => {
    if (a === 'cancel' && !(await ask(t('放弃这段录制？录到的操作不会保存。'), { ok: t('放弃'), danger: true }))) return
    setBusy(a)
    try {
      const r = await api(`/api/teach/${a}`, { method: 'POST' })
      if (a === 'stop') setTeachDraft({ bot_id: r.bot_id, draft: r.draft, steps: r.steps })
    } catch (e: any) { notice(e.message) }
    setBusy('')
  }
  const left = Math.max(0, teach.max_s - (now / 1000 - teach.started))
  return (
    <div className="teach-bar">
      <span className="rec-dot" />
      <span className="teach-text">{busy === 'stop' ? t('正在把操作整理成 {name} 的技能…', { name: bot?.name || 'Bot' }) : t('正在录制演示 {time}', { time: mmss(now / 1000 - teach.started) })}
        <span className="muted small">{busy ? '' : ' · ' + t('还能录 {time} · 只记浏览器里的操作，密码不会记录', { time: mmss(left) })}</span></span>
      <button className="btn" disabled={!!busy} onClick={() => act('cancel')}>{t('放弃')}</button>
      <button className="btn primary" disabled={!!busy} onClick={() => act('stop')}>{busy === 'stop' ? t('整理中…') : t('完成，生成技能')}</button>
    </div>
  )
}

/** 接手时在手机上输入：点画面弹不出键盘，就在这里打字再发给电脑。 */
function TypeBar({ botId }: { botId: number }) {
  const [text, setText] = useState('')
  const send = (body: any) => api(`/api/bots/${botId}/computer/type`, { method: 'POST', body }).catch((e: any) => notice(e.message))
  return (
    <div className="type-bar">
      <input value={text} placeholder={t('输入文字发到电脑（先在画面上点一下输入框）')} onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (isEnter(e) && text) { send({ text }); setText('') } }} />
      <button className="btn" disabled={!text} onClick={() => { send({ text }); setText('') }}>{t('发送')}</button>
      <button className="btn" onClick={() => send({ key: 'Return' })}>{t('回车')}</button>
      <button className="btn" onClick={() => send({ key: 'BackSpace' })}>⌫</button>
    </div>
  )
}

const STATE: Record<string, string> = {
  none: t('还没启动'), starting: t('启动中'), running: t('运行中'), paused: t('已暂停'), stopped: t('已停止'), error: t('出错了'),
}
const BACKEND: Record<string, string> = { host: t('本机 Chrome'), linked: t('你的 Mac'), local: t('本地 Docker'), instabox: t('Instabox 云端') }
const MISSING: Record<string, string> = { host: t('没找到 Chrome'), linked: t('还没借出来'), local: t('没装 Docker'), instabox: t('没设 INSTABOX_API_KEY') }
// “本机”是服务所在的那台机器。桌面 App 连着服务器（或者在浏览器里用服务器）时，那是服务器，不是你面前这台
const ON_SERVER: Record<string, string> = { host: t('服务器上的 Chrome'), local: t('服务器上的 Docker') }
const onServer = (id: string, s: Settings | null) => s?.computer_on_desktop === false && !!ON_SERVER[id]
const backendName = (id: string, s: Settings | null, st?: ComputerState) =>
  id === 'linked' && st?.link?.name ? t('你的 Mac（{name}）', { name: st.link.name }) : (onServer(id, s) ? ON_SERVER[id] : BACKEND[id]) || id

/** 用哪台电脑：本机的 Chrome、借来的 Mac、本地 Docker、Instabox。有任务在用时不能换。 */
function BackendPicker({ st }: { st: ComputerState }) {
  const s = useStore(x => x.settings)
  if (!s?.computer_backends) return <span className="muted small">· {backendName(st.backend, s, st)}</span>
  const busy = !!(st.users?.length || st.exclusive)
  const change = async (v: string) => {
    try { setSettings(await api<Settings>('/api/settings', { method: 'PUT', body: { computer_backend: v } })) }
    catch (e: any) { notice(e.message) }
  }
  return (
    <select className="pc-backend" value={s.computer_backend || 'auto'} disabled={busy}
      title={busy ? t('有任务在用电脑，用完再换') : desktop?.link && !phone && s.computer_on_desktop === false ? t('App 连着服务器。要让服务器上的 Bot 用这台 Mac，点下面的“借给服务器”') : t('用哪台电脑')}
      onChange={e => change(e.target.value)}>
      <option value="auto">{t('自动（{name}）', { name: s.computer_backend === 'auto' ? backendName(st.backend, s, st) : t('按环境选') })}</option>
      {s.computer_backends.map(b => {
        const ok = b.id === 'linked' ? !!st.link?.connected : b.ok   // 借来的 Mac 随时连上、断开，以电脑状态里的为准
        return <option key={b.id} value={b.id} disabled={!ok && b.id !== s.computer_backend}>
          {backendName(b.id, s, st)}{ok ? '' : `（${b.locked ? t('这台服务器不允许') : onServer(b.id, s) ? t('没装') : MISSING[b.id]}）`}</option>
      })}
    </select>
  )
}

/** 本机电脑：终端默认关，打开后 Bot 每条命令都要你确认。 */
function HostShell({ on }: { on: boolean }) {
  const save = async (v: boolean) => {
    if (v && !(await ask(t('让 Bot 能在这台电脑上执行终端命令？每条命令执行前都会问你，命令能访问这台电脑上你能访问的所有文件。'), { ok: t('打开终端') }))) return
    try { setSettings(await api<Settings>('/api/settings', { method: 'PUT', body: { computer_host_shell: v } })) }
    catch (e: any) { notice(e.message) }
  }
  return (
    <label className="pc-shell muted small">
      <input type="checkbox" checked={on} onChange={e => save(e.target.checked)} />
      {t('允许 Bot 用终端（每条命令都要你确认）')}
    </label>
  )
}

/** 本机电脑的“操作整台电脑”（只有 Mac）：默认关；打开后 Bot 能看整个屏幕、用你的鼠标键盘操作别的 App。还差 macOS 权限时给按钮去打开。
 *  借来的 Mac 还要在那台 Mac 上允许（App 菜单栏里的开关）：mine 是这个 App 就是借出去的那台，可以直接在这里问（App 会弹窗）。 */
function HostDesktop({ d, botId, mine }: { d: DesktopState; botId: number; mine: boolean }) {
  const [busy, setBusy] = useState(false)
  const refresh = () => api<ComputerState>(`/api/bots/${botId}/computer`).then(setComputer).catch(() => {})
  const allowHere = async (v: boolean) => {
    try { await desktop!.link!.setDesktop(v) } catch (e: any) { notice(e.message) }
    await refresh()
  }
  const save = async (v: boolean) => {
    if (v && !mine && !(await ask(t('让 Bot 能像你一样用这台电脑？它能看整个屏幕，用你的鼠标键盘打开任何 App、点击、打字（包括终端和系统设置）。每个任务第一次用会先问你；Bot 操作时你动一下鼠标或键盘，它就会停下等你交还。'), { ok: t('打开') }))) return
    try { setSettings(await api<Settings>('/api/settings', { method: 'PUT', body: { computer_host_desktop: v } })) }
    catch (e: any) { return notice(e.message) }
    if (mine) await allowHere(v)   // 借出去的就是这台：Mac 上的开关一起开关（打开时 App 弹窗问你）
  }
  const grant = async () => {
    setBusy(true)
    try { setComputer(await api<ComputerState>('/api/computer/desktop/grant', { method: 'POST' })) } catch (e: any) { notice(e.message) }
    setBusy(false)
  }
  const missing = d.allowed === false ? [] : [!d.accessibility && t('辅助功能'), !d.screen && t('屏幕录制')].filter(Boolean)
  return (
    <div className="pc-desk">
      <label className="pc-shell muted small">
        <input type="checkbox" checked={!!d.on} onChange={e => save(e.target.checked)} />
        {t('允许 Bot 操作整台电脑（看屏幕、用鼠标键盘操作其他 App）')}
      </label>
      {d.on && !!missing.length && <div className="pc-desk-perm small">
        <span>{t('还差 macOS 权限：{what}', { what: missing.join(SEP) })}</span>
        <button className="btn" disabled={busy} onClick={grant}>{t('去打开权限')}</button>
        <span className="muted">{t('在系统设置里打开 Open Super Agent 的开关；打开后还显示没有，就退出 App 再打开。')}</span>
      </div>}
      {d.on && d.allowed === false && mine && <div className="pc-desk-perm small">
        <span>{t('这台 Mac 上还没允许服务器操作屏幕')}</span>
        <button className="btn" onClick={() => allowHere(true)}>{t('允许')}</button>
      </div>}
      {d.on && !missing.length && d.problem && !(d.allowed === false && mine) && <div className="muted small">{d.problem}</div>}
      {d.on && !missing.length && !d.problem && <div className="muted small">{d.owner ? t('{who} 正在用屏幕，你动一下鼠标或键盘它就停下', { who: d.owner }) : t('Bot 操作屏幕时，你动一下鼠标或键盘它就停下')}</div>}
    </div>
  )
}

/** 桌面 App 连着服务器时：把这台 Mac 借给服务器，服务器上的 Bot 就能用它（Mac 上的小程序主动连过去，见 osa/computer/agent.py）。
 *  借：向服务器要一个借用令牌交给 App，App 在 Mac 上弹窗问你同意了才存；不同意就让服务器作废它。断开：App 删掉令牌、服务器作废它。 */
function LendMac({ st, onState }: { st: ComputerState; onState: (l: LinkState | null) => void }) {
  const s = useStore(x => x.settings)
  const [ls, setLs] = useState<LinkState | null>(null)
  const [busy, setBusy] = useState(false)
  const load = async () => {
    try { const x = await desktop!.link!.state(); setLs(x); onState(x) } catch { onState(null) }
  }
  useEffect(() => { if (desktop?.link) load() }, [st.link?.connected])
  useEffect(() => {   // 菜单栏里断开的：让服务器也作废这个令牌
    const h = (e: Event) => { api(`/api/computer/links/${(e as CustomEvent).detail}`, { method: 'DELETE' }).catch(() => {}); load() }
    window.addEventListener('osa-unlinked', h)
    return () => window.removeEventListener('osa-unlinked', h)
  }, [])
  if (!desktop?.link || phone || !ls?.available || s?.security?.demo) return null
  const use = () => api<Settings>('/api/settings', { method: 'PUT', body: { computer_backend: 'linked' } }).then(setSettings).catch((e: any) => notice(e.message))
  const lend = async () => {
    setBusy(true)
    try {
      const r = await api<{ id: number; token: string }>('/api/computer/links', { method: 'POST', body: { name: 'Mac' } })
      if (await desktop!.link!.connect(r.token, r.id)) {
        if (!st.users?.length && !st.exclusive) await use()
      } else {
        await api(`/api/computer/links/${r.id}`, { method: 'DELETE' }).catch(() => {})
      }
    } catch (e: any) { notice(e.message) }
    await load()
    setBusy(false)
  }
  const stop = async () => {
    if (!(await ask(t('不再把这台 Mac 借给服务器？服务器上的 Bot 就用不了它了，正在用它的任务会出错。'), { ok: t('断开'), danger: true }))) return
    setBusy(true)
    try {
      await desktop!.link!.disconnect()
      if (ls.id) await api(`/api/computer/links/${ls.id}`, { method: 'DELETE' })
      if (st.backend === 'linked')   // 不然任务都会报“你的 Mac 没连上”
        await api<Settings>('/api/settings', { method: 'PUT', body: { computer_backend: 'auto' } }).then(setSettings)
    } catch (e: any) { notice(e.message) }
    await load()
    setBusy(false)
  }
  return (
    <div className="pc-lend small">
      {ls.linked ? <>
        <span>{st.link?.connected ? t('这台 Mac 借给服务器了') : t('这台 Mac 借给服务器了，正在连接…')}</span>
        {st.backend !== 'linked' && st.link?.connected && <button className="btn" onClick={use}>{t('让 Bot 用它')}</button>}
        <button className="btn" disabled={busy} onClick={stop}>{t('断开')}</button>
      </> : <>
        <span className="muted">{t('让服务器上的 Bot 用这台 Mac：它们会用这台 Mac 上单独开的 Chrome，打开“操作整台电脑”后还能用屏幕。')}</span>
        <button className="btn" disabled={busy} onClick={lend}>{t('借给服务器')}</button>
      </>}
    </div>
  )
}

/** 本机电脑的画面：没有 VNC，每隔一会儿取一张 Bot 正在用的窗口的截图（手机上也能看）。窗口本身就在这台电脑的屏幕上。
 *  打开了“操作整台电脑”时 view 选整个屏幕还是 Bot 的 Chrome，空的是跟着 Bot 最近一次操作的走。 */
export function HostView({ botId, view = '' }: { botId: number; view?: string }) {
  const [src, setSrc] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let stop = false, busy = false, url = ''
    const tick = async () => {
      if (busy || document.hidden) return
      busy = true
      try {
        const r = await fetch(`/api/bots/${botId}/computer/shot${view ? `?view=${view}` : ''}`, { cache: 'no-store' })
        if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail || r.statusText)
        const next = URL.createObjectURL(await r.blob())
        if (stop) { URL.revokeObjectURL(next); return }
        if (url) URL.revokeObjectURL(url)
        url = next
        setSrc(next); setErr('')
      } catch (e: any) { if (!stop) setErr(e.message) }
      finally { busy = false }
    }
    tick()
    const i = setInterval(tick, 1500)
    return () => { stop = true; clearInterval(i); if (url) URL.revokeObjectURL(url) }
  }, [botId, view])
  return (
    <div className="pc-view">
      {src && <img className="pc-shot" src={src} alt="" />}
      {(!src || err) && <div className="pc-overlay">{err || t('正在取画面…')}</div>}
    </div>
  )
}

/** 电脑的实时画面。interactive=false 时只能看。 */
export function ComputerView({ botId, interactive }: { botId: number; interactive: boolean }) {
  const box = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const [conn, setConn] = useState<'connecting' | 'connected' | 'closed'>('connecting')
  const [retry, setRetry] = useState(0)

  useEffect(() => {
    if (!box.current) return
    setConn('connecting')
    const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + `/api/bots/${botId}/computer/vnc`
    const r = new RFB(box.current, url, { wsProtocols: ['binary'], shared: true })
    r.scaleViewport = true
    r.resizeSession = false
    r.background = 'transparent'
    r.viewOnly = !interactive
    r.addEventListener('connect', () => setConn('connected'))
    r.addEventListener('disconnect', () => { setConn('closed'); setTimeout(() => setRetry(x => x + 1), 3000) })
    rfb.current = r
    return () => { try { r.disconnect() } catch {} }
  }, [botId, retry])

  useEffect(() => {
    if (!rfb.current) return
    rfb.current.viewOnly = !interactive
    if (interactive) rfb.current.focus()
  }, [interactive])

  return (
    <div className={'pc-view' + (interactive ? ' live' : '')}>
      <div ref={box} className="pc-canvas" />
      {conn !== 'connected' && <div className="pc-overlay">{conn === 'connecting' ? t('正在连接画面…') : t('画面已断开，正在重连…')}</div>}
    </div>
  )
}

/** 正在用电脑的任务：每个在自己的浏览器窗口里做。点一下把画面固定在它的窗口上，再点一下取消（画面跟着最近的操作走）。 */
function Users({ st }: { st: ComputerState }) {
  const bots = useStore(s => s.bots)
  const tasks = useStore(s => s.tasks)
  const users = st.users || []
  if (!users.length && !st.waiting?.length) return null
  const watch = (key: string | null) =>
    api<ComputerState>('/api/computer/watch', { method: 'POST', body: { key } }).then(setComputer).catch((e: any) => notice(e.message))
  return (
    <div className="pc-users">
      <span className="muted small">{users.length > 1 ? t('{n} 个任务同时在用', { n: users.length }) : t('在用')}</span>
      {users.map(u => {
        const bot = bots.find(b => b.id === u.bot_id)
        const on = st.watch === u.key
        const last = u.task_id ? tasks[u.task_id]?.last_action : ''
        return (
          <button key={u.key} className={'chip pc-user' + (on ? ' on' : '')} disabled={!u.window}
            title={(last ? last + '\n' : '') + (!u.window ? t('还没打开浏览器') : on ? t('画面固定在这个任务的窗口上，再点一下取消') : t('在画面上看这个任务'))}
            onClick={() => watch(on ? null : u.key)}>
            {bot && <BotAvatar avatar={bot.avatar} size={14} />}{u.label}{on && <span className="pin">· {t('正在看')}</span>}
          </button>
        )
      })}
      {!!st.waiting?.length && <span className="muted small" title={st.waiting.join(SEP)}>{t('{n} 个在排队', { n: st.waiting.length })}</span>}
    </div>
  )
}

type Machine = { id: string; name: string; state: string; template: string; created: string; ours: boolean; current: boolean }

/** Instabox：列出账号下已有的机器，选一台当共用电脑（免费版只能有一台，不用再新建）。 */
function MachinePicker({ botId, onErr }: { botId: number; onErr: (e: string) => void }) {
  const [list, setList] = useState<Machine[] | null>(null)
  const [fixed, setFixed] = useState(false)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    api('/api/computer/machines').then(r => {
      setList(r.machines); setFixed(r.fixed)
      if (r.machines.length && !r.machines.some((m: Machine) => m.current)) setOpen(true)
    }).catch(e => onErr(e.message))
  }, [])
  const use = async (id: string) => {
    onErr('')
    try { setComputer(await api('/api/computer/use', { method: 'POST', body: { machine_id: id, bot_id: botId } })); setOpen(false) }
    catch (e: any) { onErr(e.message) }
  }
  if (!list || fixed || !list.length) return null
  const others = list.filter(m => !m.current)
  if (!others.length) return null
  if (!open) return <button className="ghost-btn" onClick={() => setOpen(true)}>{t('换成账号里已有的另一台电脑')}</button>
  return (
    <div className="machine-list">
      <div className="muted small">{t('你的 Instabox 账号里已经有 {n} 台电脑，可以直接用，不用新建：', { n: list.length })}</div>
      {others.map(m => (
        <div key={m.id} className="kv">
          <span>{m.name || m.id}<span className="muted small"> · {STATE[m.state] || m.state}{m.template && ` · ${m.template}`}{m.ours && ' · ' + t('之前为这里创建的')}</span></span>
          <button className="btn" onClick={() => use(m.id)}>{t('用这台')}</button>
        </div>
      ))}
    </div>
  )
}

export function ComputerPanel({ botId }: { botId: number }) {
  const st = useStore(s => s.computers[botId])
  const task = useStore(s => Object.values(s.tasks).find(x => x.bot_id === botId && x.status === 'running'))
  const teach = useStore(s => s.teach)
  const settings = useStore(s => s.settings)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [big, setBig] = useState(false)
  const [look, setLook] = useState('')   // 本机电脑：看整个屏幕（screen）还是 Bot 的 Chrome（chrome），空的是跟着 Bot 走
  const [lent, setLent] = useState<LinkState | null>(null)   // 这个 App 所在的 Mac 借给服务器了没有
  useEffect(() => { if (teach) setBig(true) }, [teach?.started])   // 开始录制就放大画面

  useEffect(() => { api<ComputerState>(`/api/bots/${botId}/computer`).then(setComputer).catch(() => {}) }, [botId])
  const act = async (a: string) => {
    setBusy(a); setErr('')
    try { setComputer(await api(`/api/bots/${botId}/computer/${a}`, { method: 'POST' })) } catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  useEffect(() => {
    const h = async (e: Event) => {
      const d = (e as CustomEvent).detail
      if (d.botId !== botId) return
      setBusy('takeover'); setErr('')   // 从“请你帮忙”卡片接手：先把那个任务的窗口放到前面
      try { setComputer(await api(`/api/bots/${botId}/computer/takeover`, { method: 'POST', body: { task_id: d.taskId } })) } catch (e: any) { setErr(e.message) }
      setBusy(''); setBig(true)
    }
    window.addEventListener('osa-takeover', h)
    return () => window.removeEventListener('osa-takeover', h)
  }, [botId])

  if (!st) return <div className="muted small">{t('读取电脑状态…')}</div>
  const host = st.backend === 'host' || st.backend === 'linked'   // 用户自己的电脑：本机，或者借来的 Mac
  const desk = host && !!st.desktop?.on && !!st.desktop.screen && st.desktop.allowed !== false   // 能看整个屏幕：Chrome 没开也有画面
  const mine = st.backend === 'linked' && !!lent?.linked && !!st.link?.connected   // 借来的就是你面前这台 Mac
  const running = st.state === 'running' || desk
  const human = st.controller === 'human'
  const view = host ? <HostView botId={botId} view={desk ? look : 'chrome'} /> : <ComputerView botId={botId} interactive={human} />
  const looks = desk && st.state === 'running' && (
    <div className="seg pc-look">
      {[['', t('跟着 Bot')], ['screen', t('整个屏幕')], ['chrome', 'Chrome']].map(([v, label]) =>
        <button key={v} className={look === v ? 'on' : ''} onClick={() => setLook(v)}>{label}</button>)}
    </div>
  )
  return (
    <div className="pc-panel">
      <div className="pc-bar">
        <span className={'pc-dot ' + (desk ? 'running' : st.state)} />
        <span>{desk && st.state !== 'running' ? t('Chrome 没开，能看整个屏幕') : STATE[st.state] || st.state}</span>
        <BackendPicker st={st} />
        <span className="grow" />
        {st.state === 'running' && host && <button className="ghost-btn" disabled={!!busy} onClick={() => act('show')} title={t('把 Bot 的 Chrome 窗口放到屏幕最前面')}>{t('显示窗口')}</button>}
        {running && <button className="ghost-btn" onClick={() => setBig(true)} title={t('放大')}>⤢ {t('放大')}</button>}
      </div>
      {looks}
      {running && <Users st={st} />}
      {running ? view : (
        <div className="pc-empty">
          {host ? <p className="muted small">{st.backend === 'linked' ? t('Bot 用借来的 Mac 上单独开的一个 Chrome（和那台 Mac 上日常用的 Chrome 分开），窗口在那台 Mac 的屏幕上。第一次用先打开它，在里面登录要 Bot 用的网站，所有 Bot 共用这些登录。') : t('Bot 用这台电脑上单独开的一个 Chrome（和你自己的 Chrome 分开），窗口就在你的屏幕上。第一次用先打开它，在里面登录要 Bot 用的网站，所有 Bot 共用这些登录。')}
            {' '}{t('几个任务可以同时用，每个任务一个窗口，最多同时 {n} 个。文件放在工作文件夹里：{dir}', { n: st.parallel || 3, dir: st.workspace || '' })}</p>
            : <p className="muted small">{t('所有 Bot 共用一台电脑（浏览器 + 终端）。几个任务可以同时用，每个任务一个浏览器窗口，登录状态和文件共用；')}
            {' '}{t('最多同时 {n} 个，多了排队。派出的任务需要时会自动启动，闲置 15 分钟自动暂停。', { n: st.parallel || 3 })}</p>}
          {st.state === 'error' && st.detail && <div className="error-line">{st.detail}</div>}
          {st.backend === 'instabox' && <MachinePicker key={st.detail} botId={botId} onErr={setErr} />}
          {!(host && st.state === 'error') && <button className="btn primary" disabled={!!busy} onClick={() => act('start')}>{busy === 'start' ? t('启动中…（首次需要一会儿）') : host ? t('打开 Chrome') : st.state === 'paused' ? t('恢复') : st.state === 'none' ? t('新建并启动电脑') : t('启动电脑')}</button>}
        </div>
      )}
      {teach && <TeachBar teach={teach} />}
      {running && human && touch && <TypeBar botId={botId} />}
      {running && !teach && (
        <div className="pc-actions">
          {human ? <>
            <span className="pc-you">{st.desktop?.paused_by === 'input' ? t('你动了鼠标或键盘，Bot 先停下了。用完点交还，它接着做') : desk ? t('Bot 已暂停，你直接用电脑') : host ? t('Bot 已暂停，你直接在 Chrome 窗口里操作') : t('你正在操作')}</span>
            <button className="btn primary" onClick={() => act('handback')}>{t('交还给 Bot')}</button>
          </> : <>
            <span className="muted small">{task?.last_action ? t('Bot：{action}', { action: task.last_action }) : host ? t('Bot 控制中') : t('Bot 控制中（你只能看）')}</span>
            <span className="grow" />
            <button className="btn" onClick={() => { act('takeover'); if (!host) setBig(true) }}>{t('接手')}</button>
            {(!host || st.state === 'running') && <button className="btn" disabled={!!busy} onClick={() => act('pause')}>{host ? t('关闭 Chrome') : t('暂停')}</button>}
          </>}
        </div>
      )}
      {host && st.state !== 'running' && desk && <button className="btn" disabled={!!busy} onClick={() => act('start')}>{busy === 'start' ? t('启动中…（首次需要一会儿）') : t('打开 Chrome')}</button>}
      {st.backend === 'host' && <HostShell on={!!st.shell} />}
      {host && st.desktop?.supported && <HostDesktop d={st.desktop} botId={botId} mine={mine} />}
      {settings?.computer_on_desktop === false && <LendMac st={st} onState={setLent} />}
      {err && <div className="error-line">{err}</div>}
      {big && running && (
        <div className="modal-bg" onMouseDown={() => setBig(false)}>
          <div className="pc-big" onMouseDown={e => e.stopPropagation()}>
            <div className="pc-bar">
              <span className={'pc-dot ' + st.state} /><b>{t('电脑')}</b>
              <span className="muted small">{'· ' + (human ? t('你正在操作') : t('Bot 控制中'))}</span><span className="grow" />
              {teach ? null : human ? <button className="btn primary" onClick={() => act('handback')}>{t('交还给 Bot')}</button>
                : <button className="btn" onClick={() => act('takeover')}>{t('接手')}</button>}
              <button className="icon-btn" onClick={() => setBig(false)}>✕</button>
            </div>
            {teach && <TeachBar teach={teach} />}
            {looks}
            <Users st={st} />
            {view}
            {human && touch && <TypeBar botId={botId} />}
          </div>
        </div>
      )}
    </div>
  )
}
