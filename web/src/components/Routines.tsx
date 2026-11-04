import { useEffect, useState } from 'react'
import { api, loadSkills, useStore } from '../store'
import type { Bot, Routine, RoutineRun, Skill, Trigger } from '../types'

const when = (ts: number | null) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
const DAYS = ['一', '二', '三', '四', '五', '六', '日']
const STATUS: Record<string, string> = {
  running: '运行中', done: '完成', failed: '失败', cancelled: '已取消', skipped: '跳过', filtered: '被过滤',
}
const SOURCE: Record<string, string> = { schedule: '定时', webhook: 'webhook', test: '测试' }

export function describe(t: Trigger) {
  if (t.kind === 'every') return t.minutes % 60 === 0 ? `每 ${t.minutes / 60} 小时` : `每 ${t.minutes} 分钟`
  if (t.kind === 'daily') {
    const d = !t.days.length || t.days.length === 7 ? '每天' : '每周' + t.days.map(x => DAYS[x - 1]).join('、')
    return `${d} ${t.times.join('、')}`
  }
  return '收到 webhook 时'
}

// ---------- 技能 ----------
export function SkillsPanel({ bot }: { bot: Bot }) {
  const skills = useStore(s => s.skills[bot.id])
  const [edit, setEdit] = useState<Partial<Skill> & { shared?: boolean } | null>(null)
  useEffect(() => { loadSkills(bot.id) }, [bot.id])
  if (edit) return <SkillEditor bot={bot} skill={edit} onDone={() => { setEdit(null); loadSkills(bot.id) }} />
  return (
    <div>
      <div className="section-head"><h4>技能</h4><button className="btn" onClick={() => setEdit({})}>＋ 新建</button></div>
      <p className="muted small">在对话里输入 /名字 调用。也可以让 {bot.name} “把刚才的做法存成技能”。</p>
      {!skills ? <div className="muted">加载中…</div> : skills.length ? skills.map(k => (
        <button key={k.id} className="list-item" onClick={() => setEdit({ ...k, shared: k.bot_id === null })}>
          <b>/{k.name}</b>{k.bot_id === null && <span className="tag">共用</span>}{k.source === 'chat' && <span className="tag">对话里保存</span>}
          <div className="muted small">{k.description || k.body.slice(0, 60)}</div>
        </button>
      )) : <div className="muted small">还没有技能</div>}
    </div>
  )
}

function SkillEditor({ bot, skill, onDone }: { bot: Bot; skill: Partial<Skill> & { shared?: boolean }; onDone: () => void }) {
  const [name, setName] = useState(skill.name || '')
  const [desc, setDesc] = useState(skill.description || '')
  const [body, setBody] = useState(skill.body || '')
  const [shared, setShared] = useState(!!skill.shared)
  const [err, setErr] = useState('')
  const save = async () => {
    try {
      const data = { name, description: desc, body, shared, bot_id: bot.id }
      if (skill.id) await api(`/api/skills/${skill.id}`, { method: 'PUT', body: data })
      else await api(`/api/bots/${bot.id}/skills`, { method: 'POST', body: data })
      onDone()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!confirm(`删除技能 /${skill.name}？`)) return
    await api(`/api/skills/${skill.id}`, { method: 'DELETE' }); onDone()
  }
  return (
    <div>
      <h4>{skill.id ? '编辑技能' : '新建技能'}</h4>
      <label className="field">名字（/ 后面的部分）<input value={name} maxLength={40} placeholder="例如：日报" onChange={e => setName(e.target.value)} /></label>
      <label className="field">一句话说明<input value={desc} maxLength={200} placeholder="什么时候用" onChange={e => setDesc(e.target.value)} /></label>
      <label className="field">做法<textarea rows={10} value={body} placeholder="步骤、要用的网站或应用、输出格式……" onChange={e => setBody(e.target.value)} /></label>
      <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> 所有 Bot 都能用</label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>保存</button>
        <button className="btn" onClick={onDone}>返回</button>
        {skill.id && <button className="btn danger" onClick={del}>删除</button>}
      </div>
    </div>
  )
}

// ---------- 定时任务 ----------
export function RoutinesPanel({ bot }: { bot: Bot }) {
  const [list, setList] = useState<Routine[] | null>(null)
  const [edit, setEdit] = useState<Partial<Routine> | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const [err, setErr] = useState('')
  const load = () => api(`/api/bots/${bot.id}/routines`).then(setList)
  useEffect(() => {
    load()
    const h = (e: Event) => { const id = (e as CustomEvent).detail; if (id == null || id === bot.id) load() }
    window.addEventListener('osa-routines', h)
    return () => window.removeEventListener('osa-routines', h)
  }, [bot.id])
  const act = async (r: Routine, action: string) => {
    setErr('')
    try { await api(`/api/routines/${r.id}/${action}`, { method: 'POST' }); load() } catch (e: any) { setErr(e.message) }
  }
  if (edit) return <RoutineEditor bot={bot} routine={edit} onDone={() => { setEdit(null); load() }} />
  return (
    <div>
      <div className="section-head"><h4>定时任务</h4><button className="btn" onClick={() => setEdit({})}>＋ 新建</button></div>
      <p className="muted small">到点或收到 webhook 时，{bot.name} 会在后台做一次，结果发到对话里。也可以直接在对话里说“每天 9 点帮我……”。</p>
      {err && <div className="error-line">{err}</div>}
      {!list ? <div className="muted">加载中…</div> : list.length ? list.map(r => (
        <div key={r.id} className={'routine' + (r.enabled ? '' : ' off')}>
          <div className="routine-head" onClick={() => setOpen(open === r.id ? null : r.id)}>
            <b>{r.name}</b>
            <span className="muted small">{describe(r.trigger)}{!r.enabled && ' · 已暂停'}{r.running && ' · 运行中'}</span>
          </div>
          <div className="muted small">
            {r.skill && <>技能 /{r.skill} · </>}
            {r.enabled && r.next_run ? `下次 ${when(r.next_run)}` : r.trigger.kind === 'webhook' ? `POST /hooks/${r.name}` : ''}
            {r.last_run && <> · 上次 {when(r.last_run.ts)} {STATUS[r.last_run.status] || r.last_run.status}</>}
          </div>
          <div className="app-actions">
            <button className="btn" onClick={() => act(r, 'test')} disabled={r.running}>测试</button>
            <button className="btn" onClick={() => act(r, r.enabled ? 'pause' : 'resume')}>{r.enabled ? '暂停' : '恢复'}</button>
            <button className="btn" onClick={() => setEdit(r)}>编辑</button>
            <button className="btn" onClick={() => setOpen(open === r.id ? null : r.id)}>记录</button>
          </div>
          {open === r.id && <Runs id={r.id} />}
        </div>
      )) : <div className="muted small">还没有定时任务</div>}
    </div>
  )
}

function Runs({ id }: { id: number }) {
  const [runs, setRuns] = useState<RoutineRun[] | null>(null)
  useEffect(() => {
    const load = () => api(`/api/routines/${id}/runs`).then(setRuns)
    load()
    window.addEventListener('osa-routines', load)
    return () => window.removeEventListener('osa-routines', load)
  }, [id])
  if (!runs) return <div className="muted small">加载中…</div>
  if (!runs.length) return <div className="muted small">还没运行过</div>
  return (
    <div className="runs">
      {runs.map(x => (
        <div key={x.id} className="kv" title={x.detail}>
          <span>{when(x.ts)} · {SOURCE[x.source] || x.source}{x.task_id ? ` · #${x.task_id}` : ''}{x.detail && ` · ${x.detail.slice(0, 40)}`}</span>
          <span className={'muted run-' + x.status}>{STATUS[x.status] || x.status}</span>
        </div>
      ))}
      <div className="muted small">保留最近 20 次</div>
    </div>
  )
}

function RoutineEditor({ bot, routine, onDone }: { bot: Bot; routine: Partial<Routine>; onDone: () => void }) {
  const skills = useStore(s => s.skills[bot.id])
  useEffect(() => { if (!skills) loadSkills(bot.id) }, [bot.id])
  const t0: any = routine.trigger || { kind: 'daily', times: ['09:00'], days: [] }
  const [name, setName] = useState(routine.name || '')
  const [prompt, setPrompt] = useState(routine.prompt || '')
  const [skill, setSkill] = useState(routine.skill || '')
  const [kind, setKind] = useState<Trigger['kind']>(t0.kind)
  const [times, setTimes] = useState((t0.times || ['09:00']).join(', '))
  const [days, setDays] = useState<number[]>(t0.days || [])
  const [minutes, setMinutes] = useState(String(t0.minutes || 60))
  const [include, setInclude] = useState((t0.include || []).join(', '))
  const [exclude, setExclude] = useState((t0.exclude || []).join(', '))
  const [filter, setFilter] = useState(t0.llm_filter || '')
  const [err, setErr] = useState('')
  const split = (s: string) => s.split(/[,，\s]+/).filter(Boolean)
  const save = async () => {
    const trigger = kind === 'daily' ? { kind, times: split(times), days }
      : kind === 'every' ? { kind, minutes: Number(minutes) }
      : { kind, include: split(include), exclude: split(exclude), llm_filter: filter }
    try {
      const data = { name, prompt, skill, trigger }
      if (routine.id) await api(`/api/routines/${routine.id}`, { method: 'PUT', body: data })
      else await api(`/api/bots/${bot.id}/routines`, { method: 'POST', body: data })
      onDone()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!confirm(`删除定时任务「${routine.name}」？运行记录也会删除。`)) return
    await api(`/api/routines/${routine.id}`, { method: 'DELETE' }); onDone()
  }
  return (
    <div>
      <h4>{routine.id ? '编辑定时任务' : '新建定时任务'}</h4>
      <label className="field">名字<input value={name} maxLength={40} placeholder={kind === 'webhook' ? '会出现在网址里，例如 github' : '例如：早报'} onChange={e => setName(e.target.value)} /></label>
      <label className="field">每次做什么<textarea rows={4} value={prompt} placeholder="例如：整理今天 AI 领域的 5 条新闻，附链接" onChange={e => setPrompt(e.target.value)} /></label>
      <label className="field">按技能做（可选）
        <select value={skill} onChange={e => setSkill(e.target.value)}>
          <option value="">不用技能</option>
          {(skills || []).map(k => <option key={k.id} value={k.name}>/{k.name}</option>)}
        </select>
      </label>
      <label className="field">什么时候
        <select value={kind} onChange={e => setKind(e.target.value as Trigger['kind'])}>
          <option value="daily">每天 / 每周固定时间</option>
          <option value="every">每隔一段时间</option>
          <option value="webhook">收到 webhook 时</option>
        </select>
      </label>
      {kind === 'daily' && <>
        <label className="field">时间（多个用逗号分开）<input value={times} placeholder="09:00, 18:00" onChange={e => setTimes(e.target.value)} /></label>
        <div className="day-row">
          {DAYS.map((d, i) => (
            <button key={d} className={days.includes(i + 1) ? 'on' : ''}
              onClick={() => setDays(days.includes(i + 1) ? days.filter(x => x !== i + 1) : [...days, i + 1].sort())}>{d}</button>
          ))}
          <span className="muted small">{days.length ? '' : '不选 = 每天'}</span>
        </div>
      </>}
      {kind === 'every' && <label className="field">间隔（分钟，至少 5）<input type="number" min={5} value={minutes} onChange={e => setMinutes(e.target.value)} /></label>}
      {kind === 'webhook' && <>
        <p className="muted small">地址：POST /hooks/{name || '名字'}。设置了 OSA_WEBHOOK_TOKEN 时请求头 X-OSA-Token 要一致，否则只接受本机请求。</p>
        <label className="field">至少包含其中一个词（可选）<input value={include} placeholder="issue, bug" onChange={e => setInclude(e.target.value)} /></label>
        <label className="field">包含就忽略（可选）<input value={exclude} placeholder="dependabot" onChange={e => setExclude(e.target.value)} /></label>
        <label className="field">让模型判断要不要处理（可选）<textarea rows={2} value={filter} placeholder="只有报告 bug 的才处理" onChange={e => setFilter(e.target.value)} /></label>
      </>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>保存</button>
        <button className="btn" onClick={onDone}>返回</button>
        {routine.id && <button className="btn danger" onClick={del}>删除</button>}
      </div>
    </div>
  )
}
