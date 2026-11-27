// 卡片页（描述、你的原话、在等你回答的问题、交付的结果、背后的任务）和新建卡片
// 卡片页是全局的一层：看板、对话里的卡片标记、任务详情都用 openNeed(id) 打开它，盖在当前页面上，关掉回到原来的地方
import { useEffect, useRef, useState } from 'react'
import { openBoard, openNeed, useStore } from '../store'
import { t } from '../i18n'
import { assignee, columnName, createNeed, fetchBoard, fetchNeed, fetchProjects, needAction, onBoard, openTask, patchNeed, refreshWaiting, running, waitReason } from '../board'
import type { Need, NeedDetail, NeedTask, Project } from '../board'
import type { Approval } from '../types'
import BotAvatar from './BotAvatar'
import Markdown from './Markdown'
import { Icon } from '../icons'
import { ask } from './Dialog'
import { useBack } from '../swipe'
import { dur } from './Observe'

const taskStatus = (x: NeedTask) => x.waiting_kind === 'question' ? t('等你回答') : x.waiting_kind === 'help' ? t('请你接手电脑')
  : x.waiting_kind ? t('等你确认') : ({
    running: t('进行中'), done: t('完成'), failed: t('失败'), cancelled: t('已取消'), interrupted: t('已中断'),
  } as Record<string, string>)[x.status] || x.status

// 卡片页改了一张卡：看板收到后先把它挪到新的列，不等 board 事件
export const needChanged = (n: Need) => window.dispatchEvent(new CustomEvent('osa-need', { detail: n }))

/** App 里挂一个：store 里 needOpen 有值就打开那张卡 */
export function NeedHost() {
  const id = useStore(s => s.needOpen)
  return id ? <NeedPage key={id} id={id} /> : null
}

function NeedPage({ id }: { id: number }) {
  const bots = useStore(s => s.bots)
  const view = useStore(s => s.view)
  const onClose = () => openNeed(null)
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  const [n, setN] = useState<NeedDetail | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [err, setErr] = useState('')
  const [title, setTitle] = useState<string | null>(null)   // 正在改的标题；null 是没在改
  const [body, setBody] = useState<string | null>(null)
  const [after, setAfter] = useState<number[] | null>(null) // 正在改的“在这之后开始”
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try { setN(await fetchNeed(id)); setErr('') } catch (e: any) { setErr(e.message) }
  }
  const latest = useRef(load)
  latest.current = load
  useEffect(() => { load(); fetchProjects().then(setProjects).catch(() => {}) }, [])
  useEffect(() => onBoard(nid => { if (nid === null || nid === id) latest.current() }), [id])
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape' && title === null && body === null && after === null) onClose() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [title, body, after])

  // 改完：接口返回的卡（没有 results、questions）盖在现有的上面，看板那边也挪过去
  const got = (x: Need) => { setN(cur => cur ? { ...cur, ...x } : cur); needChanged(x); refreshWaiting() }
  const run = async (f: () => Promise<Need>) => {
    setBusy(true)
    try { got(await f()); setErr('') } catch (e: any) { setErr(e.message) }
    setBusy(false)
  }
  const saveTitle = async () => {
    const v = (title || '').trim()
    if (!v || !n) return
    if (v !== n.title) await run(() => patchNeed(id, { title: v }))
    setTitle(null)
  }
  const saveBody = async () => {
    if (!n || body === null) return
    if (body !== n.body) await run(() => patchNeed(id, { body }))
    setBody(null)
  }
  const saveAfter = async () => {
    if (!n || after === null) return
    const same = after.length === n.after.length && after.every(x => n.after.includes(x))
    if (!same) await run(() => patchNeed(id, { after }))
    setAfter(null)
  }
  const stop = async (a: 'done' | 'cancel') => {
    if (!n) return
    if (running(n)) {
      const text = a === 'done' ? t('下面还有任务在做，标记完成会停掉它们。') : t('下面还有任务在做，不用做了会停掉它们。')
      if (!await ask(text, { ok: a === 'done' ? t('标记完成') : t('不用做了'), danger: true })) return
    }
    run(() => needAction(id, a))
  }
  // 去看任务：关掉卡片页，任务详情里有“回到卡片”
  const goTask = (taskId: number, botId?: number | null, chatId?: number | null) => { onClose(); openTask(taskId, botId, chatId, id) }

  if (!n) return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal medium bd-need" onMouseDown={e => e.stopPropagation()}>
        {err ? <div className="error-line">{err}</div> : <div className="muted">{t('加载中…')}</div>}
        <div className="modal-actions"><button className="btn" onClick={onClose}>{t('关闭')}</button></div>
      </div>
    </div>
  )

  const bot = assignee(bots, n.assignee_bot_id)
  const primary = bots.find(b => b.is_primary)
  const ended = n.column === 'done' || n.column === 'cancelled'
  const reason = n.column === 'waiting' ? waitReason(n) : ''
  const archived = !!n.archived
  const started = n.tasks.length > 0
  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal medium bd-need" role="dialog" aria-modal="true" onMouseDown={e => e.stopPropagation()}>
        <div className="bd-need-top">
          {n.key && <span className="bd-key">{n.key}</span>}
          <span className={`bd-col-chip ${n.column}`}>{columnName(n.column)}</span>
          {reason && <span className="bd-reason">{reason}</span>}
          {archived && <span className="chip faint">{t('已归档')}</span>}
          {view !== 'board' && <button className="link-btn small bd-to-board" onClick={() => openBoard()}>{t('在看板里看')}</button>}
          <button className="icon-btn bd-close" onClick={onClose} title={t('关闭')}><Icon name="x" size={18} /></button>
        </div>

        {title === null
          ? <h3 className="bd-need-title">{n.title}<button className="link-btn small" onClick={() => setTitle(n.title)}>{t('改标题')}</button></h3>
          : <div className="bd-edit">
              <input value={title} maxLength={200} autoFocus onChange={e => setTitle(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) saveTitle(); if (e.key === 'Escape') setTitle(null) }} />
              <button className="btn tiny primary" disabled={busy || !title.trim()} onClick={saveTitle}>{t('保存')}</button>
              <button className="btn tiny" onClick={() => setTitle(null)}>{t('取消')}</button>
            </div>}

        <div className="bd-meta">
          <label className="bd-meta-item">{t('项目')}
            <select value={n.project_id ?? ''} disabled={busy}
              onChange={e => run(() => patchNeed(id, { project_id: e.target.value === '' ? null : Number(e.target.value) }))}>
              <option value="">{t('不属于项目')}</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.key} · {p.name}</option>)}
            </select>
          </label>
          <label className="bd-meta-item">{t('交给')}
            {bot && <BotAvatar avatar={bot.avatar} size={18} />}
            <select value={bot?.id ?? ''} disabled={busy || archived}
              onChange={e => { const v = Number(e.target.value); run(() => patchNeed(id, { assignee_bot_id: v === primary?.id ? null : v })) }}>
              {!bot && <option value="">{t('已删除的 Bot')}</option>}
              {bots.map(b => <option key={b.id} value={b.id}>{b.name}{b.is_primary ? ' ★' : ''}</option>)}
            </select>
          </label>
          {n.progress && n.progress.total > 0 && <span className="bd-meta-item">{t('完成了')}<b>{t('{done}/{total} 项', { done: n.progress.done, total: n.progress.total })}</b></span>}
        </div>
        {started && !ended && <p className="muted small bd-hint">{t('换人只影响以后：已经在做的任务不换，下次开始时交给新选的 Bot。')}</p>}

        {(n.after.length > 0 || (!ended && !archived)) && <div className="bd-sec">
          <h4>{t('在这之后开始')}{after === null && !archived && <button className="link-btn small" onClick={() => setAfter(n.after)}>{n.after.length ? t('修改') : t('设置')}</button>}</h4>
          {after !== null
            ? <AfterPicker self={n.id} picked={after} onChange={setAfter} onCancel={() => setAfter(null)} onSave={saveAfter} busy={busy} />
            : n.blocked_by.length > 0
              ? n.blocked_by.map(b => <button key={b.id} className="bd-row" onClick={() => openNeed(b.id)}>
                  {b.key && <span className="bd-key">{b.key}</span>}<span className="bd-row-text">{b.title}</span>
                  <span className={`bd-col-chip ${b.column}`}>{columnName(b.column)}</span>
                </button>)
              : <p className="muted small">{n.after.length ? t('前面的 {n} 张卡都完成了。', { n: n.after.length }) : t('不用等别的卡。')}</p>}
        </div>}

        {n.questions.length > 0 && <div className="bd-sec bd-questions">
          <h4>{t('在等你回答')}</h4>
          {n.questions.map(a => <Ask key={a.id} a={a} onGo={() => goTask(a.task_id, a.bot_id, a.chat_id)} />)}
        </div>}

        <div className="bd-sec">
          <h4>{t('描述')}{body === null && <button className="link-btn small" onClick={() => setBody(n.body || '')}>{n.body ? t('改描述') : t('写描述')}</button>}</h4>
          {body === null
            ? n.body ? <Markdown text={n.body} /> : <p className="muted small">{t('没有描述。')}</p>
            : <>
                <textarea rows={6} value={body} autoFocus onChange={e => setBody(e.target.value)} />
                <div className="bd-edit-actions">
                  <button className="btn tiny" onClick={() => setBody(null)}>{t('取消')}</button>
                  <button className="btn tiny primary" disabled={busy} onClick={saveBody}>{t('保存')}</button>
                </div>
              </>}
        </div>

        {n.source_text && <div className="bd-sec">
          <h4>{t('你的原话')}</h4>
          <blockquote className="bd-quote">{n.source_text}</blockquote>
          {n.auto_title && <p className="muted small">{t('标题取自你的原话。')}</p>}
        </div>}

        {n.results.length > 0 && <div className="bd-sec">
          <h4>{t('交付的结果')}</h4>
          {n.results.map((r, i) => <div key={r.task_id + ':' + i} className="bd-result">
            <Markdown text={r.text} />
            <button className="link-btn small" onClick={() => goTask(r.task_id, n.tasks.find(x => x.id === r.task_id)?.bot_id)}>{t('来自任务 #{id}', { id: r.task_id })}</button>
          </div>)}
        </div>}

        {n.tasks.length > 0 && <div className="bd-sec">
          <h4>{t('背后的任务')}</h4>
          {n.tasks.map(x => <button key={x.id} className={`bd-row bd-task ${x.status}` + (x.waiting_kind ? ' waiting' : '')} onClick={() => goTask(x.id, x.bot_id)}>
            <span className="bd-task-id">#{x.id}</span>
            <span className="bd-row-text">{x.goal}{x.last_action && <span className="bd-task-act">{x.last_action}</span>}</span>
            <span className="bd-task-st">{taskStatus(x)}{x.elapsed ? ' · ' + dur(x.elapsed) : ''}</span>
          </button>)}
        </div>}

        {err && <div className="error-line">{err}</div>}
        <div className="modal-actions bd-need-actions">
          {archived
            ? <button className="btn" disabled={busy} onClick={() => run(() => needAction(id, 'restore'))}>{t('从归档里拿回来')}</button>
            : <>
                <button className="btn" disabled={busy} onClick={() => run(() => needAction(id, 'archive'))}>{t('归档')}</button>
                <span className="grow" />
                {!ended && <button className="btn danger" disabled={busy} onClick={() => stop('cancel')}>{t('不用做了')}</button>}
                {!ended && <button className="btn" disabled={busy} onClick={() => stop('done')}>{t('标记完成')}</button>}
                {n.column === 'todo' && <button className="btn primary" disabled={busy} onClick={() => run(() => needAction(id, 'start'))}>{t('开始')}</button>}
              </>}
        </div>
      </div>
    </div>
  )
}

// 选“在这之后开始”：还没完成的卡（自己除外），加上已经选了的
function AfterPicker({ self, picked, onChange, onCancel, onSave, busy }: {
  self: number; picked: number[]; onChange: (ids: number[]) => void; onCancel: () => void; onSave: () => void; busy: boolean
}) {
  const [list, setList] = useState<Need[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    fetchBoard({ project: null, q: '', from: null, to: null, archived: false }).then(b => {
      const all = Object.values(b.columns).flat()
      setList(all.filter(x => x.id !== self && (picked.includes(x.id) || x.column === 'todo' || x.column === 'doing' || x.column === 'waiting')))
    }).catch(e => setErr(e.message))
  }, [])
  return <>
    {err && <div className="error-line">{err}</div>}
    {!list ? <div className="muted small">{t('加载中…')}</div> : !list.length ? <p className="muted small">{t('没有别的还没完成的卡。')}</p> :
      <div className="bd-after">
        {list.map(x => <label key={x.id} className="check">
          <input type="checkbox" checked={picked.includes(x.id)} onChange={e => onChange(e.target.checked ? [...picked, x.id] : picked.filter(i => i !== x.id))} />
          {x.key && <span className="bd-key">{x.key}</span>}<span className="bd-row-text">{x.title}</span>
        </label>)}
      </div>}
    <p className="muted small">{t('选了的卡都完成后，这张卡自动开始，并带上它们的结果；其中有取消或失败的就不开始。')}</p>
    <div className="bd-edit-actions">
      <button className="btn tiny" onClick={onCancel}>{t('取消')}</button>
      <button className="btn tiny primary" disabled={busy || !list} onClick={onSave}>{t('保存')}</button>
    </div>
  </>
}

// 一个在等你的问题（或者确认、请你接手）：点了去那个任务
function Ask({ a, onGo }: { a: Approval; onGo: () => void }) {
  const text = a.kind === 'question' ? a.question || a.goal : a.kind === 'help' ? t('请你接手电脑') : t('等你确认：{tool}', { tool: a.tool })
  return (
    <button className="bd-row bd-ask" onClick={onGo}>
      <span className="bd-row-text">
        {text}
        {a.options && a.options.length > 0 && <span className="bd-opts">{a.options.map(o => <span key={o} className="chip">{o}</span>)}</span>}
      </span>
      <span className="link-btn small">{t('去回答')} →</span>
    </button>
  )
}

export function NewNeed({ projects, project, open, onClose, onCreated }: {
  projects: Project[]; project: number | null; open: Need[]; onClose: () => void; onCreated: (n: Need) => void
}) {
  const bots = useStore(s => s.bots)
  const primary = bots.find(b => b.is_primary)
  const root = useRef<HTMLDivElement>(null)
  useBack(onClose, root)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [proj, setProj] = useState<number | null>(project)
  const [who, setWho] = useState<number | null>(primary?.id ?? null)
  const [after, setAfter] = useState<number[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [])
  const create = async (start: boolean) => {
    if (!title.trim()) return
    setBusy(true)
    try {
      onCreated(await createNeed({
        title: title.trim(), body: body.trim() || undefined, project_id: proj,
        assignee_bot_id: who === primary?.id ? null : who, after, start,
      }))
    } catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return (
    <div className="modal-bg" ref={root} onMouseDown={onClose}>
      <div className="modal medium bd-new" role="dialog" aria-modal="true" onMouseDown={e => e.stopPropagation()}>
        <h3>{t('新建卡片')}</h3>
        <label className="field">{t('标题')}<input value={title} maxLength={200} autoFocus placeholder={t('要做成什么，一句话')} onChange={e => setTitle(e.target.value)} /></label>
        <label className="field">{t('描述（可选）')}<textarea rows={4} value={body} placeholder={t('背景、要求、做到什么程度算完成……')} onChange={e => setBody(e.target.value)} /></label>
        <div className="bd-new-row">
          <label className="field">{t('项目')}
            <select value={proj ?? ''} onChange={e => setProj(e.target.value === '' ? null : Number(e.target.value))}>
              <option value="">{t('不属于项目')}</option>
              {projects.map(p => <option key={p.id} value={p.id}>{p.key} · {p.name}</option>)}
            </select>
          </label>
          <label className="field">{t('交给谁')}
            <select value={who ?? ''} onChange={e => setWho(Number(e.target.value))}>
              {bots.map(b => <option key={b.id} value={b.id}>{b.name}{b.is_primary ? ' ★' : ''}</option>)}
            </select>
          </label>
        </div>
        {open.length > 0 && <div className="field">{t('在这之后开始（可选）')}
          <div className="bd-after">
            {open.map(x => <label key={x.id} className="check">
              <input type="checkbox" checked={after.includes(x.id)} onChange={e => setAfter(e.target.checked ? [...after, x.id] : after.filter(i => i !== x.id))} />
              {x.key && <span className="bd-key">{x.key}</span>}<span className="bd-row-text">{x.title}</span>
            </label>)}
          </div>
        </div>}
        {err && <div className="error-line">{err}</div>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>{t('取消')}</button>
          <span className="grow" />
          <button className="btn" disabled={busy || !title.trim()} onClick={() => create(false)}>{t('放进待办')}</button>
          <button className="btn primary" disabled={busy || !title.trim()} onClick={() => create(true)}>{t('马上开始')}</button>
        </div>
      </div>
    </div>
  )
}
