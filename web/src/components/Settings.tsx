import { useEffect, useState } from 'react'
import { api, refreshLists, useStore } from '../store'
import type { Settings as S } from '../types'

const ROLE_NAMES: Record<string, string> = {
  router: '对话（Bot 本身）', worker: '后台任务', reviewer: 'Heavy 汇总审稿', filter: '触发器过滤',
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
          {[['models', '模型'], ['limits', '限制与预算'], ['usage', '费用'], ['apps', '应用与触发器']].map(([k, v]) =>
            <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{v}</button>)}
          <span className="grow" /><button className="icon-btn" onClick={onClose}>✕</button>
        </div>
        <div className="modal-content">
          {tab === 'models' && <Models />}
          {tab === 'limits' && <Limits />}
          {tab === 'usage' && <Usage />}
          {tab === 'apps' && <Apps />}
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

function Apps() {
  const [s, setS] = useState<any>(null)
  useEffect(() => { api('/api/status').then(setS) }, [])
  if (!s) return <div className="muted">加载中…</div>
  return (
    <div>
      <h4>应用（MCP）</h4>
      {Object.entries(s.mcp).map(([k, v]) => <div className="kv" key={k}><span>{k}</span><span className="muted">{String(v)}</span></div>)}
      <p className="muted small">在 config.yaml 的 mcp_servers 里添加和启用。下一阶段会改成在这里直接连接。</p>
      <h4>触发器</h4>
      {s.triggers.map((t: any) => <div className="kv" key={t.name}><span>{t.name}（{t.type}）</span><span className="muted">{t.enabled ? '启用' : '关闭'}</span></div>)}
      <h4>最近触发</h4>
      {s.trigger_log.length ? s.trigger_log.map((l: any, i: number) => <div className="kv" key={i}><span>{l.ts} {l.trigger}：{l.detail}</span><span className="muted">{l.verdict}</span></div>) : <div className="muted small">暂无</div>}
    </div>
  )
}
