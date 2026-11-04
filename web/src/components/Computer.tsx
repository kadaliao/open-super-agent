import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { api, useStore, setComputer } from '../store'
import type { ComputerState } from '../types'

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
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const [big, setBig] = useState(false)

  useEffect(() => { api<ComputerState>(`/api/bots/${botId}/computer`).then(setComputer).catch(() => {}) }, [botId])
  const act = async (a: string) => {
    setBusy(a); setErr('')
    try { setComputer(await api(`/api/bots/${botId}/computer/${a}`, { method: 'POST' })) } catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  useEffect(() => {
    const h = (e: Event) => { if ((e as CustomEvent).detail === botId) { act('takeover'); setBig(true) } }
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
      {running ? <ComputerView botId={botId} interactive={human} /> : (
        <div className="pc-empty">
          <p className="muted small">所有 Bot 共用一台电脑（浏览器 + 终端），同一时间只有一个任务在用，其他排队。派出的任务需要时会自动启动，闲置 15 分钟自动暂停。</p>
          {st.backend === 'instabox' && <MachinePicker key={st.detail} botId={botId} onErr={setErr} />}
          <button className="btn primary" disabled={!!busy} onClick={() => act('start')}>{busy === 'start' ? '启动中…（首次需要一会儿）' : st.state === 'paused' ? '恢复' : st.state === 'none' ? '新建并启动电脑' : '启动电脑'}</button>
        </div>
      )}
      {running && (
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
              {human ? <button className="btn primary" onClick={() => act('handback')}>交还给 Bot</button>
                : <button className="btn" onClick={() => act('takeover')}>接手</button>}
              <button className="icon-btn" onClick={() => setBig(false)}>✕</button>
            </div>
            <ComputerView botId={botId} interactive={human} />
          </div>
        </div>
      )}
    </div>
  )
}
