import { useEffect, useRef, useState } from 'react'
import { api } from '../store'
import type { Avatar, Bot } from '../types'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'
import { t, tn } from '../i18n'

// Bot 模板：新建 Bot 时从模板装；Bot 详情里导出、分享；/template/<token> 是分享出去的公开页
export type TemplateCard = { id: string; name: string; description: string; avatar: Avatar; apps: string[]; skills: number; routines: number }
type App = { name: string; title: string; connected?: boolean; in_market: boolean }
type Routine = { name: string; prompt: string; skill: string; when: string }
export type TemplatePreview = {
  name: string; description: string; avatar: Avatar; instructions: string; apps: App[]
  skills: { name: string; description: string; body: string }[]; routines: Routine[]; text?: string
}

// compact：只列内置模板，不要“空白 Bot”和从链接、文件装（第一次打开的引导里用）
export function TemplatePicker({ onPreview, onBlank, compact }: { onPreview: (p: TemplatePreview) => void; onBlank?: () => void; compact?: boolean }) {
  const [list, setList] = useState<TemplateCard[] | null>(null)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const file = useRef<HTMLInputElement>(null)
  useEffect(() => { api('/api/templates').then(setList) }, [])
  const open = async (body: any, what: string) => {
    setBusy(what); setErr('')
    try { onPreview(await api('/api/templates/preview', { method: 'POST', body })) } catch (e: any) { setErr(e.message) }
    setBusy('')
  }
  const pick = async (f?: File) => {
    if (!f) return
    if (f.size > 400_000) return setErr(t('文件太大了，不像是一个 Bot 模板'))
    open({ text: await f.text() }, 'file')
  }
  return (
    <div className="tpl-pick">
      <div className="tpl-grid">
        {!compact && <button className="tpl-card blank" onClick={onBlank}>
          <div className="tpl-plus">＋</div><b>{t('空白 Bot')}</b><div className="muted small">{t('起个名字，在对话里交代它做什么')}</div>
        </button>}
        {(list || []).map(tp => (
          <button key={tp.id} className="tpl-card" disabled={!!busy} onClick={() => open({ id: tp.id }, tp.id)}>
            <BotAvatar avatar={tp.avatar} size={36} />
            <b>{tp.name}</b>
            <div className="muted small">{tp.description}</div>
            <div className="tpl-meta">{t('{n} 个技能', { n: tp.skills })}{tp.routines ? ' · ' + t('{n} 个定时任务', { n: tp.routines }) : ''}{busy === tp.id ? ' · ' + t('读取中…') : ''}</div>
          </button>
        ))}
      </div>
      {!compact && <label className="field">{t('从别人分享的模板装')}
        <div className="env-row">
          <input value={url} placeholder={t('粘贴 …/template/… 链接，或 GitHub 上的 .osabot.yaml')} onChange={e => setUrl(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && url.trim() && open({ url }, 'url')} />
          <button className="btn" disabled={!url.trim() || !!busy} onClick={() => open({ url }, 'url')}>{busy === 'url' ? t('读取中…') : t('读取')}</button>
          <button className="btn" disabled={!!busy} onClick={() => file.current?.click()}>{t('选择文件…')}</button>
          <input ref={file} type="file" accept=".yaml,.yml" hidden onChange={e => { pick(e.target.files?.[0]); e.target.value = '' }} />
        </div>
      </label>}
      {err && <div className="error-line">{err}</div>}
    </div>
  )
}

function Contents({ p, full = false }: { p: TemplatePreview; full?: boolean }) {
  const [showInstr, setShowInstr] = useState(false)
  const [skill, setSkill] = useState<string | null>(null)
  return <>
    {p.instructions && !full && <div className="tpl-sec">
      <button className="ghost-btn" onClick={() => setShowInstr(!showInstr)}>{t('人设和工作方式')} {showInstr ? '▾' : '▸'}</button>
      {showInstr && <pre className="tpl-pre">{p.instructions}</pre>}
    </div>}
    {p.apps.length > 0 && <div className="tpl-sec"><b className="small">{t('要用的应用')}</b>
      <div className="tpl-apps">{p.apps.map(a => <span key={a.name} className={'tag' + (a.connected === false ? ' warn' : '')}>
        {a.connected !== false ? a.title : a.in_market ? t('{title}（还没连接）', { title: a.title }) : t('{title}（应用市场里没有）', { title: a.title })}</span>)}</div>
      {p.apps.some(a => a.connected === false) && <div className="muted small">{t('装好以后在侧栏“应用”里连上，Bot 才能用它们。')}</div>}
    </div>}
    {p.skills.length > 0 && <div className="tpl-sec"><b className="small">{t('技能')}</b>
      {p.skills.map(s => <div key={s.name} className="tpl-item">
        <button className="ghost-btn" onClick={() => setSkill(skill === s.name ? null : s.name)}>/{s.name} {skill === s.name ? '▾' : '▸'}</button>
        <span className="muted small">{s.description}</span>
        {skill === s.name && <pre className="tpl-pre">{s.body}</pre>}
      </div>)}
    </div>}
    {p.routines.length > 0 && <div className="tpl-sec"><b className="small">{t('定时任务')}</b>
      {p.routines.map(r => <div key={r.name} className="tpl-item small">
        <b>{r.name}</b> · {r.when} · {r.skill ? t('用技能 /{skill}', { skill: r.skill }) : r.prompt.slice(0, 80)}
      </div>)}
    </div>}
  </>
}

export function TemplateInstall({ p, onBack, onInstalled }: { p: TemplatePreview; onBack: () => void; onInstalled: (b: Bot) => void }) {
  const [name, setName] = useState(p.name)
  const [enable, setEnable] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const install = async () => {
    setBusy(true); setErr('')
    try { onInstalled(await api('/api/templates/install', { method: 'POST', body: { text: p.text, name, enable_routines: enable } })) }
    catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return (
    <div className="tpl-install">
      <div className="tpl-head"><BotAvatar avatar={p.avatar} size={52} />
        <div><label className="field">{t('名字')}<input value={name} maxLength={40} onChange={e => setName(e.target.value)} /></label>
          {p.description && <div className="muted small">{p.description}</div>}</div>
      </div>
      <Contents p={p} />
      <p className="muted small">{t('模板里的人设、技能和定时任务都是给 Bot 的指令。别人分享的模板，装之前点开看一遍。装好以后都能在右栏里改。')}</p>
      {p.routines.length > 0 && <label className="check"><input type="checkbox" checked={enable} onChange={e => setEnable(e.target.checked)} /> {t('装好就开始定时运行（不勾的话先暂停，在右栏“定时”里开启）')}</label>}
      {err && <div className="error-line">{err}</div>}
      <div className="modal-actions">
        <button className="btn" onClick={onBack}>{t('返回')}</button>
        <button className="btn primary" disabled={busy || !name.trim()} onClick={install}>{busy ? t('安装中…') : t('装好这个 Bot')}</button>
      </div>
    </div>
  )
}

// Bot 详情里：导出成模板文件、生成分享链接
export function ShareTemplate({ bot }: { bot: Bot }) {
  const [token, setToken] = useState<string | null | undefined>(undefined)
  const [copied, setCopied] = useState(false)
  useEffect(() => { api(`/api/bots/${bot.id}/template/share`).then(r => setToken(r.token)) }, [bot.id])
  const toggle = async (on: boolean) => setToken((await api(`/api/bots/${bot.id}/template/share`, { method: 'POST', body: { on } })).token)
  const url = token ? `${location.origin}/template/${token}` : ''
  const copy = async () => { await navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  return (
    <div>
      <h4>{t('做成模板')}</h4>
      <p className="muted small">{t('把名字、头像、职责、技能、定时任务打包，别人装上就得到一个一样的 Bot。对话、记忆、资料库和应用的密钥不会带上。')}</p>
      <div className="row-actions">
        <a className="btn" href={`/api/bots/${bot.id}/template`} download>{t('导出模板文件')}</a>
        {token !== undefined && (url ? <button className="btn" onClick={() => toggle(false)}>{t('取消分享')}</button>
          : <button className="btn" onClick={() => toggle(true)}>{t('生成分享链接')}</button>)}
      </div>
      {url && <div className="env-row">
        <input readOnly value={url} onFocus={e => e.target.select()} />
        <button className="btn" onClick={copy}>{copied ? t('已复制') : t('复制')}</button>
      </div>}
      {url && <p className="muted small">{t('链接展示的是这个 Bot 现在的样子，之后改了职责或技能，打开链接看到的也会变。职责和技能里别写密码、token。')}</p>}
    </div>
  )
}

// /template/<token>：别人打开分享出来的模板
export function SharedTemplate({ token }: { token: string }) {
  const [p, setP] = useState<TemplatePreview | null>(null)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)
  useEffect(() => { api(`/api/shared/templates/${token}`).then(x => { setP(x); document.title = t('Bot 模板：{name}', { name: x.name }) }).catch((e: any) => setErr(e.message)) }, [token])
  if (err) return <div className="shared"><div className="shared-empty">{err}</div></div>
  if (!p) return <div className="loading">◆</div>
  const copy = async () => { await navigator.clipboard.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  return (
    <div className="shared">
      <header className="shared-head"><BotAvatar avatar={p.avatar} size={22} /><b>{t('Bot 模板：{name}', { name: p.name })}</b>
        <span className="muted small">open-super-agent</span></header>
      <div className="page-md">
        {p.description && <p className="skill-desc">{p.description}</p>}
        <div className="skill-howto">
          <b>{t('装到你自己的 open-super-agent')}</b>
          <p className="small">{tn('复制这个页面的链接，在侧栏 ＋ → {new} 里粘贴到“从别人分享的模板装”；或者下载文件后选择它。装之前会先给你看一遍。', { new: <b>{t('新建 Bot')}</b> })}</p>
          <div className="row-actions">
            <button className="btn primary" onClick={copy}>{copied ? t('已复制') : t('复制链接')}</button>
            <a className="btn" href={`/api/shared/templates/${token}/raw`} download>{t('下载文件')}</a>
          </div>
        </div>
        <Contents p={p} full />
        {p.instructions && <><h4>{t('人设和工作方式')}</h4><Markdown text={p.instructions} /></>}
      </div>
    </div>
  )
}
