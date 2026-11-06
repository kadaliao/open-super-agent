import { useEffect, useRef, useState } from 'react'
import { api, loadSkills, setTeachDraft, showTask, useStore } from '../store'
import type { Avatar, Bot, Routine, RoutineRun, Skill, SkillDraft, Trigger } from '../types'
import { lang, locale, t, tc, tn } from '../i18n'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'

// 英文：Nov 6, 9:05 AM；中文保持 11/06 09:05
const when = (ts: number | null) => ts ? new Date(ts * 1000).toLocaleString(locale, lang === 'en'
  ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
// 周一到周日。英文是 Mon…Sun，选星期的小按钮里只放前两个字母（Mo、Tu…），中文不受影响
const DAYS = [tc('weekday', '一'), tc('weekday', '二'), tc('weekday', '三'), tc('weekday', '四'), tc('weekday', '五'), tc('weekday', '六'), tc('weekday', '日')]
const LIST = lang === 'en' ? ', ' : '、'   // 列举用的分隔符
const STATUS: Record<string, string> = {
  running: t('运行中'), done: t('完成'), failed: t('失败'), cancelled: t('已取消'), skipped: t('跳过'), filtered: t('被过滤'),
}
const SOURCE: Record<string, string> = { schedule: tc('source', '定时'), webhook: 'webhook', test: t('测试') }

export function describe(tr: Trigger) {
  if (tr.kind === 'every') {
    const h = tr.minutes / 60
    return tr.minutes % 60 ? t('每 {n} 分钟', { n: tr.minutes }) : h === 1 ? t('每 1 小时') : t('每 {n} 小时', { n: h })
  }
  if (tr.kind === 'daily') {
    const times = tr.times.join(LIST), wd = tr.days.join()
    if (!tr.days.length || tr.days.length === 7) return t('每天 {times}', { times })
    // 工作日、周末：中文照旧逐个列出，英文说 Weekdays / Weekends
    if (wd === '1,2,3,4,5') return t('每周一、二、三、四、五 {times}', { times })
    if (wd === '6,7') return t('每周六、日 {times}', { times })
    return t('每周{days} {times}', { days: tr.days.map(x => DAYS[x - 1]).join(LIST), times })
  }
  return t('收到 webhook 时')
}

// ---------- 技能 ----------
export function SkillsPanel({ bot }: { bot: Bot }) {
  const skills = useStore(s => s.skills[bot.id])
  const draft = useStore(s => (s.teachDraft?.bot_id === bot.id ? s.teachDraft : null))
  const teach = useStore(s => s.teach)
  const [edit, setEdit] = useState<Editing | null>(null)
  const [teaching, setTeaching] = useState(false)
  const [importing, setImporting] = useState(false)
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { loadSkills(bot.id) }, [bot.id])
  // 演示录完生成了草稿：直接打开编辑器让你审核
  useEffect(() => { if (draft) setEdit({ ...draft.draft, source: 'teach' }) }, [draft])
  const startTeach = async () => {
    setErr('')
    try { await api(`/api/bots/${bot.id}/teach/start`, { method: 'POST', body: { note } }); setTeaching(false); setNote('') }
    catch (e: any) { setErr(e.message) }
  }
  if (edit) return <SkillEditor bot={bot} skill={edit} steps={edit.source === 'teach' ? draft?.steps : undefined}
    onDone={() => { setEdit(null); setTeachDraft(null); loadSkills(bot.id) }} />
  return (
    <div>
      <div className="section-head"><h4>{t('技能')}</h4>
        <div className="app-actions">
          <button className="btn" disabled={!!teach} title={t('在电脑上做一遍，Bot 整理成技能')} onClick={() => setTeaching(!teaching)}>{t('演示一遍')}</button>
          <button className="btn" title={t('从文件或分享链接装一个技能')} onClick={() => setImporting(!importing)}>{t('导入')}</button>
          <button className="btn" onClick={() => setEdit({})}>＋ {t('新建')}</button>
        </div>
      </div>
      {teaching && (
        <div className="teach-start">
          <label className="field">{t('要演示什么？（可选，帮 {name} 理解）', { name: bot.name })}
            <input value={note} maxLength={200} autoFocus placeholder={t('例如：在 GitHub 上给仓库提一个 issue')} onChange={e => setNote(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && startTeach()} /></label>
          <p className="muted small">{t('开始后电脑交给你操作：在浏览器里完整做一遍，做完点“完成，生成技能”。最长 10 分钟。')} {t('只记浏览器里的点击、输入和打开的网址，密码不会记录。录制期间后台任务会排队等电脑。')}</p>
          {err && <div className="error-line">{err}</div>}
          <div className="row-actions"><button className="btn primary" onClick={startTeach}>{t('开始录制')}</button><button className="btn" onClick={() => setTeaching(false)}>{t('取消')}</button></div>
        </div>
      )}
      {importing && <ImportSkill bot={bot} onDraft={d => { setImporting(false); setEdit({ ...d, source: 'import', draft: d }) }}
        onCancel={() => setImporting(false)} />}
      {teach && <div className="muted small">● {t('正在录制演示，在右栏“电脑”里操作，做完点“完成”。')}</div>}
      <p className="muted small">{t('在对话里输入 /名字 调用。也可以让 {name} “把刚才的做法存成技能”，或者点“演示一遍”做给它看。', { name: bot.name })}</p>
      {!skills ? <div className="muted">{t('加载中…')}</div> : skills.length ? skills.map(k => (
        <button key={k.id} className="list-item" onClick={() => setEdit({ ...k, shared: k.bot_id === null })}>
          <b>/{k.name}</b>{k.bot_id === null && <span className="tag">{t('共用')}</span>}{k.source === 'chat' && <span className="tag">{t('对话里保存')}</span>}
          {k.source === 'teach' && <span className="tag">{t('演示生成')}</span>}{k.source === 'import' && <span className="tag">{t('导入的')}</span>}
          {k.share_token && <span className="tag">{t('已分享')}</span>}
          <div className="muted small">{k.description || k.body.slice(0, 60)}</div>
        </button>
      )) : <div className="muted small">{t('还没有技能')}</div>}
    </div>
  )
}

type Editing = Partial<Skill> & { shared?: boolean; draft?: SkillDraft }

// 导入：贴一个链接（别人分享的、GitHub 上的）或者选一个 .md 文件，解析成草稿交给编辑器审核
function ImportSkill({ bot, onDraft, onCancel }: { bot: Bot; onDraft: (d: SkillDraft) => void; onCancel: () => void }) {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const file = useRef<HTMLInputElement>(null)
  const parse = async (body: any) => {
    setBusy(true); setErr('')
    try { onDraft(await api(`/api/bots/${bot.id}/skills/parse`, { method: 'POST', body })) } catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  const pick = async (f: File | undefined) => {
    if (!f) return
    if (f.size > 200_000) return setErr(t('文件太大了，不像是一个技能'))
    parse({ text: await f.text(), filename: f.name })
  }
  return (
    <div className="teach-start">
      <label className="field">{t('技能链接')}
        <input value={url} autoFocus placeholder={t('别人分享的 …/skill/… 链接，或 GitHub 上的 SKILL.md')} onChange={e => setUrl(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && url.trim() && parse({ url })} /></label>
      <p className="muted small">{t('技能是一个 Markdown 文件（开头写 name 和 description），和 Claude 等用的 SKILL.md 一样。导入后先给你看一遍，保存了才生效。')}</p>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" disabled={!url.trim() || busy} onClick={() => parse({ url })}>{busy ? t('读取中…') : t('读取链接')}</button>
        <button className="btn" disabled={busy} onClick={() => file.current?.click()}>{t('选择文件…')}</button>
        <button className="btn" onClick={onCancel}>{t('取消')}</button>
        <input ref={file} type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden
          onChange={e => { pick(e.target.files?.[0]); e.target.value = '' }} />
      </div>
    </div>
  )
}

function ShareSkill({ skill, onChange }: { skill: Partial<Skill>; onChange: (s: Skill) => void }) {
  const [copied, setCopied] = useState(false)
  const url = skill.share_token ? `${location.origin}/skill/${skill.share_token}` : ''
  const toggle = async (on: boolean) => onChange(await api(`/api/skills/${skill.id}/share`, { method: 'POST', body: { on } }))
  const copy = async () => { await navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  return (
    <div className="skill-share">
      <div className="row-actions">
        <a className="btn" href={`/api/skills/${skill.id}/export`} download>{t('导出文件')}</a>
        {url ? <button className="btn" onClick={() => toggle(false)}>{t('取消分享')}</button>
          : <button className="btn" onClick={() => toggle(true)}>{t('生成分享链接')}</button>}
      </div>
      {url && <div className="env-row">
        <input readOnly value={url} onFocus={e => e.target.select()} />
        <button className="btn" onClick={copy}>{copied ? t('已复制') : t('复制')}</button>
      </div>}
      {url && <p className="muted small">{t('拿到链接的人能看到这个技能的做法，可以导入到自己的 open-super-agent。做法里别写密码、token 这类东西。')}</p>}
    </div>
  )
}

function SkillEditor({ bot, skill: initial, steps, onDone }: { bot: Bot; skill: Editing; steps?: string[]; onDone: () => void }) {
  const [skill, setSkill] = useState(initial)
  const [name, setName] = useState(skill.name || '')
  const [desc, setDesc] = useState(skill.description || '')
  const [body, setBody] = useState(skill.body || '')
  const [shared, setShared] = useState(!!skill.shared)
  const [err, setErr] = useState('')
  const [showSteps, setShowSteps] = useState(false)
  const fromTeach = skill.source === 'teach' && !skill.id
  const draft = !skill.id ? skill.draft : undefined
  const save = async () => {
    try {
      const data = { name, description: desc, body, shared, bot_id: bot.id, source: fromTeach ? 'teach' : draft ? 'import' : undefined }
      if (skill.id) await api(`/api/skills/${skill.id}`, { method: 'PUT', body: data })
      else await api(`/api/bots/${bot.id}/skills`, { method: 'POST', body: data })
      onDone()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!confirm(t('删除技能 /{name}？', { name: skill.name || '' }))) return
    await api(`/api/skills/${skill.id}`, { method: 'DELETE' }); onDone()
  }
  return (
    <div>
      <h4>{skill.id ? t('编辑技能') : fromTeach ? t('审核演示生成的技能') : draft ? t('审核导入的技能') : t('新建技能')}</h4>
      {fromTeach && <p className="muted small">{t('{name} 根据你的演示写了下面的做法，还没保存。改好后保存，再在对话里输入 /名字 试一次。', { name: bot.name })}</p>}
      {draft && <div className="skill-import-note">
        <p className="small">{t('技能是交给 {name} 照着做的说明，还没保存。先看一遍做法：别装来路不明的，也留意有没有让它把资料发到别处。', { name: bot.name })}</p>
        {draft.exists && name.trim() === draft.name && <p className="small warn">{t('已经有 /{name} 了，保存会替换掉原来的。想留着原来的就改个名字。', { name })}</p>}
        {draft.apps.length > 0 && <p className="small">{t('用到的应用：')}{draft.apps.map(a => <span key={a.name} className={'tag' + (a.connected ? '' : ' warn')}>
          {a.title}{a.connected ? '' : t('（还没连接）')}</span>)}
          {draft.apps.some(a => !a.connected) && <> {t('没连接的应用可以在侧栏“应用”里连上。')}</>}</p>}
      </div>}
      {steps && steps.length > 0 && <>
        <button className="ghost-btn" onClick={() => setShowSteps(!showSteps)}>{t('录到的操作（{n} 步）', { n: steps.length })}{showSteps ? '▾' : '▸'}</button>
        {showSteps && <pre className="teach-steps">{steps.join('\n')}</pre>}
      </>}
      <label className="field">{t('名字（/ 后面的部分）')}<input value={name} maxLength={40} placeholder={t('例如：日报')} onChange={e => setName(e.target.value)} /></label>
      <label className="field">{t('一句话说明')}<input value={desc} maxLength={200} placeholder={t('什么时候用')} onChange={e => setDesc(e.target.value)} /></label>
      <label className="field">{t('做法')}<textarea rows={10} value={body} placeholder={t('步骤、要用的网站或应用、输出格式……')} onChange={e => setBody(e.target.value)} /></label>
      <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> {t('所有 Bot 都能用')}</label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={() => (!(fromTeach || draft) || confirm(t('不保存这个草稿？'))) && onDone()}>{fromTeach || draft ? t('不要了') : t('返回')}</button>
        {skill.id && <button className="btn danger" onClick={del}>{t('删除')}</button>}
      </div>
      {skill.id && <ShareSkill skill={skill} onChange={s => setSkill({ ...skill, share_token: s.share_token })} />}
    </div>
  )
}

// /skill/<token>：别人打开分享出来的技能。只读，教他怎么装到自己的 open-super-agent
export function SharedSkill({ token }: { token: string }) {
  const [s, setS] = useState<{ name: string; description: string; body: string; apps: string[]; updated: number; bot?: { name: string; avatar: Avatar } } | null>(null)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)
  useEffect(() => { api(`/api/shared/skills/${token}`).then(x => { setS(x); document.title = t('技能 /{name}', { name: x.name }) }).catch((e: any) => setErr(e.message)) }, [token])
  if (err) return <div className="shared"><div className="shared-empty">{err}</div></div>
  if (!s) return <div className="loading">◆</div>
  const copy = async () => { await navigator.clipboard.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  return (
    <div className="shared">
      <header className="shared-head">
        {s.bot && <BotAvatar avatar={s.bot.avatar} size={22} />}
        <b>{t('技能 /{name}', { name: s.name })}</b>
        <span className="muted small">{s.bot ? t('{name} 的技能', { name: s.bot.name }) + ' · ' : ''}{t('{when} 更新', { when: when(s.updated) })}</span>
      </header>
      <div className="page-md">
        {s.description && <p className="skill-desc">{s.description}</p>}
        <div className="skill-howto">
          <b>{t('装到你自己的 open-super-agent')}</b>
          <p className="small">{tn('复制这个页面的链接，在右栏“技能”里点{b}粘贴进去；或者下载文件后选择它。导入时会先给你看一遍。', { b: <b>{t('导入')}</b> })}</p>
          {s.apps.length > 0 && <p className="small">{t('要用到的应用：')}{s.apps.map((a, i) => <span key={a}>{i > 0 && LIST}<code>@{a}</code></span>)}</p>}
          <div className="row-actions">
            <button className="btn primary" onClick={copy}>{copied ? t('已复制') : t('复制链接')}</button>
            <a className="btn" href={`/api/shared/skills/${token}/raw`} download>{t('下载文件')}</a>
          </div>
        </div>
        <h4>{t('做法')}</h4>
        <Markdown text={s.body} />
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
      <div className="section-head"><h4>{t('定时任务')}</h4><button className="btn" onClick={() => setEdit({})}>＋ {t('新建')}</button></div>
      <p className="muted small">{t('到点或收到 webhook 时，{name} 会在后台做一次，结果发到对话里。也可以直接在对话里说“每天 9 点帮我……”。', { name: bot.name })}</p>
      {err && <div className="error-line">{err}</div>}
      {!list ? <div className="muted">{t('加载中…')}</div> : list.length ? list.map(r => (
        <div key={r.id} className={'routine' + (r.enabled ? '' : ' off')}>
          <div className="routine-head" onClick={() => setOpen(open === r.id ? null : r.id)}>
            <b>{r.name}</b>
            <span className="muted small">{describe(r.trigger)}{!r.enabled && ' · ' + t('已暂停')}{r.running && ' · ' + t('运行中')}</span>
          </div>
          <div className="muted small">
            {r.skill && <>{t('技能 /{name}', { name: r.skill })} · </>}
            {r.enabled && r.next_run ? t('下次 {when}', { when: when(r.next_run) }) : r.trigger.kind === 'webhook' ? `POST /hooks/${r.name}` : ''}
            {r.last_run && <> · {t('上次 {when} {status}', { when: when(r.last_run.ts), status: STATUS[r.last_run.status] || r.last_run.status })}</>}
          </div>
          <div className="app-actions">
            <button className="btn" onClick={() => act(r, 'test')} disabled={r.running}>{t('测试')}</button>
            <button className="btn" onClick={() => act(r, r.enabled ? 'pause' : 'resume')}>{r.enabled ? t('暂停') : t('恢复')}</button>
            <button className="btn" onClick={() => setEdit(r)}>{t('编辑')}</button>
            <button className="btn" onClick={() => setOpen(open === r.id ? null : r.id)}>{t('记录')}</button>
          </div>
          {open === r.id && <Runs id={r.id} />}
        </div>
      )) : <div className="muted small">{t('还没有定时任务')}</div>}
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
  if (!runs) return <div className="muted small">{t('加载中…')}</div>
  if (!runs.length) return <div className="muted small">{t('还没运行过')}</div>
  return (
    <div className="runs">
      {runs.map(x => (
        <div key={x.id} className="kv" title={x.detail}>
          <span>{when(x.ts)} · {SOURCE[x.source] || x.source}{x.task_id ? <> · <button className="link-btn" title={t('看这次运行每一步做了什么')} onClick={() => showTask(x.task_id!)}>#{x.task_id}</button></> : ''}{x.detail && ` · ${x.detail.slice(0, 40)}`}</span>
          <span className={'muted run-' + x.status}>{STATUS[x.status] || x.status}</span>
        </div>
      ))}
      <div className="muted small">{t('保留最近 20 次')}</div>
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
    if (!confirm(t('删除定时任务「{name}」？运行记录也会删除。', { name: routine.name || '' }))) return
    await api(`/api/routines/${routine.id}`, { method: 'DELETE' }); onDone()
  }
  return (
    <div>
      <h4>{routine.id ? t('编辑定时任务') : t('新建定时任务')}</h4>
      <label className="field">{t('名字')}<input value={name} maxLength={40} placeholder={kind === 'webhook' ? t('会出现在网址里，例如 github') : t('例如：早报')} onChange={e => setName(e.target.value)} /></label>
      <label className="field">{t('每次做什么')}<textarea rows={4} value={prompt} placeholder={t('例如：整理今天 AI 领域的 5 条新闻，附链接')} onChange={e => setPrompt(e.target.value)} /></label>
      <label className="field">{t('按技能做（可选）')}
        <select value={skill} onChange={e => setSkill(e.target.value)}>
          <option value="">{t('不用技能')}</option>
          {(skills || []).map(k => <option key={k.id} value={k.name}>/{k.name}</option>)}
        </select>
      </label>
      <label className="field">{t('什么时候')}
        <select value={kind} onChange={e => setKind(e.target.value as Trigger['kind'])}>
          <option value="daily">{t('每天 / 每周固定时间')}</option>
          <option value="every">{t('每隔一段时间')}</option>
          <option value="webhook">{t('收到 webhook 时')}</option>
        </select>
      </label>
      {kind === 'daily' && <>
        <label className="field">{t('时间（多个用逗号分开）')}<input value={times} placeholder="09:00, 18:00" onChange={e => setTimes(e.target.value)} /></label>
        <div className="day-row">
          {DAYS.map((d, i) => (
            <button key={d} className={days.includes(i + 1) ? 'on' : ''}
              onClick={() => setDays(days.includes(i + 1) ? days.filter(x => x !== i + 1) : [...days, i + 1].sort())}>{d.slice(0, 2)}</button>
          ))}
          <span className="muted small">{days.length ? '' : t('不选 = 每天')}</span>
        </div>
      </>}
      {kind === 'every' && <label className="field">{t('间隔（分钟，至少 5）')}<input type="number" min={5} value={minutes} onChange={e => setMinutes(e.target.value)} /></label>}
      {kind === 'webhook' && <>
        <p className="muted small">{t('地址：POST /hooks/{name}。设置了 OSA_WEBHOOK_TOKEN 时请求头 X-OSA-Token 要一致，否则只接受本机请求。', { name: name || tc('url', '名字') })}</p>
        <label className="field">{t('至少包含其中一个词（可选）')}<input value={include} placeholder="issue, bug" onChange={e => setInclude(e.target.value)} /></label>
        <label className="field">{t('包含就忽略（可选）')}<input value={exclude} placeholder="dependabot" onChange={e => setExclude(e.target.value)} /></label>
        <label className="field">{t('让模型判断要不要处理（可选）')}<textarea rows={2} value={filter} placeholder={t('只有报告 bug 的才处理')} onChange={e => setFilter(e.target.value)} /></label>
      </>}
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={onDone}>{t('返回')}</button>
        {routine.id && <button className="btn danger" onClick={del}>{t('删除')}</button>}
      </div>
    </div>
  )
}
