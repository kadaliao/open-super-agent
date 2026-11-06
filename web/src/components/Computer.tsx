import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { api, useStore, setComputer, setTeachDraft } from '../store'
import type { ComputerState, Teach } from '../types'
import BotAvatar from './BotAvatar'

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`
const touch = typeof matchMedia !== 'undefined' && matchMedia('(pointer: coarse)').matches

/** 演示录制中：计时、完成（生成技能草稿）、放弃。 */
function TeachBar({ t }: { t: Teach }) {
  const [now, setNow] = useState(Date.now())
  const [busy, setBusy] = useState('')
  const bot = useStore(s => s.bots.find(b => b.id === t.bot_id))
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(i) }, [])
  const act = async (a: 'stop' | 'cancel') => {
    if (a === 'cancel' && !confirm('放弃这段录制？录到的操作不会保存。')) return
    setBusy(a)
    try {
      const r = await api(`/api/teach/${a}`, { method: 'POST' })
      if (a === 'stop') setTeachDraft({ bot_id: r.bot_id, draft: r.draft, steps: r.steps })
    } catch (e: any) { alert(e.message) }
    setBusy('')
  }
  const left = Math.max(0, t.max_s - (now / 1000 - t.started))
  return (
    <div className="teach-bar">
      <span className="rec-dot" />
      <span className="teach-text">{busy === 'stop' ? `正在把操作整理成 ${bot?.name || 'Bot'} 的技能…` : `正在录制演示 ${mmss(now / 1000 - t.started)}`}
        <span className="muted small">{busy ? '' : ` · 还能录 ${mmss(left)} · 只记浏览器里的操作，密码不会记录`}</span></span>
      <button className="btn" disabled={!!busy} onClick={() => act('cancel')}>放弃</button>
      <button className="btn primary" disabled={!!busy} onClick={() => act('stop')}>{busy === 'stop' ? '整理中…' : '完成，生成技能'}</button>
    </div>
  )
}

/** 接手时在手机上输入：点画面弹不出键盘，就在这里打字再发给电脑。 */
function TypeBar({ botId }: { botId: number }) {
  const [text, setText] = useState('')
  const send = (body: any) => api(`/api/bots/${botId}/computer/type`, { method: 'POST', body }).catch((e: any) => alert(e.message))
  return (
    <div className="type-bar">
      <input value={text} placeholder="输入文字发到电脑（先在画面上点一下输入框）" onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && text) { send({ text }); setText('') } }} />
      <button className="btn" disabled={!text} onClick={() => { send({ text }); setText('') }}>发送</button>
      <button className="btn" onClick={() => send({ key: 'Return' })}>回车</button>
      <button className="btn" onClick={() => send({ key: 'BackSpace' })}>⌫</button>
    </div>
  )
}

const STATE: Record<string, string> = {
  none: '还没启动', starting: '启动中', running: '运行中', paused: '已暂停', stopped: '已停止', error: '出错了',
}
const BACKEND: Record<string, string> = { local: '本地 Docker', instabox: 'Instabox 云端' }

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
      {conn !== 'connected' && <div className="pc-overlay">{conn === 'connecting' ? '正在连接画面…' : '画面已断开，正在重连…'}</div>}
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
    api<ComputerState>('/api/computer/watch', { method: 'POST', body: { key } }).then(setComputer).catch((e: any) => alert(e.message))
  return (
    <div className="pc-users">
      <span className="muted small">{users.length > 1 ? `${users.length} 个任务同时在用` : '在用'}</span>
      {users.map(u => {
        const bot = bots.find(b => b.id === u.bot_id)
        const on = st.watch === u.key
        const last = u.task_id ? tasks[u.task_id]?.last_action : ''
        return (
          <button key={u.key} className={'chip pc-user' + (on ? ' on' : '')} disabled={!u.window}
            title={(last ? last + '\n' : '') + (!u.window ? '还没打开浏览器' : on ? '画面固定在这个任务的窗口上，再点一下取消' : '在画面上看这个任务')}
            onClick={() => watch(on ? null : u.key)}>
            {bot && <BotAvatar avatar={bot.avatar} size={14} />}{u.label}{on && <span className="pin">· 正在看</span>}
          </button>
        )
      })}
      {!!st.waiting?.length && <span className="muted small" title={st.waiting.join('、')}>{st.waiting.length} 个在排队</span>}
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
  if (!open) return <button className="ghost-btn" onClick={() => setOpen(true)}>换成账号里已有的另一台电脑</button>
  return (
    <div className="machine-list">
      <div className="muted small">你的 Instabox 账号里已经有 {list.length} 台电脑，可以直接用，不用新建：</div>
      {others.map(m => (
        <div key={m.id} className="kv">
          <span>{m.name || m.id}<span className="muted small"> · {STATE[m.state] || m.state}{m.template && ` · ${m.template}`}{m.ours && ' · 之前为这里创建的'}</span></span>
          <button className="btn" onClick={() => use(m.id)}>用这台</button>
        </div>
      ))}
    </div>
  )
}

export function ComputerPanel({ botId }: { botId: number }) {
  const st = useStore(s => s.computers[botId])
  const task = useStore(s => Object.values(s.tasks).find(t => t.bot_id === botId && t.status === 'running'))
  const teach = useStore(s => s.teach)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [big, setBig] = useState(false)
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

  if (!st) return <div className="muted small">读取电脑状态…</div>
  const running = st.state === 'running'
  const human = st.controller === 'human'
  return (
    <div className="pc-panel">
      <div className="pc-bar">
        <span className={'pc-dot ' + st.state} />
        <span>{STATE[st.state] || st.state}</span>
        <span className="muted small">· {BACKEND[st.backend] || st.backend}</span>
        <span className="grow" />
        {running && <button className="ghost-btn" onClick={() => setBig(true)} title="放大">⤢ 放大</button>}
      </div>
      {running && <Users st={st} />}
      {running ? <ComputerView botId={botId} interactive={human} /> : (
        <div className="pc-empty">
          <p className="muted small">所有 Bot 共用一台电脑（浏览器 + 终端）。几个任务可以同时用，每个任务一个浏览器窗口，登录状态和文件共用；
            最多同时 {st.parallel || 3} 个，多了排队。派出的任务需要时会自动启动，闲置 15 分钟自动暂停。</p>
          {st.backend === 'instabox' && <MachinePicker key={st.detail} botId={botId} onErr={setErr} />}
          <button className="btn primary" disabled={!!busy} onClick={() => act('start')}>{busy === 'start' ? '启动中…（首次需要一会儿）' : st.state === 'paused' ? '恢复' : st.state === 'none' ? '新建并启动电脑' : '启动电脑'}</button>
        </div>
      )}
      {teach && <TeachBar t={teach} />}
      {running && human && touch && <TypeBar botId={botId} />}
      {running && !teach && (
        <div className="pc-actions">
          {human ? <>
            <span className="pc-you">你正在操作</span>
            <button className="btn primary" onClick={() => act('handback')}>交还给 Bot</button>
          </> : <>
            <span className="muted small">{task?.last_action ? `Bot：${task.last_action}` : 'Bot 控制中（你只能看）'}</span>
            <span className="grow" />
            <button className="btn" onClick={() => { act('takeover'); setBig(true) }}>接手</button>
            <button className="btn" disabled={!!busy} onClick={() => act('pause')}>暂停</button>
          </>}
        </div>
      )}
      {err && <div className="error-line">{err}</div>}
      {big && running && (
        <div className="modal-bg" onMouseDown={() => setBig(false)}>
          <div className="pc-big" onMouseDown={e => e.stopPropagation()}>
            <div className="pc-bar">
              <span className={'pc-dot ' + st.state} /><b>电脑</b>
              <span className="muted small">{human ? '· 你正在操作' : '· Bot 控制中'}</span><span className="grow" />
              {teach ? null : human ? <button className="btn primary" onClick={() => act('handback')}>交还给 Bot</button>
                : <button className="btn" onClick={() => act('takeover')}>接手</button>}
              <button className="icon-btn" onClick={() => setBig(false)}>✕</button>
            </div>
            {teach && <TeachBar t={teach} />}
            <Users st={st} />
            <ComputerView botId={botId} interactive={human} />
            {human && touch && <TypeBar botId={botId} />}
          </div>
        </div>
      )}
    </div>
  )
}
