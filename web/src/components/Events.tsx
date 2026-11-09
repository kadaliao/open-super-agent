import { useEffect, useMemo, useState } from 'react'
import { api, loadSkills, useStore } from '../store'
import type { Bot, EventSource, EventTrigger, Routine, Scenario, SourceField } from '../types'
import { t } from '../i18n'
import { ask } from './Dialog'
import { Runs, STATUS, describe, when } from './Routines'

// 事件：某件事发生时（收到 webhook、订阅源有新文章、网页变了……），Bot 在后台处理一次。
// 和定时任务存在同一张表里（category 区分），来源和每种来源要填的参数从 /api/event-sources 拿

let cache: Promise<EventSource[]> | null = null
export function useSources() {
  const [list, setList] = useState<EventSource[] | null>(null)
  useEffect(() => {
    cache = cache || api<EventSource[]>('/api/event-sources').catch(e => { cache = null; throw e })
    cache.then(setList).catch(() => setList([]))
  }, [])
  return list
}

export const hookUrl = (r: Routine) => r.event?.hook ? location.origin + r.event.hook : ''

function Copy({ text }: { text: string }) {
  const [ok, setOk] = useState(false)
  return <button className="btn" onClick={async () => { await navigator.clipboard.writeText(text); setOk(true); setTimeout(() => setOk(false), 1500) }}>
    {ok ? t('已复制') : t('复制')}</button>
}

export function EventsPanel({ bot }: { bot: Bot }) {
  const sources = useSources()
  const [list, setList] = useState<Routine[] | null>(null)
  const [edit, setEdit] = useState<Partial<Routine> | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const [msg, setMsg] = useState<{ id: number; text: string } | null>(null)
  const [err, setErr] = useState('')
  const load = () => api<Routine[]>(`/api/bots/${bot.id}/routines`).then(rs => setList(rs.filter(r => r.category === 'event')))
  useEffect(() => {
    load()
    const h = (e: Event) => { const id = (e as CustomEvent).detail; if (id == null || id === bot.id) load() }
    window.addEventListener('osa-routines', h)
    return () => window.removeEventListener('osa-routines', h)
  }, [bot.id])
  const act = async (r: Routine, action: string) => {
    setErr(''); setMsg(null)
    try {
      const n: Routine = await api(`/api/routines/${r.id}/${action}`, { method: 'POST' })
      if (n.note) setMsg({ id: r.id, text: n.note })
      load()
    } catch (e: any) { setErr(e.message) }
  }
  const src = (r: Routine) => sources?.find(s => s.id === (r.trigger as EventTrigger).source)
  if (edit) return <EventEditor bot={bot} routine={edit} sources={sources} onDone={() => { setEdit(null); load() }} />
  return (
    <div>
      <div className="section-head"><h4>{t('事件')}</h4><button className="btn" onClick={() => setEdit({})}>＋ {t('新建')}</button></div>
      <p className="muted small">{t('某件事发生时（收到 webhook、订阅源有新文章、网页变了、收到邮件、价格到了……），{name} 会在后台处理一次，结果发到对话里。也可以直接在对话里说“这个网页有变化就告诉我”。', { name: bot.name })}</p>
      {err && <div className="error-line">{err}</div>}
      {!list ? <div className="muted">{t('加载中…')}</div> : list.length ? list.map(r => {
        const s = src(r), ev = r.event
        return (
          <div key={r.id} className={'routine' + (r.enabled ? '' : ' off')}>
            <div className="routine-head" onClick={() => setOpen(open === r.id ? null : r.id)}>
              <b><span className="ev-icon">{s?.icon || '⚡'}</span>{r.name}</b>
              <span className="muted small">{!r.enabled ? t('已暂停') : r.running ? t('运行中') : ''}</span>
            </div>
            <div className="muted small">{describe(r.trigger, r.describe)}{r.skill && <> · {t('技能 /{name}', { name: r.skill })}</>}</div>
            {ev?.missing && <div className="small warn">{t('这种事件来源现在没有了，暂时不会触发。')}</div>}
            {ev?.mode === 'push' && hookUrl(r) && <div className="env-row ev-url"><input readOnly value={hookUrl(r)} onFocus={e => e.target.select()} /><Copy text={hookUrl(r)} /></div>}
            <div className="muted small">
              {ev?.mode === 'poll' && (ev.checked ? t('上次看 {when}', { when: when(ev.checked) }) : t('还没看过'))}
              {ev?.mode === 'poll' && r.enabled && r.next_run ? ' · ' + t('下次 {when}', { when: when(r.next_run) }) : ''}
              {ev?.pending ? ' · ' + t('排队 {n} 条', { n: ev.pending }) : ''}
              {r.last_run && <>{ev?.mode === 'poll' ? ' · ' : ''}{t('上次 {when} {status}', { when: when(r.last_run.ts), status: STATUS[r.last_run.status] || r.last_run.status })}</>}
            </div>
            {ev?.error && <div className="small run-failed">{t('出错：{error}', { error: ev.error })}</div>}
            {msg?.id === r.id && <div className="small muted">{msg.text}</div>}
            <div className="app-actions">
              <button className="btn" onClick={() => act(r, 'test')} disabled={r.running}
                title={ev?.mode === 'poll' ? t('先去看一次；没有新的，就拿最近看到的一条跑一遍') : t('拿一条示例事件跑一遍')}>{t('测试')}</button>
              {ev?.mode === 'poll' && <button className="btn" onClick={() => act(r, 'check')}>{t('现在看一次')}</button>}
              <button className="btn" onClick={() => act(r, r.enabled ? 'pause' : 'resume')}>{r.enabled ? t('暂停') : t('恢复')}</button>
              <button className="btn" onClick={() => setEdit(r)}>{t('编辑')}</button>
              <button className="btn" onClick={() => setOpen(open === r.id ? null : r.id)}>{t('记录')}</button>
            </div>
            {open === r.id && <Runs id={r.id} />}
          </div>
        )
      }) : <div className="muted small">{t('还没有事件')}</div>}
    </div>
  )
}

// ---------- 选来源 ----------
type Pick = { source: EventSource; scenario?: Scenario }

function Picker({ sources, onPick, onCancel }: { sources: EventSource[]; onPick: (p: Pick) => void; onCancel: () => void }) {
  const [cat, setCat] = useState('')
  const [q, setQ] = useState('')
  const cats = useMemo(() => [...new Set(sources.map(s => s.category))], [sources])
  const cards = useMemo(() => {
    const out: Pick[] = []
    for (const s of sources) {
      if (cat && s.category !== cat) continue
      for (const sc of s.scenarios) out.push({ source: s, scenario: sc })
      out.push({ source: s })   // 不用现成的用法，自己填
    }
    const k = q.trim().toLowerCase()
    return k ? out.filter(p => [p.source.title, p.source.description, p.scenario?.title, p.scenario?.description]
      .some(x => x?.toLowerCase().includes(k))) : out
  }, [sources, cat, q])
  return (
    <div>
      <h4>{t('新建事件')}</h4>
      <p className="muted small">{t('什么事发生时让 Bot 处理？选一个常见用法（之后都能改），或者自己设置。')}</p>
      <input className="ev-search" value={q} placeholder={t('搜索，比如 GitHub、邮件、降价、下雨')} onChange={e => setQ(e.target.value)} autoFocus />
      <div className="ev-cats">
        <button className={cat ? '' : 'on'} onClick={() => setCat('')}>{t('全部')}</button>
        {cats.map(c => <button key={c} className={cat === c ? 'on' : ''} onClick={() => setCat(c)}>{c}</button>)}
      </div>
      <div className="ev-grid">
        {cards.map(p => {
          const off = p.source.unavailable
          return (
            <button key={p.source.id + (p.scenario?.title || '')} className={'ev-card' + (p.scenario ? '' : ' blank')} disabled={!!off}
              title={off || undefined} onClick={() => onPick(p)}>
              <span className="ev-card-icon">{p.source.icon || '⚡'}</span>
              <b>{p.scenario?.title || t('自己设置：{title}', { title: p.source.title })}</b>
              <span className="muted small">{off || p.scenario?.description || p.source.description}</span>
              {p.scenario && <span className="tag">{p.source.title}</span>}
            </button>
          )
        })}
        {!cards.length && <div className="muted small">{t('没找到。可以选“收到 Webhook”接任何能发 webhook 的服务。')}</div>}
      </div>
      <div className="row-actions"><button className="btn" onClick={onCancel}>{t('返回')}</button></div>
    </div>
  )
}

// ---------- 参数表单 ----------
function FieldInput({ f, value, onChange, saved }: { f: SourceField; value: any; onChange: (v: any) => void; saved: boolean }) {
  const label = f.label + (f.required || /可选|optional/i.test(f.label) ? '' : t('（可选）'))   // i18n-ignore：认出标签里已经写了“可选”
  if (f.type === 'bool') return <label className="check"><input type="checkbox" checked={!!value} onChange={e => onChange(e.target.checked)} /> {f.label}
    {f.help && <span className="muted small">{f.help}</span>}</label>
  let input
  if (f.type === 'select') input = <select value={value ?? ''} onChange={e => onChange(e.target.value)}>
    {!f.required && !f.default && <option value="">—</option>}
    {f.options!.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
  else if (f.type === 'multi') {
    const v: string[] = value || []
    input = <div className="ev-multi">{f.options!.map(o => <label key={o.value} className="check">
      <input type="checkbox" checked={v.includes(o.value)} onChange={e => onChange(e.target.checked ? [...v, o.value] : v.filter(x => x !== o.value))} /> {o.label}</label>)}</div>
  }
  else if (f.type === 'textarea' || f.type === 'list') input = <textarea rows={f.type === 'list' ? 3 : 4} value={f.type === 'list' ? (value || []).join('\n') : value ?? ''}
    placeholder={f.placeholder || (f.type === 'list' ? t('一行一个') : '')} onChange={e => onChange(f.type === 'list' ? e.target.value.split('\n') : e.target.value)} />
  else if (f.type === 'secret') input = <input type="password" autoComplete="new-password" value={value ?? ''}
    placeholder={saved ? t('已存进保险库，留空不改') : f.placeholder || ''} onChange={e => onChange(e.target.value)} />
  else input = <input type={f.type === 'number' ? 'number' : f.type === 'url' ? 'url' : 'text'} min={f.min} max={f.max} value={value ?? ''}
    placeholder={f.placeholder || ''} onChange={e => onChange(e.target.value)} />
  return <label className="field">{label}{input}{f.help && <span className="muted small">{f.help}</span>}</label>
}

function initialParams(src: EventSource, given: Record<string, any> = {}) {
  const out: Record<string, any> = {}
  for (const f of src.fields) {
    const v = given[f.key] ?? f.default
    if (v !== undefined && v !== null) out[f.key] = v
  }
  return out
}

function EventEditor({ bot, routine, sources, onDone }: { bot: Bot; routine: Partial<Routine>; sources: EventSource[] | null; onDone: () => void }) {
  const skills = useStore(s => s.skills[bot.id])
  useEffect(() => { if (!skills) loadSkills(bot.id) }, [bot.id])
  const tr = routine.trigger as EventTrigger | undefined
  const [src, setSrc] = useState<EventSource | null>(null)
  const [name, setName] = useState(routine.name || '')
  const [prompt, setPrompt] = useState(routine.prompt || '')
  const [skill, setSkill] = useState(routine.skill || '')
  const [params, setParams] = useState<Record<string, any>>(tr?.params || {})
  const [every, setEvery] = useState(tr?.every ? String(tr.every) : '')
  const [include, setInclude] = useState((tr?.include || []).join(', '))
  const [exclude, setExclude] = useState((tr?.exclude || []).join(', '))
  const [filter, setFilter] = useState(tr?.llm_filter || '')
  const [showFilter, setShowFilter] = useState(!!(tr && (tr.include.length || tr.exclude.length || tr.llm_filter)))
  const [cur, setCur] = useState<Partial<Routine>>(routine)   // 新建的 webhook 类保存后留在这里，换成存好的那个
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // 编辑已有的：来源定了，直接进表单
  useEffect(() => { if (tr && sources && !src) setSrc(sources.find(s => s.id === tr.source) || null) }, [sources])
  if (!sources) return <div className="muted">{t('加载中…')}</div>
  if (tr && !src) return <div>
    <p className="error-line">{t('这个事件的来源（{source}）现在没有了，改不了，可以删掉。', { source: tr.source })}</p>
    <div className="row-actions"><button className="btn" onClick={onDone}>{t('返回')}</button><DeleteBtn r={cur as Routine} onDone={onDone} /></div></div>
  if (!src) return <Picker sources={sources} onCancel={onDone} onPick={({ source, scenario }) => {
    setSrc(source)
    setParams(initialParams(source, scenario?.params))
    setEvery(String(scenario?.every || source.every))
    if (scenario) {
      setName(scenario.name); setPrompt(scenario.prompt); setInclude(scenario.include.join(', '))
      setExclude(scenario.exclude.join(', ')); setFilter(scenario.llm_filter)
      setShowFilter(!!(scenario.include.length || scenario.exclude.length || scenario.llm_filter))
    }
  }} />
  const split = (s: string) => s.split(/[,，\n]+/).map(x => x.trim()).filter(Boolean)
  const save = async () => {
    setErr(''); setBusy(true)
    const clean = Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Array.isArray(v) ? v.map(x => typeof x === 'string' ? x.trim() : x).filter(x => x !== '') : v]))
    const trigger = { kind: 'event', source: src.id, params: clean, include: split(include), exclude: split(exclude), llm_filter: filter,
      ...(src.mode === 'poll' && every ? { every: Number(every) } : {}) }
    try {
      const data = { name, prompt, skill, trigger }
      const r: Routine = cur.id ? await api(`/api/routines/${cur.id}`, { method: 'PUT', body: data })
        : await api(`/api/bots/${bot.id}/routines`, { method: 'POST', body: data })
      // 新建的 webhook 类：留在这里给你复制地址；别的回列表
      if (!cur.id && src.mode === 'push') { setCur(r); setBusy(false); return }
      onDone()
    } catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  const rekey = async () => {
    if (!(await ask(t('换一个新地址？旧地址马上失效，要在对方那里改成新地址。'), { ok: t('换地址') }))) return
    try { setCur(await api(`/api/routines/${cur.id}/rekey`, { method: 'POST' })) } catch (e: any) { setErr(e.message) }
  }
  const url = cur.id ? hookUrl(cur as Routine) : ''
  return (
    <div className="ev-form">
      <h4>{cur.id ? t('编辑事件') : t('新建事件')}</h4>
      <div className="ev-src">
        <span className="ev-card-icon">{src.icon || '⚡'}</span>
        <div><b>{src.title}</b><div className="muted small">{src.description}</div></div>
        {!cur.id && <button className="link-btn" onClick={() => { setSrc(null); setErr('') }}>{t('换一个')}</button>}
      </div>
      {src.mode === 'push' && <div className="ev-setup">
        {url ? <>
          <b className="small">{t('地址')}</b>
          <div className="env-row"><input readOnly value={url} onFocus={e => e.target.select()} /><Copy text={url} /></div>
          {src.setup && <p className="small">{src.setup.replace('{url}', t('上面的地址'))}</p>}
          <p className="muted small"><button className="link-btn" onClick={rekey}>{t('换一个地址')}</button>{t('（地址泄露了的时候）')}</p>
          {(cur.trigger as EventTrigger)?.path && <p className="muted small">{t('旧地址 /hooks/{name} 也还能用（要带 X-OSA-Token）。换地址后旧的就不认了。', { name: (cur.trigger as EventTrigger).path! })}</p>}
        </> : <p className="small">{t('保存后会生成一个专用地址，填到对方的 webhook 设置里。')}{src.setup && ' ' + src.setup.replace('{url}', t('这个地址'))}</p>}
      </div>}
      <label className="field">{t('名字')}<input value={name} maxLength={40} placeholder={src.title} onChange={e => setName(e.target.value)} /></label>
      {src.fields.map(f => <FieldInput key={f.key} f={f} value={params[f.key]} saved={!!tr?.params?.[f.key]}
        onChange={v => setParams({ ...params, [f.key]: v })} />)}
      {src.mode === 'poll' && <label className="field">{t('多久看一次（分钟，至少 {n}）', { n: src.min_every })}
        <input type="number" min={src.min_every} value={every} placeholder={String(src.every)} onChange={e => setEvery(e.target.value)} /></label>}
      <label className="field">{t('事件来了做什么')}<textarea rows={4} value={prompt} placeholder={t('例如：总结成三条要点，和 AI 无关的写 [无新内容]')} onChange={e => setPrompt(e.target.value)} /></label>
      <label className="field">{t('按技能做（可选）')}
        <select value={skill} onChange={e => setSkill(e.target.value)}>
          <option value="">{t('不用技能')}</option>
          {(skills || []).map(k => <option key={k.id} value={k.name}>/{k.name}</option>)}
        </select>
      </label>
      <button className="ghost-btn" onClick={() => setShowFilter(!showFilter)}>{t('过滤（可选）')}{showFilter ? '▾' : '▸'}</button>
      {showFilter && <>
        <p className="muted small">{t('先按关键词逐条筛，剩下的再让模型判断要不要处理。被筛掉的不会派任务，记录里看得到。')}</p>
        <label className="field">{t('至少包含其中一个词')}<input value={include} placeholder="issue, bug" onChange={e => setInclude(e.target.value)} /></label>
        <label className="field">{t('包含就忽略')}<input value={exclude} placeholder="dependabot" onChange={e => setExclude(e.target.value)} /></label>
        <label className="field">{t('让模型判断要不要处理')}<textarea rows={2} value={filter} placeholder={t('只有报告 bug 的才处理')} onChange={e => setFilter(e.target.value)} /></label>
      </>}
      {src.mode === 'poll' && !cur.id && <p className="muted small">{t('保存后先看一次、记下现状，之后有新的才处理。')}</p>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" disabled={busy} onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={onDone}>{cur.id && !routine.id ? t('完成') : t('返回')}</button>
        {cur.id && <DeleteBtn r={cur as Routine} onDone={onDone} />}
      </div>
    </div>
  )
}

function DeleteBtn({ r, onDone }: { r: Routine; onDone: () => void }) {
  const del = async () => {
    if (!(await ask(t('删除事件「{name}」？运行记录也会删除。', { name: r.name || '' }), { ok: t('删除'), danger: true }))) return
    await api(`/api/routines/${r.id}`, { method: 'DELETE' }); onDone()
  }
  return <button className="btn danger" onClick={del}>{t('删除')}</button>
}
