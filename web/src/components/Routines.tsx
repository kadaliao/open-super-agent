import { useEffect, useRef, useState } from 'react'
import { api, loadSkills, openApps, setTeachDraft, showTask, useStore } from '../store'
import type { Avatar, Bot, MarketSkill, Routine, RoutineRun, Skill, SkillDraft, Trigger } from '../types'
import { lang, locale, t, tc, tn } from '../i18n'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'
import { ask } from './Dialog'
import { isEnter } from '../keys'

// 英文：Nov 6, 9:05 AM；中文保持 11/06 09:05
export const when = (ts: number | null) => ts ? new Date(ts * 1000).toLocaleString(locale, lang === 'en'
  ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
  : { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
// 周一到周日。英文是 Mon…Sun，选星期的小按钮里只放前两个字母（Mo、Tu…），中文不受影响
const DAYS = [tc('weekday', '一'), tc('weekday', '二'), tc('weekday', '三'), tc('weekday', '四'), tc('weekday', '五'), tc('weekday', '六'), tc('weekday', '日')]
const LIST = lang === 'en' ? ', ' : '、'   // 列举用的分隔符
export const STATUS: Record<string, string> = {
  running: t('运行中'), done: t('完成'), failed: t('失败'), cancelled: t('已取消'), skipped: t('跳过'), filtered: t('被过滤'),
  quiet: t('没有新内容'), ignored: t('不处理'),
}
export const SOURCE: Record<string, string> = { schedule: tc('source', '定时'), webhook: t('收到推送'), event: tc('source', '事件'), test: t('测试') }

// 触发方式的一句话。事件的说明（来源 + 盯着什么）由服务端写好，放在 text 里
export function describe(tr: Trigger, text?: string) {
  if (tr.kind === 'event') return text || t('事件')
  if (tr.kind === 'once') return t('{at} 一次', { at: tr.at })
  if (tr.kind === 'monthly') return t('每月 {days} 号 {times}', { days: tr.month_days.join(LIST), times: tr.times.join(LIST) })
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
  return ''
}

// ---------- 技能 ----------
export function SkillsPanel({ bot }: { bot: Bot }) {
  const skills = useStore(s => s.skills[bot.id])
  const draft = useStore(s => (s.teachDraft?.bot_id === bot.id ? s.teachDraft : null))
  const teach = useStore(s => s.teach)
  const [edit, setEdit] = useState<Editing | null>(null)
  const [teaching, setTeaching] = useState(false)
  const [importing, setImporting] = useState(false)
  const [market, setMarket] = useState(false)
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
      <div className="section-head skills-head"><h4>{t('技能')}</h4>
        <div className="app-actions">
          <button className="btn" disabled={!!teach} title={t('在电脑上做一遍，Bot 整理成技能')} onClick={() => setTeaching(!teaching)}>{t('演示一遍')}</button>
          <button className="btn" title={t('随 open-super-agent 发布的现成技能')} onClick={() => { setMarket(!market); setImporting(false) }}>{t('技能市场')}</button>
          <button className="btn" title={t('从文件或分享链接装一个技能')} onClick={() => { setImporting(!importing); setMarket(false) }}>{t('导入')}</button>
          <button className="btn" onClick={() => setEdit({})}>＋ {t('新建')}</button>
        </div>
      </div>
      {teaching && (
        <div className="teach-start">
          <label className="field">{t('要演示什么？（可选，帮 {name} 理解）', { name: bot.name })}
            <input value={note} maxLength={200} autoFocus placeholder={t('例如：在 GitHub 上给仓库提一个 issue')} onChange={e => setNote(e.target.value)}
              onKeyDown={e => isEnter(e) && startTeach()} /></label>
          <p className="muted small">{t('开始后电脑交给你操作：在浏览器里完整做一遍，做完点“完成，生成技能”。最长 10 分钟。')} {t('只记浏览器里的点击、输入和打开的网址，密码不会记录。录制期间后台任务会排队等电脑。')}</p>
          {err && <div className="error-line">{err}</div>}
          <div className="row-actions"><button className="btn primary" onClick={startTeach}>{t('开始录制')}</button><button className="btn" onClick={() => setTeaching(false)}>{t('取消')}</button></div>
        </div>
      )}
      {importing && <ImportSkill bot={bot} onDraft={d => { setImporting(false); setEdit({ name: d.name, description: d.description, body: d.body, source: 'import', draft: d }) }}
        onCancel={() => setImporting(false)} />}
      {market && <SkillMarket bot={bot} onClose={() => setMarket(false)} />}
      {teach && <div className="muted small">● {t('正在录制演示，在右栏“电脑”里操作，做完点“完成”。')}</div>}
      <p className="muted small">{t('在对话里输入 /名字 调用。也可以让 {name} “把刚才的做法存成技能”，或者点“演示一遍”做给它看。', { name: bot.name })}</p>
      {!skills ? <div className="muted">{t('加载中…')}</div> : skills.length ? skills.map(k => (
        <button key={k.id} className="list-item" onClick={() => setEdit({ ...k, shared: k.bot_id === null })}>
          <b>/{k.name}</b>{k.bot_id === null && <span className="tag">{t('共用')}</span>}{k.source === 'chat' && <span className="tag">{t('对话里保存')}</span>}
          {k.source === 'teach' && <span className="tag">{t('演示生成')}</span>}{k.source === 'import' && <span className="tag">{t('导入的')}</span>}
          {k.source === 'market' && <span className="tag">{t('技能市场')}</span>}
          {Object.keys(k.files || {}).length > 0 && <span className="tag">{t('{n} 个文件', { n: Object.keys(k.files).length })}</span>}
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
    if (/\.zip$/i.test(f.name)) {   // 带脚本、参考资料的技能包：原样传给服务器解开
      if (f.size > 2_000_000) return setErr(t('压缩包太大了，最多 {n} MB', { n: 2 }))
      return parse({ zip: toBase64(new Uint8Array(await f.arrayBuffer())), filename: f.name })
    }
    if (f.size > 200_000) return setErr(t('文件太大了，不像是一个技能'))
    parse({ text: await f.text(), filename: f.name })
  }
  return (
    <div className="teach-start">
      <label className="field">{t('技能链接')}
        <input value={url} autoFocus placeholder={t('别人分享的 …/skill/… 链接，或 GitHub 上的 SKILL.md、技能文件夹')} onChange={e => setUrl(e.target.value)}
          onKeyDown={e => isEnter(e) && url.trim() && parse({ url })} /></label>
      <p className="muted small">{t('技能是一个 Markdown 文件（开头写 name 和 description），和 Claude 等用的 SKILL.md 一样；带 scripts/、references/ 的技能包用 zip 或 GitHub 文件夹链接。导入后先给你看一遍，保存了才生效。')}</p>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" disabled={!url.trim() || busy} onClick={() => parse({ url })}>{busy ? t('读取中…') : t('读取链接')}</button>
        <button className="btn" disabled={busy} onClick={() => file.current?.click()}>{t('选择文件…')}</button>
        <button className="btn" onClick={onCancel}>{t('取消')}</button>
        <input ref={file} type="file" accept=".md,.markdown,.txt,.zip,text/markdown,text/plain,application/zip" hidden
          onChange={e => { pick(e.target.files?.[0]); e.target.value = '' }} />
      </div>
    </div>
  )
}

function toBase64(buf: Uint8Array) {
  let s = ''
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000))
  return btoa(s)
}

// 技能市场：随项目发布的技能（osa/catalog/skills），装上就是这个 Bot 的，之后照常能改
function SkillMarket({ bot, onClose }: { bot: Bot; onClose: () => void }) {
  const [list, setList] = useState<MarketSkill[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const load = () => api<MarketSkill[]>(`/api/bots/${bot.id}/skills/market`).then(setList).catch((e: any) => setErr(e.message))
  useEffect(() => { load() }, [bot.id])
  const install = async (m: MarketSkill) => {
    if (m.installed && !(await ask(t('已经有 /{name} 了，重新安装会替换掉它，包括你改过的地方。', { name: m.name }), { ok: t('替换') }))) return
    setBusy(m.id); setErr('')
    try { await api(`/api/bots/${bot.id}/skills/market/${m.id}`, { method: 'POST' }); await load(); loadSkills(bot.id) }
    catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  return (
    <div className="teach-start">
      <div className="section-head"><b>{t('技能市场')}</b><button className="ghost-btn" onClick={onClose}>{t('收起')}</button></div>
      <p className="muted small">{t('随 open-super-agent 发布、看过的技能。装上就是 {name} 的，可以接着改。', { name: bot.name })}</p>
      {!list ? <div className="muted small">{t('加载中…')}</div> : list.map(m => (
        <div key={m.id} className="tpl-item skill-market-item">
          <div className="section-head">
            <button className="ghost-btn" onClick={() => setOpen(open === m.id ? null : m.id)}><b>/{m.name}</b> {open === m.id ? '▾' : '▸'}</button>
            <button className="btn" disabled={!!busy} onClick={() => install(m)}>{busy === m.id ? t('安装中…') : m.installed ? t('重新安装') : t('安装')}</button>
          </div>
          <div className="muted small">{m.description}</div>
          {open === m.id && <pre className="tpl-pre">{m.body}</pre>}
        </div>
      ))}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

// 技能带的文件（scripts/、references/）：点开看内容；能删的给一个删除按钮
function SkillFiles({ files, onRemove }: { files: Record<string, string>; onRemove?: (path: string) => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const paths = Object.keys(files)
  if (!paths.length) return null
  return (
    <div className="tpl-sec"><b className="small">{t('带的文件（{n} 个）', { n: paths.length })}</b>
      {paths.map(p => <div key={p} className="tpl-item">
        <button className="ghost-btn" onClick={() => setOpen(open === p ? null : p)}><code>{p}</code> {open === p ? '▾' : '▸'}</button>
        <span className="muted small">{t('{n} 个字', { n: files[p].length })}</span>
        {onRemove && <button className="ghost-btn" title={t('不带这个文件')} onClick={() => onRemove(p)}>✕</button>}
        {open === p && <pre className="tpl-pre">{files[p]}</pre>}
      </div>)}
      <p className="muted small">{t('Bot 用 use_skill 取这些文件；脚本在电脑的工作文件夹里运行。')}</p>
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
  const [files, setFiles] = useState<Record<string, string>>(skill.files || skill.draft?.files || {})
  const [err, setErr] = useState('')
  const [showSteps, setShowSteps] = useState(false)
  const fromTeach = skill.source === 'teach' && !skill.id
  const draft = !skill.id ? skill.draft : undefined
  const save = async () => {
    try {
      // 导入的草稿把 frontmatter 里声明的应用一起存下；已有的技能不传 apps，保持原样
      const data = { name, description: desc, body, shared, bot_id: bot.id, source: fromTeach ? 'teach' : draft ? 'import' : undefined, files,
                     ...(draft ? { apps: draft.declared_apps } : {}) }
      if (skill.id) await api(`/api/skills/${skill.id}`, { method: 'PUT', body: data })
      else await api(`/api/bots/${bot.id}/skills`, { method: 'POST', body: data })
      onDone()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!(await ask(t('删除技能 /{name}？', { name: skill.name || '' }), { ok: t('删除'), danger: true }))) return
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
          {a.connected ? a.title : a.in_market ? t('{title}（还没连接）', { title: a.title }) : t('{title}（应用市场里没有）', { title: a.title })}</span>)}
          {draft.apps.some(a => !a.connected && a.in_market) && <> <button className="ghost-btn" onClick={openApps}>{t('去连接')} →</button></>}</p>}
        {draft.warning && <p className="small warn">{draft.warning}</p>}
        {draft.skipped.length > 0 && <p className="small">{t('这些不是文字文件，没有导入：')}{draft.skipped.map(p => <code key={p} className="tag">{p}</code>)}</p>}
        {Object.keys(draft.files).length > 0 && <p className="small">{t('带了脚本的技能会让 {name} 在电脑里运行它们，先点开下面的文件看一遍。', { name: bot.name })}</p>}
      </div>}
      {steps && steps.length > 0 && <>
        <button className="ghost-btn" onClick={() => setShowSteps(!showSteps)}>{t('录到的操作（{n} 步）', { n: steps.length })}{showSteps ? '▾' : '▸'}</button>
        {showSteps && <pre className="teach-steps">{steps.join('\n')}</pre>}
      </>}
      <label className="field">{t('名字（/ 后面的部分）')}<input value={name} maxLength={40} placeholder={t('例如：日报')} onChange={e => setName(e.target.value)} /></label>
      <label className="field">{t('一句话说明')}<input value={desc} maxLength={200} placeholder={t('什么时候用')} onChange={e => setDesc(e.target.value)} /></label>
      <label className="field">{t('做法')}<textarea rows={10} value={body} placeholder={t('步骤、要用的网站或应用、输出格式……')} onChange={e => setBody(e.target.value)} /></label>
      <SkillFiles files={files} onRemove={p => setFiles(Object.fromEntries(Object.entries(files).filter(([k]) => k !== p)))} />
      <label className="check"><input type="checkbox" checked={shared} onChange={e => setShared(e.target.checked)} /> {t('所有 Bot 都能用')}</label>
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={async () => (!(fromTeach || draft) || await ask(t('不保存这个草稿？'), { ok: t('不要了'), danger: true })) && onDone()}>{fromTeach || draft ? t('不要了') : t('返回')}</button>
        {skill.id && <button className="btn danger" onClick={del}>{t('删除')}</button>}
      </div>
      {skill.id && <ShareSkill skill={skill} onChange={s => setSkill({ ...skill, share_token: s.share_token })} />}
    </div>
  )
}

// /skill/<token>：别人打开分享出来的技能。只读，教他怎么装到自己的 open-super-agent
export function SharedSkill({ token }: { token: string }) {
  const [s, setS] = useState<{ name: string; description: string; body: string; apps: string[]; files: string[]; updated: number; bot?: { name: string; avatar: Avatar } } | null>(null)
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
          {s.files?.length > 0 && <p className="small">{t('带的文件：')}{s.files.map((f, i) => <span key={f}>{i > 0 && LIST}<code>{f}</code></span>)}</p>}
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
  const load = () => api<Routine[]>(`/api/bots/${bot.id}/routines`).then(rs => setList(rs.filter(r => r.category !== 'event')))
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
      <p className="muted small">{t('到点时，{name} 会在后台做一次，结果发到对话里。也可以直接在对话里说“每天 9 点帮我……”“明天下午 3 点提醒我……”。某件事发生时才做的，放在“事件”里。', { name: bot.name })}</p>
      {err && <div className="error-line">{err}</div>}
      {!list ? <div className="muted">{t('加载中…')}</div> : list.length ? list.map(r => (
        <div key={r.id} className={'routine' + (r.enabled ? '' : ' off')}>
          <div className="routine-head" onClick={() => setOpen(open === r.id ? null : r.id)}>
            <b>{r.name}</b>
            <span className="muted small">{describe(r.trigger)}{!r.enabled && ' · ' + t('已暂停')}{r.running && ' · ' + t('运行中')}</span>
          </div>
          <div className="muted small">
            {r.skill && <>{t('技能 /{name}', { name: r.skill })} · </>}
            {r.enabled && r.next_run ? t('下次 {when}', { when: when(r.next_run) }) : r.trigger.kind === 'once' && !r.next_run ? t('已经跑过') : ''}
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

export function Runs({ id }: { id: number }) {
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

// 明天这个时候（“某个时间点一次”默认值），格式和 <input type="datetime-local"> 一样
const tomorrow = () => {
  const d = new Date(Date.now() + 86400_000), p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T09:00`
}

type ScheduleKind = 'once' | 'daily' | 'monthly' | 'every'

function RoutineEditor({ bot, routine, onDone }: { bot: Bot; routine: Partial<Routine>; onDone: () => void }) {
  const skills = useStore(s => s.skills[bot.id])
  useEffect(() => { if (!skills) loadSkills(bot.id) }, [bot.id])
  const t0: any = routine.trigger || { kind: 'daily', times: ['09:00'], days: [] }
  const [name, setName] = useState(routine.name || '')
  const [prompt, setPrompt] = useState(routine.prompt || '')
  const [skill, setSkill] = useState(routine.skill || '')
  const [kind, setKind] = useState<ScheduleKind>(t0.kind)
  const [at, setAt] = useState(t0.at ? t0.at.replace(' ', 'T') : tomorrow())
  const [times, setTimes] = useState((t0.times || ['09:00']).join(', '))
  const [days, setDays] = useState<number[]>(t0.days || [])
  const [mdays, setMdays] = useState((t0.month_days || [1]).join(', '))
  const [minutes, setMinutes] = useState(String(t0.minutes || 60))
  const [err, setErr] = useState('')
  const split = (s: string) => s.split(/[,，\s]+/).filter(Boolean)
  const save = async () => {
    const trigger = kind === 'once' ? { kind, at: at.replace('T', ' ') }
      : kind === 'daily' ? { kind, times: split(times), days }
      : kind === 'monthly' ? { kind, times: split(times), month_days: split(mdays).map(Number) }
      : { kind, minutes: Number(minutes) }
    try {
      const data = { name, prompt, skill, trigger }
      if (routine.id) await api(`/api/routines/${routine.id}`, { method: 'PUT', body: data })
      else await api(`/api/bots/${bot.id}/routines`, { method: 'POST', body: data })
      onDone()
    } catch (e: any) { setErr(e.message) }
  }
  const del = async () => {
    if (!(await ask(t('删除定时任务「{name}」？运行记录也会删除。', { name: routine.name || '' }), { ok: t('删除'), danger: true }))) return
    await api(`/api/routines/${routine.id}`, { method: 'DELETE' }); onDone()
  }
  return (
    <div>
      <h4>{routine.id ? t('编辑定时任务') : t('新建定时任务')}</h4>
      <label className="field">{t('名字')}<input value={name} maxLength={40} placeholder={t('例如：早报')} onChange={e => setName(e.target.value)} /></label>
      <label className="field">{t('每次做什么')}<textarea rows={4} value={prompt} placeholder={t('例如：整理今天 AI 领域的 5 条新闻，附链接')} onChange={e => setPrompt(e.target.value)} /></label>
      <label className="field">{t('按技能做（可选）')}
        <select value={skill} onChange={e => setSkill(e.target.value)}>
          <option value="">{t('不用技能')}</option>
          {(skills || []).map(k => <option key={k.id} value={k.name}>/{k.name}</option>)}
        </select>
      </label>
      <label className="field">{t('什么时候')}
        <select value={kind} onChange={e => setKind(e.target.value as ScheduleKind)}>
          <option value="once">{t('某个时间点一次（提醒）')}</option>
          <option value="daily">{t('每天 / 每周固定时间')}</option>
          <option value="monthly">{t('每月几号')}</option>
          <option value="every">{t('每隔一段时间')}</option>
        </select>
      </label>
      {kind === 'once' && <label className="field">{t('时间')}<input type="datetime-local" value={at} onChange={e => setAt(e.target.value)} /></label>}
      {kind === 'monthly' && <>
        <label className="field">{t('每月几号（多个用逗号分开，这个月没有的在月底跑）')}<input value={mdays} placeholder="1, 15" onChange={e => setMdays(e.target.value)} /></label>
        <label className="field">{t('时间（多个用逗号分开）')}<input value={times} placeholder="09:00" onChange={e => setTimes(e.target.value)} /></label>
      </>}
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
      {err && <div className="error-line">{err}</div>}
      <div className="row-actions">
        <button className="btn primary" onClick={save}>{t('保存')}</button>
        <button className="btn" onClick={onDone}>{t('返回')}</button>
        {routine.id && <button className="btn danger" onClick={del}>{t('删除')}</button>}
      </div>
    </div>
  )
}
