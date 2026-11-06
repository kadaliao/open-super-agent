import { useEffect, useRef, useState } from 'react'
import { api } from '../store'

// 设置 → 备份与迁移：导出一个加密的备份文件；在新机器上上传它恢复
type Info = { counts: Record<string, number>; secrets: number; started: number; version: string; staged: Staged | null }
type Staged = { created: number; version: string; counts: Record<string, number>; secrets: number; unreadable: string[]; local_apps: number; channels_off: number }
const LABELS: [string, string][] = [['bots', '个 Bot'], ['chats', '个对话'], ['messages', '条消息'], ['memories', '条记忆'], ['skills', '个技能'],
  ['routines', '个定时任务'], ['pages', '个页面'], ['apps', '个应用'], ['channels', '个聊天软件连接']]
const summary = (c: Record<string, number>, secrets: number) =>
  LABELS.filter(([k]) => c[k]).map(([k, v]) => `${c[k]} ${v}`).concat(secrets ? [`${secrets} 个密钥`] : []).join('、')
const when = (t: number) => new Date(t * 1000).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const b64 = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)))

export default function Backup() {
  const [info, setInfo] = useState<Info | null>(null)
  const load = () => api('/api/backup').then(setInfo)
  useEffect(() => { load() }, [])
  if (!info) return <div className="muted">加载中…</div>
  return (
    <div>
      <p className="muted">把这台的全部数据打成一个加密文件：换机器、从本机搬到服务器时，在新的那台上传它就能接着用。</p>
      <Export info={info} />
      <Restore info={info} onChange={load} />
    </div>
  )
}

function Export({ info }: { info: Info }) {
  const [p1, setP1] = useState('')
  const [p2, setP2] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [done, setDone] = useState('')
  const ok = p1.length >= 8 && p1 === p2
  const go = async () => {
    setBusy(true); setErr(''); setDone('')
    try {
      const r = await fetch('/api/backup/export', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ passphrase: p1 }) })
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `导出失败（${r.status}）`)
      const name = decodeURIComponent((r.headers.get('content-disposition') || '').match(/filename\*?=(?:UTF-8'')?"?([^";]+)/)?.[1] || 'osa-backup.osabak')
      const blob = await r.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob); a.download = name; a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 10_000)
      const bad = Number(r.headers.get('x-backup-unreadable') || 0)
      setDone(`已下载 ${name}（${(blob.size / 1024 / 1024).toFixed(1)} MB）。口令请另外记好，忘了就打不开。` +
        (bad ? ` 有 ${bad} 个密钥解不开（主密钥换过），没有带上。` : ''))
      setP1(''); setP2('')
    } catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  return (
    <section className="bk-box">
      <h4>导出</h4>
      <p className="small">会带上：{summary(info.counts, info.secrets)}，还有资料库页面、应用配置、确认规则、费用记录。
        不带电脑里的文件和浏览器的登录状态。</p>
      <p className="muted small">备份里有你所有的密钥（应用的 token、邮箱授权码、聊天软件的机器人密钥），所以要设一个口令加密，恢复时要输同一个口令。</p>
      <div className="bk-row">
        <input type="password" autoComplete="new-password" placeholder="口令（至少 8 个字符）" value={p1} onChange={e => setP1(e.target.value)} />
        <input type="password" autoComplete="new-password" placeholder="再输一遍" value={p2} onChange={e => setP2(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && ok && go()} />
        <button className="btn primary" disabled={!ok || busy} onClick={go}>{busy ? '打包中…' : '下载备份'}</button>
      </div>
      {p2 && p1 !== p2 && <div className="muted small">两次输入的不一样</div>}
      {err && <div className="error-line">{err}</div>}
      {done && <div className="ok-line small">{done}</div>}
      <p className="muted small">服务器上可以定时备份：<code>python -m osa.backup export -o 目录</code>，口令放在环境变量 <code>OSA_BACKUP_PASSPHRASE</code> 里。</p>
    </section>
  )
}

function Restore({ info, onChange }: { info: Info; onChange: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [pass, setPass] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const staged = info.staged
  const upload = async () => {
    if (!file) return
    setBusy('解密检查中…'); setErr('')
    try {
      const r = await fetch('/api/backup/restore', { method: 'PUT', headers: { 'x-passphrase': b64(pass) }, body: file })
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `上传失败（${r.status}）`)
      setPass(''); setFile(null); onChange()
    } catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  const cancel = async () => { await api('/api/backup/restore', { method: 'DELETE' }); onChange() }
  const confirm_ = async () => {
    if (!confirm('用这个备份替换这台的全部数据？正在做的任务会被打断。原来的数据库会在服务器上留一份。')) return
    setBusy('正在重启…'); setErr('')
    try {
      await api('/api/backup/restore/confirm', { method: 'POST' })
      const t0 = Date.now()
      for (;;) {   // 等服务重启回来：started 变了就是新进程
        await new Promise(r => setTimeout(r, 1500))
        try { const x = await api('/api/backup'); if (x.started !== info.started) break } catch { /* 还在重启 */ }
        if (Date.now() - t0 > 90_000) {
          setBusy(''); setErr('服务没有自动起来：手动启动它，启动时会换上备份。'); return
        }
      }
      location.href = '/'
    } catch (e: any) { setErr(e.message); setBusy('') }
  }
  return (
    <section className="bk-box">
      <h4>从备份恢复</h4>
      {staged ? <>
        <p className="small"><b>{when(staged.created)}</b> 的备份{staged.version ? `（v${staged.version}）` : ''}：{summary(staged.counts, staged.secrets)}。</p>
        <ul className="small bk-notes">
          <li>会<b>替换</b>这台现在的全部数据，原来的数据库在服务器上留一份（<code>data/osa.db.before-restore-时间</code>）。</li>
          <li>电脑不跟着走：这台会按自己的设置开电脑，原来电脑里的文件不在备份里。</li>
          {staged.channels_off > 0 && <li>{staged.channels_off} 个聊天软件连接会先停用：确认原来那台已经停掉，再到“聊天软件”里启用，不然两边会抢同一个机器人。</li>}
          <li>原来那台如果还开着，记得停掉，不然定时任务会跑两遍。</li>
          {staged.unreadable?.length > 0 && <li>备份时有 {staged.unreadable.length} 个密钥解不开，没有带过来，用到它们的应用要重新填。</li>}
        </ul>
        {err && <div className="error-line">{err}</div>}
        <div className="row-actions">
          <button className="btn danger" disabled={!!busy} onClick={confirm_}>{busy || '恢复并重启'}</button>
          <button className="btn" disabled={!!busy} onClick={cancel}>不恢复了</button>
        </div>
      </> : <>
        <p className="muted small">上传之前导出的 .osabak 文件，先解密检查，给你看里面有什么，确认后才替换。</p>
        <div className="bk-row">
          <button className="btn" onClick={() => input.current?.click()}>{file ? file.name : '选择备份文件…'}</button>
          <input ref={input} type="file" accept=".osabak" hidden onChange={e => { setFile(e.target.files?.[0] || null); e.target.value = '' }} />
          <input type="password" autoComplete="off" placeholder="备份时设的口令" value={pass} onChange={e => setPass(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && file && pass && upload()} />
          <button className="btn primary" disabled={!file || !pass || !!busy} onClick={upload}>{busy || '检查备份'}</button>
        </div>
        {err && <div className="error-line">{err}</div>}
      </>}
    </section>
  )
}
