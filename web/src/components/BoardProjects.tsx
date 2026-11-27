// 项目管理：列表、新建、修改、删除（卡片留下，变成不属于项目）
import { useEffect, useRef, useState } from 'react'
import { t, tc } from '../i18n'
import { addProjectMemory, createProject, deleteProject, fetchProjectMemories, fetchProjects, KEY_RE, patchProject } from '../board'
import type { Project, ProjectInput } from '../board'
import { ask } from './Dialog'
import { useBack } from '../swipe'
import { Icon } from '../icons'
import { MemoryItem } from './Memories'
import type { Memory } from '../types'
import '../memories.css'

export function Projects({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  const [list, setList] = useState<Project[] | null>(null)
  const [err, setErr] = useState('')
  const [edit, setEdit] = useState<Project | 'new' | null>(null)
  const [mem, setMem] = useState<number | null>(null)   // 展开了哪个项目的记忆
  const load = async () => { try { setList(await fetchProjects()); setErr('') } catch (e: any) { setErr(e.message) } }
  useEffect(() => { load() }, [])
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') edit ? setEdit(null) : onClose() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [edit])
  const remove = async (p: Project) => {
    const text = p.count
      ? t('删除项目“{name}”？它下面的 {n} 张卡会留下，变成不属于项目；项目记忆一起删掉。', { name: p.name, n: p.count })
      : t('删除项目“{name}”？项目记忆一起删掉。', { name: p.name })
    if (!await ask(text, { ok: t('删除'), danger: true })) return
    try { await deleteProject(p.id); await load(); onChanged() } catch (e: any) { setErr(e.message) }
  }
  const saved = async () => { setEdit(null); await load(); onChanged() }

  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal medium bd-projects-modal" role="dialog" aria-modal="true" onMouseDown={e => e.stopPropagation()}>
        <div className="bd-need-top">
          <h3>{edit === 'new' ? t('新建项目') : edit ? t('修改项目') : tc('看板', '项目')}</h3>
          <button className="icon-btn bd-close" onClick={onClose} title={t('关闭')}><Icon name="x" size={18} /></button>
        </div>
        {edit ? <ProjectForm p={edit === 'new' ? null : edit} onCancel={() => setEdit(null)} onSaved={saved} /> : <>
          <p className="muted small">{t('卡片归到项目下，按项目编号（比如 OPC-12）。项目的简介和目录会交代给做这些卡的任务。')}</p>
          {err && <div className="error-line">{err}</div>}
          {!list ? <div className="muted">{t('加载中…')}</div> : list.length === 0
            ? <div className="lib-empty"><p className="muted">{t('还没有项目。')}</p></div>
            : <div className="bd-proj-list">{list.map(p => (
              <div key={p.id} className="bd-proj">
                <span className="bd-key">{p.key}</span>
                <div className="bd-proj-main">
                  <div className="bd-proj-name">{p.name}<span className="muted small">{t('{n} 张卡', { n: p.count })}</span></div>
                  {p.brief && <div className="muted small bd-proj-brief">{p.brief}</div>}
                  {(p.dir || p.repo) && <div className="bd-proj-where">
                    {p.dir && <code>{p.dir}</code>}
                    {p.repo && <a className="link-btn" href={repoUrl(p.repo)} target="_blank" rel="noreferrer">{p.repo}</a>}
                  </div>}
                </div>
                <div className="bd-proj-actions">
                  <button className={'btn tiny' + (mem === p.id ? ' on' : '')} aria-expanded={mem === p.id} onClick={() => setMem(mem === p.id ? null : p.id)}>
                    {t('记忆')} <Icon name="chevron" size={12} className={mem === p.id ? '' : 'bd-rot'} /></button>
                  <button className="btn tiny" onClick={() => setEdit(p)}>{t('修改')}</button>
                  <button className="btn tiny danger" onClick={() => remove(p)}>{t('删除')}</button>
                </div>
                {mem === p.id && <ProjectMemories p={p} />}
              </div>
            ))}</div>}
          <div className="modal-actions">
            <button className="btn primary" onClick={() => setEdit('new')}><Icon name="plus" size={14} /> {t('新建项目')}</button>
          </div>
        </>}
      </div>
    </div>
  )
}

const repoUrl = (r: string) => /^https?:\/\//.test(r) ? r : 'https://github.com/' + r.replace(/^github\.com\//, '')

function ProjectForm({ p, onCancel, onSaved }: { p: Project | null; onCancel: () => void; onSaved: () => void }) {
  const [key, setKey] = useState(p?.key || '')
  const [name, setName] = useState(p?.name || '')
  const [brief, setBrief] = useState(p?.brief || '')
  const [dir, setDir] = useState(p?.dir || '')
  const [repo, setRepo] = useState(p?.repo || '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const keyOk = KEY_RE.test(key)
  const save = async () => {
    const body: ProjectInput = { key, name: name.trim(), brief: brief.trim(), dir: dir.trim(), repo: repo.trim() }
    setBusy(true)
    try {
      if (p) { const { key: _, ...rest } = body; await patchProject(p.id, rest) }
      else await createProject(body)
      onSaved()
    } catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return <>
    <div className="bd-new-row">
      <label className="field">{t('编号前缀')}
        <input value={key} maxLength={8} disabled={!!p} placeholder="OPC" autoFocus={!p}
          onChange={e => setKey(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''))} />
        <span className="muted small">{p ? t('建好以后不能改。') : t('2–8 个大写字母或数字，卡片编号是 {key}-1、{key}-2……', { key: key || 'OPC' })}</span>
      </label>
      <label className="field">{t('名字')}<input value={name} maxLength={60} autoFocus={!!p} placeholder={t('例如：官网改版')} onChange={e => setName(e.target.value)} /></label>
    </div>
    <label className="field">{t('简介（可选）')}<textarea rows={3} value={brief} placeholder={t('这个项目是做什么的、要注意什么')} onChange={e => setBrief(e.target.value)} /></label>
    <label className="field">{t('目录（可选）')}<input value={dir} placeholder="~/work/site" onChange={e => setDir(e.target.value)} /></label>
    <label className="field">{t('GitHub 仓库（可选）')}<input value={repo} placeholder="owner/repo" onChange={e => setRepo(e.target.value)} /></label>
    {err && <div className="error-line">{err}</div>}
    <div className="modal-actions">
      <button className="btn" onClick={onCancel}>{t('返回')}</button>
      <button className="btn primary" disabled={busy || !keyOk || !name.trim()} onClick={save}>{p ? t('保存') : t('创建')}</button>
    </div>
  </>
}

// 项目记忆：只有这个项目的任务读到（Bot 在对话里看到进行中的卡所属项目的记忆）。样子和改法照记忆面板
function ProjectMemories({ p }: { p: Project }) {
  const [list, setList] = useState<Memory[] | null>(null)
  const [text, setText] = useState('')
  const [until, setUntil] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // 项目记忆都是“记下的事”：旧数据没有 kind 的按 note 算，才能设截止日、置顶
  const load = () => fetchProjectMemories(p.id).then(r => setList(r.map(m => ({ ...m, kind: m.kind || 'note' })))).catch((e: any) => setErr(e.message))
  useEffect(() => {
    load()
    window.addEventListener('osa-memories', load)
    return () => window.removeEventListener('osa-memories', load)
  }, [p.id])
  const add = async () => {
    if (!text.trim()) return
    setBusy(true); setErr('')
    try { await addProjectMemory(p.id, { text: text.trim(), ...(until ? { until } : {}) }); setText(''); setUntil(''); await load() }
    catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  const pinned = (list || []).filter(m => m.pinned), rest = (list || []).filter(m => !m.pinned)
  return (
    <div className="bd-proj-mem">
      <p className="muted small">{t('只有做这个项目的卡的任务读到这些；Bot 在对话里也看得到进行中的卡所属项目的记忆。点一条可以修改。')}</p>
      {err && <div className="error-line">{err}</div>}
      {!list ? <div className="muted small">{t('加载中…')}</div>
        : !list.length ? <div className="muted small">{t('还没有。比如这个项目的约定、要用的账号、交付的格式。')}</div>
        : [...pinned, ...rest].map(m => <MemoryItem key={m.id} m={m} onChange={load} />)}
      <div className="bd-proj-mem-add">
        <textarea rows={2} value={text} maxLength={500} placeholder={t('加一条，比如：官网的文案都用第二人称')} onChange={e => setText(e.target.value)} />
        <div className="mem-until">
          <label>{t('截止日')} <input type="date" value={until} onChange={e => setUntil(e.target.value)} /></label>
          {!until && <span className="muted small">{t('不过期')}</span>}
          <span className="grow" />
          <button className="btn tiny primary" disabled={busy || !text.trim()} onClick={add}>{t('添加')}</button>
        </div>
      </div>
    </div>
  )
}
