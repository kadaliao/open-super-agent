// 需求看板：类型、请求和侧栏上“等你处理”的数字。接口见 docs/plans/tasks-board.md 的“看板和项目”。
import { useEffect, useSyncExternalStore } from 'react'
import { api, getState, onEvent, openChat, setBackTo, showTask, useStore } from './store'
import { t, tc } from './i18n'
import type { Approval, Bot, Memory, NeedRef } from './types'

// todo 还没交出去；doing 有任务在做；waiting 有任务在等你，或者最近一个任务失败、取消、中断了；done、cancelled 是结束的
export type Column = 'todo' | 'doing' | 'waiting' | 'done' | 'cancelled'
export const COLUMNS: Column[] = ['todo', 'doing', 'waiting', 'done']   // 看板上的四列，已取消的折在下面

// 卡片下面的一个任务（waiting_kind：approval 等你确认、help 请你接手电脑、question 等你回答）
export type NeedTask = {
  id: number; bot_id: number; status: string; waiting_kind: 'approval' | 'help' | 'question' | null
  goal: string; last_action?: string; elapsed?: number
}
// 一张卡（需求）。key 是项目里的编号（OPC-12），不属于项目的是 null；auto_title：标题直接取自你的原话（source_text）
// after：要等哪些卡（id）；blocked_by：其中还没完成的；origin：从哪条消息来的，自己建的是 null
export type Need = {
  id: number; key: string | null; no: number | null; project_id: number | null
  title: string; auto_title: boolean; body: string; source_text: string
  column: Column; assignee_bot_id: number | null
  after: number[]; blocked_by: { id: number; key: string | null; title: string; column: Column }[]
  tasks: NeedTask[]; progress: { done: number; total: number } | null; latest: string
  created: number; updated: number; archived: boolean | number
  origin: { chat_id: number; message_id: number } | null
}
// 卡片页：另带交付的结果和在等你回答的问题（格式和待确认列表里的一样）
export type NeedDetail = Need & { results: { task_id: number; text: string }[]; questions: Approval[] }
export type Project = { id: number; key: string; name: string; brief: string; dir: string; repo: string; count: number; created: number }
// counts：每一列的总数，不受分页影响（旧版服务没有）
export type Board = { columns: Record<Column, Need[]>; projects: Project[]; more: boolean; counts?: Record<Column, number> }

// 看板顶上的筛选。project：项目 id，'none' 是不属于项目的，null 是全部；from / to：创建时间（秒）
export type Filter = { project: number | 'none' | null; q: string; from: number | null; to: number | null; archived: boolean }

export const LIMIT = 200

export function boardUrl(f: Filter, before?: number) {
  const p = new URLSearchParams()
  if (f.project !== null) p.set('project', String(f.project))
  if (f.q.trim()) p.set('q', f.q.trim())
  if (f.from) p.set('from', String(f.from))
  if (f.to) p.set('to', String(f.to))
  p.set('archived', f.archived ? '1' : '0')
  p.set('limit', String(LIMIT))
  if (before) p.set('before', String(before))
  return '/api/board?' + p.toString()
}

export const fetchBoard = (f: Filter, before?: number) => api<Board>(boardUrl(f, before))
export const fetchNeed = (id: number) => api<NeedDetail>(`/api/needs/${id}`)
export const createNeed = (body: { title: string; body?: string; project_id?: number | null; assignee_bot_id?: number | null; after?: number[]; start: boolean }) =>
  api<Need>('/api/needs', { method: 'POST', body })
export const patchNeed = (id: number, body: Partial<Pick<Need, 'title' | 'body' | 'project_id' | 'assignee_bot_id' | 'after'>>) =>
  api<Need>(`/api/needs/${id}`, { method: 'PATCH', body })
export const needAction = (id: number, act: 'start' | 'done' | 'cancel' | 'archive' | 'restore') =>
  api<Need>(`/api/needs/${id}/${act}`, { method: 'POST', body: {} })

export const fetchProjects = () => api<Project[]>('/api/projects')
export type ProjectInput = { key: string; name: string; brief?: string; dir?: string; repo?: string }
export const createProject = (body: ProjectInput) => api<Project>('/api/projects', { method: 'POST', body })
export const patchProject = (id: number, body: Partial<ProjectInput>) => api<Project>(`/api/projects/${id}`, { method: 'PATCH', body })
export const deleteProject = (id: number) => api(`/api/projects/${id}`, { method: 'DELETE' })
export const KEY_RE = /^[A-Z0-9]{2,8}$/

// 项目记忆：只有这个项目的任务读到。改、删用普通记忆的接口（PATCH / DELETE /api/memories/{id}）
export const fetchProjectMemories = (id: number) => api<Memory[]>(`/api/projects/${id}/memories`)
export const addProjectMemory = (id: number, body: { text: string; until?: string | null }) =>
  api<Memory>(`/api/projects/${id}/memories`, { method: 'POST', body })

// 这张卡下面有任务在跑（标记完成、不用做了会停掉它们，先问一句）
export const running = (n: Need) => n.tasks.some(x => x.status === 'running')
// 加载更多：从已经拿到的里面最小的 id 往前取（和对话的 before 一样）
export const oldest = (b: Board) => Math.min(...Object.values(b.columns).flat().map(n => n.id))

// 两份合起来（加载更多）：同一张卡以后来的为准
export function merge(a: Board, b: Board): Board {
  const cols = {} as Record<Column, Need[]>
  for (const c of [...COLUMNS, 'cancelled'] as Column[]) {
    const seen = new Set((b.columns[c] || []).map(n => n.id))
    cols[c] = [...(a.columns[c] || []).filter(n => !seen.has(n.id)), ...(b.columns[c] || [])]
  }
  return { columns: cols, projects: b.projects, more: b.more }
}

export const columnName = (c: Column) => ({
  todo: tc('看板', '待办'), doing: tc('看板', '进行中'), waiting: t('等你处理'), done: tc('看板', '已完成'), cancelled: tc('看板', '已取消'),
})[c]

// 最近的那个任务（id 最大）
const lastTask = (n: Need): NeedTask | undefined => n.tasks.reduce<NeedTask | undefined>((a, x) => !a || x.id > a.id ? x : a, undefined)

// 卡在“等你处理”的原因：任务在等你，或者最近一个任务没做完
export function waitReason(n: Need): string {
  const w = n.tasks.find(x => x.waiting_kind)
  if (w) return w.waiting_kind === 'question' ? t('等你回答') : w.waiting_kind === 'help' ? t('请你接手电脑') : t('等你确认')
  const last = lastTask(n)
  if (last?.status === 'failed') return t('任务失败了')
  if (last?.status === 'cancelled') return t('任务取消了')
  if (last?.status === 'interrupted') return t('任务中断了')
  return ''
}

export const assignee = (bots: Bot[], id: number | null) => id == null ? bots.find(b => b.is_primary) : bots.find(b => b.id === id)

// 去看一个任务：打开它所在的对话，右栏“任务”里打开它的过程。fromNeed：从这张卡的卡片页点过来的，任务详情里能回到卡片
export async function openTask(taskId: number, botId?: number | null, chatId?: number | null, fromNeed?: number) {
  const s = getState()
  const chat = chatId ?? s.tasks[taskId]?.chat_id ?? s.bots.find(b => b.id === botId)?.chat_id
  setBackTo(fromNeed ? { task: taskId, need: fromNeed, view: s.view } : null)
  if (chat) await openChat(chat)
  showTask(taskId)
}

// ---------- 一张卡的简称（编号、标题）：对话里只知道 need_id 的地方用，取一次记下来，卡变了再取 ----------
const refs = new Map<number, NeedRef | null>()   // null：取过，没有这张卡
const loading = new Set<number>()
const refSubs = new Set<() => void>()
let refVer = 0
function loadRef(id: number) {
  if (loading.has(id)) return
  loading.add(id)
  api<Need>(`/api/needs/${id}`).then(n => refs.set(id, { id, key: n.key, title: n.title })).catch(() => refs.set(id, null))
    .finally(() => { loading.delete(id); refVer++; refSubs.forEach(f => f()) })
}
let refWatch = false
export function useNeedRef(id: number | null | undefined, known?: NeedRef | null): NeedRef | null {
  if (!refWatch) {
    refWatch = true
    onBoard(nid => { if (nid !== null && refs.has(nid)) loadRef(nid) })
  }
  useSyncExternalStore(f => { refSubs.add(f); return () => { refSubs.delete(f) } }, () => refVer)
  useEffect(() => { if (id && !known && !refs.has(id)) loadRef(id) }, [id, known])
  if (known) return known
  return id ? refs.get(id) ?? null : null
}

// 卡片有变化（服务端发 {"type":"board","need_id":N}）。几张卡一起变时只回调一次
export function onBoard(f: (needId: number | null) => void) {
  let timer: ReturnType<typeof setTimeout> | null = null
  let ids = new Set<number | null>()
  return onEvent(d => {
    if (d.type !== 'board') return
    ids.add(d.need_id ?? null)
    if (timer) return
    timer = setTimeout(() => {
      const got = ids
      timer = null; ids = new Set()
      got.forEach(id => f(id))
    }, 250)
  })
}

// ---------- 侧栏上的数字：在“等你处理”的卡有几张 ----------
let waiting = 0
let started = false
let hasCounts = false   // 服务端给 counts：只要数字，一张卡都不用取
const subs = new Set<() => void>()

export async function refreshWaiting() {
  try {
    const f: Filter = { project: null, q: '', from: null, to: null, archived: false }
    const b = await api<Board>(hasCounts ? boardUrl(f).replace(`limit=${LIMIT}`, 'limit=1') : boardUrl(f))
    hasCounts = !!b.counts
    // 旧版服务没有 counts：数第一页里 waiting 那一列
    const n = b.counts ? b.counts.waiting || 0 : b.columns.waiting?.length || 0
    if (n !== waiting) { waiting = n; subs.forEach(f => f()) }
  } catch { /* 服务端还没有看板（旧版）：不显示 */ }
}

function start() {
  if (started) return
  started = true
  onBoard(() => refreshWaiting())
}

export function useWaiting() {
  start()
  // 断线重连以后再取一次（断线期间可能漏了事件）
  const connected = useStore(s => s.connected)
  useEffect(() => { if (connected) refreshWaiting() }, [connected])
  return useSyncExternalStore(f => { subs.add(f); return () => { subs.delete(f) } }, () => waiting)
}
