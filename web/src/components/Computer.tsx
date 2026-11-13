import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { api, useStore, setComputer, setMacs, setSettings, setTeachDraft } from '../store'
import type { ComputerState, DesktopState, MacState, Settings, Teach } from '../types'
import { lang, t, tn } from '../i18n'
import BotAvatar from './BotAvatar'
import { ask, notice } from './Dialog'
import { isEnter } from '../keys'
import { desktop, phone, type LinkState } from '../desktop'
import { useBack } from '../swipe'
import { Icon } from '../icons'
import { Section, useToast } from './settingsKit'

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
export const PC_NAMES: Record<string, string> = { host: t('本机'), local: t('Docker 沙盒'), instabox: t('云电脑') }
const openPcSettings = () => window.dispatchEvent(new CustomEvent('osa-open-settings', { detail: 'computer' }))

/** 设置 → 电脑：Bot 用哪台电脑干活（所有 Bot 共用一台），选好以后一直用它；电脑面板里只显示是哪台。
 *  App 自己独立运行时三种：本机（这台 Mac 上 Bot 自己的 Chrome，能操作整个屏幕）、Docker 沙盒、云电脑（Instabox）。
 *  服务器上只有 Docker 沙盒和云电脑（给哪几种由服务器定，computer_choices）：不用服务器自己当电脑。
 *  你的 Mac 不在这里：它们是派任务的去处，见电脑面板里的“你的 Mac”。云电脑还没填 API Key 时先填 Key。 */
export function ComputerSettings() {
  const s = useStore(x => x.settings) as Settings
  const botId = useStore(x => (x.bots.find(b => b.is_primary) || x.bots[0])?.id)
  const st = useStore(x => botId === undefined ? undefined : x.computers[botId])
  const toast = useToast()
  const [busy, setBusy] = useState('')
  const [keyOpen, setKeyOpen] = useState(false)
  const [keyVer, setKeyVer] = useState(0)   // 换过 Key：重新列账号里的机器
  const [pcs, setPcs] = useState(s.computer_parallel)
  useEffect(() => { if (botId !== undefined) api<ComputerState>(`/api/bots/${botId}/computer`).then(setComputer).catch(() => {}) }, [botId])
  const cur = s.computer_current || st?.backend || ''
  const can = (id: string) => s.computer_backends?.find(b => b.id === id)
  const ids = ['host', 'local', 'instabox'].filter(id => id === cur || !s.computer_choices || s.computer_choices.includes(id))
  const inUse = !!(st?.users?.length || st?.exclusive)
  const key = can('instabox')?.key
  const save = async (body: object, done: string) => {
    try { setSettings(await api<Settings>('/api/settings', { method: 'PUT', body })); toast(done) }
    catch (e: any) { toast(e.message, true) }
  }
  const choose = async (id: string) => {
    if (id === cur || busy) return
    if (id === 'instabox' && !can('instabox')?.ok) { setKeyOpen(true); return }
    setBusy(id)
    await save({ computer_backend: id }, t('已换成{name}，之后的任务都用它', { name: PC_NAMES[id] }))
    setBusy('')
  }
  const desc: Record<string, string> = {
    host: t('就是这台 Mac：Bot 用它自己的一个 Chrome（和你的 Chrome 分开），给了权限还能看屏幕、操作别的 App。'),
    local: t('这台机器上的一个 Docker 容器，里面有浏览器和终端，碰不到容器外面的东西。浏览器开着时要占几百 MB 内存。'),
    instabox: t('Instabox 上的一台云端机器，里面有浏览器和终端，不占这台机器的内存。要 Instabox 的 API Key。'),
  }
  if (s.security?.demo) return <p className="muted">{t('演示服务器上没开电脑。')}</p>
  return (
    <>
      <Section title={t('用哪台电脑')} desc={inUse ? t('有任务在用电脑（{who}），用完才能换。', { who: (st?.users || []).map(u => u.label).join(SEP) || st?.exclusive || '' })
        : t('所有 Bot 共用这一台：上网、跑命令、处理文件都在上面。选好以后一直用它。')}>
        <div className="pc-choices" role="radiogroup">
          {ids.map(id => {
            const b = can(id)
            const why = id === 'local' ? b?.why : id === 'host' && !b?.ok ? t('这台 Mac 上没找到 Chrome') : ''
            const off = (id !== cur && !!why) || inUse
            return (
              <label key={id} className={'pc-choice' + (id === cur ? ' on' : '') + (off && id !== cur ? ' off' : '')}>
                <input type="radio" name="pc" checked={id === cur} disabled={off || !!busy} onChange={() => choose(id)} />
                <span className="pc-choice-main">
                  <b>{PC_NAMES[id]}{id === cur && <span className="tag">{t('正在用')}</span>}{busy === id && <span className="muted small"> · {t('切换中…')}</span>}</b>
                  <span className="muted small">{desc[id]}</span>
                  {why && <span className="pc-choice-why small">{why}</span>}
                  {id === 'instabox' && (
                    <span className="pc-choice-key small">
                      <span className="muted">{key === 'saved' ? t('API Key：在这里填过') : key === 'env' ? t('API Key：来自环境变量 INSTABOX_API_KEY') : t('还没填 API Key')}</span>
                      {!keyOpen && <button type="button" className="ghost-btn" onClick={e => { e.preventDefault(); setKeyOpen(true) }}>{key ? t('换 API Key') : t('填 API Key')}</button>}
                    </span>)}
                </span>
              </label>)
          })}
        </div>
        {keyOpen && <InstaboxKey saved={key === 'saved'} onClose={() => setKeyOpen(false)} onDone={async () => {
          setKeyOpen(false); setKeyVer(v => v + 1)
          if (cur !== 'instabox') await save({ computer_backend: 'instabox' }, t('已换成{name}，之后的任务都用它', { name: PC_NAMES.instabox }))
          else toast(t('已保存'))
        }} />}
      </Section>
      {cur === 'instabox' && botId !== undefined && can('instabox')?.ok &&
        <Section title={t('云电脑用哪台机器')} desc={t('Instabox 账号里已经有机器的话，可以直接用，不用新建（免费版只能有一台）。')}>
          <MachinePicker key={keyVer} botId={botId} onErr={e => { if (e) toast(e, true) }} always />
        </Section>}
      <Section title={t('同时用电脑的任务')} desc={t('几个后台任务可以同时用电脑，每个开一个浏览器窗口，登录状态共用；多了排队。电脑内存小就调小（1–8）')}>
        <input className="pc-parallel" type="number" min={1} max={8} value={pcs} onChange={e => setPcs(Number(e.target.value))}
          onBlur={() => { if (pcs && pcs !== s.computer_parallel) save({ computer_parallel: pcs }, t('已保存，马上生效')) }} />
      </Section>
    </>
  )
}

/** 填云电脑（Instabox）的 API Key：服务器先试一下能不能用，能用才加密存起来。saved：以前在这里填过（可以删掉，回到环境变量）。 */
function InstaboxKey({ saved, onDone, onClose }: { saved: boolean; onDone: () => void; onClose: () => void }) {
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const save = async (k: string) => {
    setBusy(true); setErr('')
    try { setSettings(await api<Settings>('/api/computer/instabox-key', { method: 'PUT', body: { key: k } })); onDone() }
    catch (e: any) { setErr(e.message) }
    finally { setBusy(false) }
  }
  return (
    <div className="pc-key">
      <div className="muted small">
        {tn('云电脑用的是 Instabox：在 {site} 注册，建一个 API Key（权限要有 machines:write），粘贴到这里。Key 加密保存，之后不会再显示。',
          { site: <a href="https://instabox.sh" target="_blank" rel="noreferrer">instabox.sh</a> })}
      </div>
      <div className="env-row">
        <input type="password" value={key} autoFocus placeholder="API Key" autoComplete="off" disabled={busy}
          onChange={e => setKey(e.target.value)} onKeyDown={e => { if (isEnter(e) && key.trim()) save(key) }} />
        <button className="btn primary" disabled={busy || !key.trim()} onClick={() => save(key)}>{busy ? t('检查中…') : t('保存')}</button>
        <button className="btn" disabled={busy} onClick={onClose}>{t('取消')}</button>
      </div>
      {saved && <button className="ghost-btn" disabled={busy} onClick={() => save('')}>{t('删掉这里填的 Key')}</button>}
      {err && <div className="error-line">{err}</div>}
    </div>
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

/** 还没给 macOS 权限时：Bot 只能用浏览器。给了“辅助功能”和“屏幕录制”，它就能看屏幕、用鼠标键盘操作别的 App。
 *  写明缺的是哪一个；显示着的时候每隔几秒、切回这个窗口时都重新问一次，在系统设置里打开以后这里自己消失。
 *  here：就在那台 Mac 上，可以点按钮弹系统的授权提示、打开系统设置。 */
function DeskPerms({ desk, here, grant, refresh }: {
  desk: DesktopState; here: boolean; grant: () => Promise<unknown>; refresh: () => void
}) {
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const tick = () => { if (!document.hidden) refresh() }
    const i = setInterval(tick, 3000)
    window.addEventListener('focus', tick)
    return () => { clearInterval(i); window.removeEventListener('focus', tick) }
  }, [])
  if (desk.accessibility === undefined && desk.screen === undefined)   // 没问到（那台 Mac 没回应之类）：直接说原因
    return desk.problem ? <div className="pc-perm"><div className="pc-perm-head"><Icon name="alert" size={15} /><span>{desk.problem}</span></div></div> : null
  const items: [string, string, boolean][] = [
    [t('辅助功能'), t('用鼠标键盘操作其他 App'), !!desk.accessibility],
    [t('屏幕录制'), t('看屏幕'), !!desk.screen],
  ]
  const missing = items.filter(x => !x[2]).length
  const go = async () => {
    setBusy(true)
    try { await grant() } catch (e: any) { notice(e.message) }
    setBusy(false); refresh()
  }
  return (
    <div className="pc-perm">
      <div className="pc-perm-head">
        <Icon name="alert" size={15} />
        <span><b>{missing > 1 ? t('还差 2 项 macOS 权限') : t('还差 1 项 macOS 权限')}</b>
          <span className="muted">{t('现在 Bot 在这台 Mac 上只能用浏览器')}</span></span>
        {here ? <button className="btn small" disabled={busy} onClick={go}>{busy ? t('打开中…') : t('去打开')}</button>
          : <span className="muted small">{t('在那台 Mac 上打开')}</span>}
      </div>
      <ul className="pc-perm-list">
        {items.map(([name, use, ok]) => (
          <li key={name} className={ok ? 'ok' : ''}>
            <Icon name={ok ? 'check' : 'x'} size={13} /><b>{name}</b><span className="muted">{ok ? t('已打开') : use}</span>
          </li>))}
      </ul>
      {here && <p className="muted small">{t('在系统设置的列表里打开 Open Super Agent；列表里没有的话，点下面的“+”在“应用程序”里选它。打开后这里自己更新（系统要是提示“退出并重新打开”，照做就行）。')}</p>}
    </div>
  )
}

/** 一台 Mac 的终端：默认关，哪台设备上都能开关（手机上也行），打开后 Bot 每条命令都要你确认。 */
function MacShell({ m }: { m: MacState }) {
  const save = async (v: boolean) => {
    if (v && !(await ask(t('让 Bot 能在「{name}」上执行终端命令？每条命令执行前都会问你，命令能访问那台 Mac 上你能访问的所有文件。', { name: m.name }), { ok: t('打开终端') }))) return
    try { await api(`/api/computer/macs/${m.id}`, { method: 'PUT', body: { shell: v } }) }
    catch (e: any) { notice(e.message) }
  }
  return (
    <label className="pc-shell muted small">
      <input type="checkbox" checked={m.shell} disabled={!m.can_shell && !m.shell} onChange={e => save(e.target.checked)} />
      {t('允许 Bot 用终端（每条命令都要你确认）')}
      {!m.can_shell && <>{' · '}{t('那台 Mac 上的 App 要先更新到新版')}</>}
    </label>
  )
}

const reloadMacs = () => api<MacState[]>('/api/computer/macs').then(setMacs).catch(() => {})

/** 一台 Mac：连没连着、有几个任务在用（可以看画面）、你接手了就交还、缺的权限、终端开关、移除。here：就是这个 App 所在的这台。 */
function MacRow({ m, here, onRemove }: { m: MacState; here: boolean; onRemove: () => void }) {
  const [view, setView] = useState(false)
  const act = (a: string) => api(`/api/computer/macs/${m.id}/${a}`, { method: 'POST' })
  const desk = m.desktop
  const n = m.users?.length || 0
  return (
    <div className="pc-mac">
      <div className="pc-mac-bar">
        <span className={'pc-dot ' + (m.connected ? 'running' : 'error')} />
        <span className="pc-mac-name">{m.name}{here && <span className="muted small"> · {t('这台')}</span>}</span>
        <span className="muted small">{!m.connected ? t('没连上') : n ? t('{n} 个任务在用', { n }) : t('在线')}</span>
        <span className="grow" />
        {m.connected && n > 0 && <button className="ghost-btn" onClick={() => setView(v => !v)}>{view ? t('收起画面') : t('看画面')}</button>}
        <button className="ghost-btn" disabled={n > 0} title={n ? t('有任务在用，做完再移除') : ''} onClick={onRemove}>{t('移除')}</button>
      </div>
      {!m.connected && <p className="muted small">{here ? t('正在连接…（一直连不上就退出 App 再打开）') : t('那台 Mac 上的 App 没开、没联网，或者电脑睡着了。')}</p>}
      {view && m.connected && <HostView url={`/api/computer/macs/${m.id}/shot`} />}
      {m.connected && m.controller === 'human' && (
        <div className="pc-actions">
          <span className="pc-you">{desk?.paused_by === 'input' ? t('你动了鼠标或键盘，Bot 先停下了。用完点交还，它接着做') : t('Bot 已暂停，你直接用这台 Mac')}</span>
          <button className="btn primary" onClick={() => act('handback').catch((e: any) => notice(e.message))}>{t('交还给 Bot')}</button>
        </div>
      )}
      {m.connected && desk?.supported && !(desk.accessibility && desk.screen) &&
        <DeskPerms desk={desk} here={here} grant={() => act('grant')} refresh={reloadMacs} />}
      {m.connected && <MacShell m={m} />}
    </div>
  )
}

/** 你的 Mac：服务器上的 Bot 能把某个任务派到这些 Mac 上做（任务要用那台 Mac 上才有的东西时），每次派过去先问你。
 *  连着服务器的 Mac App 里能把这台加进来（App 先在 Mac 上弹窗问你）；哪台设备上都能看状态、开关终端、移除。 */
function Macs() {
  const macs = useStore(s => s.macs)
  const [ls, setLs] = useState<LinkState | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const reloadLink = () => { if (desktop?.link && !phone) desktop.link.state().then(setLs).catch(() => setLs(null)) }
  useEffect(() => { reloadMacs(); reloadLink() }, [])
  const mine = ls?.available && ls.linked ? macs?.find(m => m.id === ls.id) : undefined   // 这个 App 所在的这台
  const add = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api<{ id: number; token: string }>('/api/computer/links', { method: 'POST', body: { name: 'Mac' } })
      if (!(await desktop!.link!.connect(r.token, r.id)))   // 你在 Mac 的弹窗里点了取消
        await api(`/api/computer/links/${r.id}`, { method: 'DELETE' })
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(false); reloadLink(); reloadMacs() }
  }
  const remove = async (m: MacState) => {
    if (!(await ask(t('移除「{name}」？之后 Bot 不能再把任务派到这台 Mac；要再加，得在那台 Mac 的 App 里重新允许。', { name: m.name }), { ok: t('移除'), danger: true }))) return
    setErr('')
    try {
      await api(`/api/computer/links/${m.id}`, { method: 'DELETE' })
      if (mine?.id === m.id) await desktop!.link!.disconnect()
    } catch (e: any) { setErr(e.message) }
    reloadLink(); reloadMacs()
  }
  if (!macs || (!macs.length && !ls?.available)) return null   // 没有登记过的 Mac，也不在 Mac 的 App 里：不占地方
  return (
    <div className="pc-macs">
      <div className="pc-macs-head">
        <b>{t('你的 Mac')}</b>
        <span className="muted small">{t('有的事只有你的 Mac 上才做得了（那台 Mac 上登录着的 App、本地文件）：Bot 可以把那个任务派过去做，每次先问你。')}</span>
      </div>
      {macs.map(m => <MacRow key={m.id} m={m} here={mine?.id === m.id} onRemove={() => remove(m)} />)}
      {ls?.available && !mine && <button className="btn" disabled={busy} onClick={add}>{busy ? t('等你在弹窗里确认…') : t('允许 Bot 把任务派到这台 Mac')}</button>}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

/** 本机电脑（或者你的某台 Mac）的画面：没有 VNC，每隔一会儿取一张截图（手机上也能看）：跟着 Bot 最近一次操作的走，
 *  它在操作屏幕时是整个屏幕，在浏览器里做事时是它的浏览器窗口。url 是取截图的地址。 */
export function HostView({ url: shot }: { url: string }) {
  const [src, setSrc] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let stop = false, busy = false, url = ''
    const tick = async () => {
      if (busy || document.hidden) return
      busy = true
      try {
        const r = await fetch(shot, { cache: 'no-store' })
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
  }, [shot])
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
function MachinePicker({ botId, onErr, always }: { botId: number; onErr: (e: string) => void; always?: boolean }) {
  const [list, setList] = useState<Machine[] | null>(null)
  const [fixed, setFixed] = useState(false)
  const [open, setOpen] = useState(!!always)
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
  if (always && list && fixed) return <p className="muted small">{t('服务器的配置里定了用哪台机器（computer.instabox_machine），这里不能换。')}</p>
  if (always && list && !list.length) return <p className="muted small">{t('账号里还没有机器：第一次用电脑时会自动新建一台。')}</p>
  if (!list || fixed || !list.length) return null
  const others = always ? list : list.filter(m => !m.current)
  if (!others.length) return null
  if (!open) return <button className="ghost-btn" onClick={() => setOpen(true)}>{t('换成账号里已有的另一台电脑')}</button>
  return (
    <div className="machine-list">
      {!always && <div className="muted small">{t('你的 Instabox 账号里已经有 {n} 台电脑，可以直接用，不用新建：', { n: list.length })}</div>}
      {others.map(m => (
        <div key={m.id} className="kv">
          <span>{m.name || m.id}<span className="muted small"> · {STATE[m.state] || m.state}{m.template && ` · ${m.template}`}{m.ours && ' · ' + t('之前为这里创建的')}</span></span>
          {m.current ? <span className="tag">{t('正在用')}</span> : <button className="btn" onClick={() => use(m.id)}>{t('用这台')}</button>}
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
  const settings = useStore(s => s.settings)
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
  const host = st.backend === 'host'   // 用户自己的电脑：桌面 App 所在的这台 Mac
  const live = st.state === 'running' || (host && !!st.desktop?.screen)   // 有画面：浏览器开着，或者能看整个屏幕
  const human = st.controller === 'human'
  const view = host ? <HostView url={`/api/bots/${botId}/computer/shot`} /> : <ComputerView botId={botId} interactive={human} />
  return (
    <div className="pc-panel">
      <div className="pc-bar">
        <span className={'pc-dot ' + (host ? 'running' : st.state)} />
        <span className="pc-name">{PC_NAMES[st.backend] || t('Bot 电脑')}</span>
        {!host && <span className="muted small">{STATE[st.state] || st.state}</span>}
        <span className="grow" />
        {live && <button className="ghost-btn" onClick={() => setBig(true)} title={t('放大')}>⤢ {t('放大')}</button>}
        {!settings?.security?.demo && <button className="ghost-btn" onClick={openPcSettings} title={t('在设置里换用哪台电脑')}>{t('换电脑')}</button>}
      </div>
      {live && <Users st={st} />}
      {live ? view : (
        <div className="pc-empty">
          {host ? <p className="muted small">{t('Bot 用这台电脑做事时，画面会显示在这里。')}</p>
            : <p className="muted small">{t('所有 Bot 共用一台电脑（浏览器 + 终端）。几个任务可以同时用，每个任务一个浏览器窗口，登录状态和文件共用；')}
            {' '}{t('最多同时 {n} 个，多了排队。派出的任务需要时会自动启动，闲置 15 分钟自动暂停。', { n: st.parallel || 3 })}</p>}
          {st.state === 'error' && st.detail && <div className="error-line">{st.detail}</div>}
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
      {host && st.desktop?.supported && !(st.desktop.accessibility && st.desktop.screen) &&
        <DeskPerms desk={st.desktop} here grant={() => api<ComputerState>('/api/computer/desktop/grant', { method: 'POST' }).then(setComputer)}
          refresh={() => api<ComputerState>(`/api/bots/${botId}/computer`).then(setComputer).catch(() => {})} />}
      {st.backend === 'host' && <HostShell on={!!st.shell} />}
      {!settings?.computer_on_desktop && <Macs />}
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
