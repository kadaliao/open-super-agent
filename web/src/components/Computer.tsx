import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { api, useStore, setComputer, setSettings, setTeachDraft } from '../store'
import type { ComputerState, DesktopState, Settings, Teach } from '../types'
import { lang, t } from '../i18n'
import BotAvatar from './BotAvatar'
import { ask, notice } from './Dialog'
import { isEnter } from '../keys'
import { desktop, phone, type LinkState } from '../desktop'
import { useBack } from '../swipe'

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
/** 用哪台电脑。App 自己独立运行时就是本机，不用选；连着服务器时只在“Bot 电脑”（服务器那边配好的）和“本机”（这台 Mac）之间选。
 *  在这台 Mac 的 App 里选“本机”就是把它借给服务器：向服务器要一个借用令牌交给 App，App 弹窗问你同意了才借；选回“Bot 电脑”就收回。
 *  在别处（手机、浏览器）只能在 Bot 电脑和已经连上的那台 Mac 之间切换。ls 是这个 App 所在的 Mac 借出去了没有（见 desktop.ts）。 */
function ComputerPicker({ st, ls, reload, setErr }: { st: ComputerState; ls: LinkState | null; reload: () => void; setErr: (e: string) => void }) {
  const s = useStore(x => x.settings)
  const [busy, setBusy] = useState(false)
  if (!s || s.computer_on_desktop) return <span className="pc-name">{t('本机')}</span>
  const here = !!ls?.available   // 在 Mac 的 App 里，能借出这台 Mac
  const linked = st.backend === 'linked'
  if (!(here || st.link?.connected || linked) || s.security?.demo) return <span className="pc-name">{t('Bot 电脑')}</span>
  const setBackend = async (b: string) => setSettings(await api<Settings>('/api/settings', { method: 'PUT', body: { computer_backend: b } }))
  const change = async (v: string) => {
    setBusy(true); setErr('')
    try {
      if (v === 'linked') {
        if (here && !ls?.linked) {
          const r = await api<{ id: number; token: string }>('/api/computer/links', { method: 'POST', body: { name: 'Mac' } })
          if (!(await desktop!.link!.connect(r.token, r.id))) {   // 你在弹窗里点了取消
            await api(`/api/computer/links/${r.id}`, { method: 'DELETE' })
            return
          }
        }
        await setBackend('linked')
      } else {
        await setBackend('auto')
        if (ls?.linked) {   // 收回这台 Mac
          await desktop!.link!.disconnect()
          if (ls.id) await api(`/api/computer/links/${ls.id}`, { method: 'DELETE' })
        }
      }
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(false); reload() }
  }
  const inUse = !!(st.users?.length || st.exclusive)
  const mac = here ? t('本机') : st.link?.name || t('我的 Mac')   // 在这台 Mac 的 App 里就叫“本机”
  return (
    <select className="pc-backend" value={linked ? 'linked' : 'bot'} disabled={busy || inUse}
      title={inUse ? t('有任务在用电脑，用完再换') : t('用哪台电脑')} onChange={e => change(e.target.value)}>
      <option value="bot">{t('Bot 电脑')}</option>
      <option value="linked" disabled={!here && !st.link?.connected}>{mac}{linked && !st.link?.connected ? t('（没连上）') : ''}</option>
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

/** 本机（或借来的 Mac）还没给 macOS 权限时：Bot 只能用浏览器。给了“辅助功能”和“屏幕录制”，它就能看屏幕、用鼠标键盘操作别的 App。
 *  here：就在那台 Mac 上，可以点按钮弹系统的授权提示。 */
function DeskPerms({ here }: { here: boolean }) {
  const [busy, setBusy] = useState(false)
  const grant = async () => {
    setBusy(true)
    try { setComputer(await api<ComputerState>('/api/computer/desktop/grant', { method: 'POST' })) } catch (e: any) { notice(e.message) }
    setBusy(false)
  }
  return (
    <div className="pc-desk-perm small">
      <span>{t('要让 Bot 能看屏幕、用鼠标键盘操作其他 App，给 Open Super Agent 打开“辅助功能”和“屏幕录制”权限（打开后还显示这句，就退出 App 再打开）。')}</span>
      {here ? <button className="btn" disabled={busy} onClick={grant}>{t('去打开')}</button> : <span className="muted">{t('在那台 Mac 上打开')}</span>}
    </div>
  )
}

/** 本机电脑的画面：没有 VNC，每隔一会儿取一张截图（手机上也能看）：跟着 Bot 最近一次操作的走，
 *  它在操作屏幕时是整个屏幕，在浏览器里做事时是它的浏览器窗口。 */
export function HostView({ botId }: { botId: number }) {
  const [src, setSrc] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let stop = false, busy = false, url = ''
    const tick = async () => {
      if (busy || document.hidden) return
      busy = true
      try {
        const r = await fetch(`/api/bots/${botId}/computer/shot`, { cache: 'no-store' })
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
  }, [botId])
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
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [big, setBig] = useState(false)
  const bigRef = useRef<HTMLDivElement>(null)
  useBack(() => setBig(false), bigRef, big)
  const [ls, setLs] = useState<LinkState | null>(null)   // 这个 App 所在的 Mac 借给服务器了没有（只有连着服务器的桌面 App 有）
  const reloadLink = () => { if (desktop?.link && !phone) desktop.link.state().then(setLs).catch(() => setLs(null)) }
  useEffect(reloadLink, [st?.link?.connected, st?.backend])
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
  const offline = st.backend === 'linked' && !st.link?.connected
  const live = !offline && (st.state === 'running' || (host && !!st.desktop?.screen))   // 有画面：浏览器开着，或者能看整个屏幕
  const human = st.controller === 'human'
  const here = st.backend === 'host' || !!ls?.linked   // 就在那台电脑上（能点按钮弹系统的授权提示）
  const view = host ? <HostView botId={botId} /> : <ComputerView botId={botId} interactive={human} />
  return (
    <div className="pc-panel">
      <div className="pc-bar">
        <span className={'pc-dot ' + (host ? (offline ? 'error' : 'running') : st.state)} />
        <ComputerPicker st={st} ls={ls} reload={reloadLink} setErr={setErr} />
        {!host && <span className="muted small">{STATE[st.state] || st.state}</span>}
        <span className="grow" />
        {live && <button className="ghost-btn" onClick={() => setBig(true)} title={t('放大')}>⤢ {t('放大')}</button>}
      </div>
      {live && <Users st={st} />}
      {live ? view : (
        <div className="pc-empty">
          {host ? <p className="muted small">{offline ? st.detail : t('Bot 用这台电脑做事时，画面会显示在这里。')}</p>
            : <p className="muted small">{t('所有 Bot 共用一台电脑（浏览器 + 终端）。几个任务可以同时用，每个任务一个浏览器窗口，登录状态和文件共用；')}
            {' '}{t('最多同时 {n} 个，多了排队。派出的任务需要时会自动启动，闲置 15 分钟自动暂停。', { n: st.parallel || 3 })}</p>}
          {st.state === 'error' && st.detail && !offline && <div className="error-line">{st.detail}</div>}
          {st.backend === 'instabox' && <MachinePicker key={st.detail} botId={botId} onErr={setErr} />}
          {!host && <button className="btn primary" disabled={!!busy} onClick={() => act('start')}>{busy === 'start' ? t('启动中…（首次需要一会儿）') : st.state === 'paused' ? t('恢复') : st.state === 'none' ? t('新建并启动电脑') : t('启动电脑')}</button>}
        </div>
      )}
      {teach && <TeachBar teach={teach} />}
      {live && human && touch && <TypeBar botId={botId} />}
      {live && !teach && (
        <div className="pc-actions">
          {human ? <>
            <span className="pc-you">{st.desktop?.paused_by === 'input' ? t('你动了鼠标或键盘，Bot 先停下了。用完点交还，它接着做') : host ? t('Bot 已暂停，你直接用电脑') : t('你正在操作')}</span>
            <button className="btn primary" onClick={() => act('handback')}>{t('交还给 Bot')}</button>
          </> : <>
            <span className="muted small">{task?.last_action ? t('Bot：{action}', { action: task.last_action }) : host ? t('Bot 控制中') : t('Bot 控制中（你只能看）')}</span>
            <span className="grow" />
            <button className="btn" onClick={() => { act('takeover'); if (!host) setBig(true) }}>{t('接手')}</button>
            {!host && <button className="btn" disabled={!!busy} onClick={() => act('pause')}>{t('暂停')}</button>}
          </>}
        </div>
      )}
      {host && !offline && st.desktop?.supported && !(st.desktop.accessibility && st.desktop.screen) && <DeskPerms here={here} />}
      {st.backend === 'host' && <HostShell on={!!st.shell} />}
      {err && <div className="error-line">{err}</div>}
      {big && live && (
        <div className="modal-bg" ref={bigRef} onMouseDown={() => setBig(false)}>
          <div className="pc-big" onMouseDown={e => e.stopPropagation()}>
            <div className="pc-bar">
              <span className={'pc-dot ' + st.state} /><b>{t('电脑')}</b>
              <span className="muted small">{'· ' + (human ? t('你正在操作') : t('Bot 控制中'))}</span><span className="grow" />
              {teach ? null : human ? <button className="btn primary" onClick={() => act('handback')}>{t('交还给 Bot')}</button>
                : <button className="btn" onClick={() => act('takeover')}>{t('接手')}</button>}
              <button className="icon-btn" onClick={() => setBig(false)}>✕</button>
            </div>
            {teach && <TeachBar teach={teach} />}
            <Users st={st} />
            {view}
            {human && touch && <TypeBar botId={botId} />}
          </div>
        </div>
      )}
    </div>
  )
}
