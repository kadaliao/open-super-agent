import { useSyncExternalStore } from 'react'
import { syncLang, t } from './i18n'
import type { App, Approval, Bot, CatalogItem, Chat, ComputerState, Message, Settings, Skill, Stream, Task, TaskInfo, TaskStep, Teach, TeachDraft } from './types'

type State = {
  ready: boolean
  connected: boolean
  bots: Bot[]
  chats: Chat[]
  settings: Settings | null
  shapes: string[]
  colors: string[]
  active: number | null              // 当前打开的对话
  messages: Record<number, Message[]>
  hasMore: Record<number, boolean>
  streams: Record<number, Stream | undefined>
  tasks: Record<number, Task>
  taskSteps: Record<number, TaskStep[]>   // 打开着的任务详情的过程，边做边更新
  openTask: number | null            // 右栏“任务”里打开的是哪个任务
  approvals: Record<number, Approval>
  unread: Record<number, number>
  errors: Record<number, string | undefined>
  computers: Record<number, ComputerState>
  apps: App[]
  catalog: CatalogItem[]             // 应用市场
  categories: [string, string][]     // 应用市场的分类：[键, 名字]，按显示顺序
  oauthCallback: string              // 登录授权的回调地址：自己建 OAuth 客户端时要填到对方那里
  skills: Record<number, Skill[]>    // 每个 Bot 能用的技能（含共用的）
  view: 'chat' | 'library' | 'apps'  // 主区显示对话、资料库还是应用市场
  libraryPage: number | null         // 资料库里打开的页面
  teach: Teach | null                // 正在“演示一遍”的录制
  teachDraft: TeachDraft | null      // 录完生成的技能草稿，等你在技能面板里审核
  auth: boolean                      // 服务端开了登录（设置了 OSA_PASSWORD）
  onboarding: boolean                // 第一次打开：显示引导
}

let state: State = {
  ready: false, connected: false, bots: [], chats: [], settings: null, shapes: [], colors: [],
  active: null, messages: {}, hasMore: {}, streams: {}, tasks: {}, taskSteps: {}, openTask: null, approvals: {}, unread: {}, errors: {}, computers: {}, apps: [], catalog: [], categories: [], oauthCallback: '', skills: {},
  view: 'chat', libraryPage: null, teach: null, teachDraft: null, auth: false, onboarding: false,
}
const subs = new Set<() => void>()
const set = (patch: Partial<State> | ((s: State) => Partial<State>)) => {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) }
  subs.forEach(f => f())
}
export const getState = () => state
export const setSettings = (settings: Settings) => set({ settings })
// 所有 Bot 共用一台电脑：任何一个 Bot 的电脑状态变了，其他 Bot 的面板也跟着变
export const setComputer = (c: ComputerState) => set(s => ({
  computers: Object.fromEntries([...new Set([...Object.keys(s.computers).map(Number), c.bot_id])].map(k => [k, { ...c, bot_id: k }])),
}))

export function useStore<T>(sel: (s: State) => T): T {
  return useSyncExternalStore(cb => (subs.add(cb), () => subs.delete(cb)), () => sel(state))
}

// ---------- REST ----------
// 登录过期：回登录页，登录后回到现在的位置
export function toLogin() {
  location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search)
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any } = {}): Promise<T> {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'content-type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  })
  if (r.status === 401 && !path.startsWith('/api/shared/')) toLogin()
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || t('请求失败（{status}）', { status: r.status }))
  return data
}

export async function bootstrap() {
  const b = await api('/api/bootstrap')
  if (syncLang(b.settings?.language)) return new Promise<void>(() => {})   // 语言和服务端的不一样：重新加载中
  const tasks: Record<number, Task> = {}
  b.tasks.forEach((t: Task) => (tasks[t.id] = t))
  const approvals: Record<number, Approval> = {}
  b.approvals.forEach((a: Approval) => (approvals[a.id] = { ...a, status: 'pending' }))
  // 从推送通知点进来时地址是 /?chat=12
  const fromUrl = Number(new URLSearchParams(location.search).get('chat'))
  if (fromUrl) history.replaceState(null, '', location.pathname)
  const saved = fromUrl || Number(localStorage.getItem('osa.active'))
  const active = b.chats.some((c: Chat) => c.id === saved) ? saved : b.bots[0]?.chat_id ?? null
  set({ bots: b.bots, chats: b.chats, settings: b.settings, shapes: b.shapes, colors: b.colors,
        tasks, approvals, apps: b.apps || [], catalog: b.catalog || [], categories: b.categories || [], oauthCallback: b.oauth_callback || '', teach: b.teach || null, auth: !!b.auth, onboarding: !!b.onboarding, ready: true })
  if (active) openChat(active)
}

export const setTeachDraft = (d: TeachDraft | null) => set({ teachDraft: d })

export async function refreshLists() {
  const b = await api('/api/bootstrap')
  set({ bots: b.bots, chats: b.chats, settings: b.settings, apps: b.apps || [] })
}

export async function refreshApps() {
  const r = await api('/api/apps')
  set({ apps: r.apps, catalog: r.catalog, categories: r.categories || [], oauthCallback: r.oauth_callback || '' })
  return r
}

export async function loadSkills(botId: number) {
  const list: Skill[] = await api(`/api/bots/${botId}/skills`)
  set(s => ({ skills: { ...s.skills, [botId]: list } }))
  return list
}

export async function openChat(chatId: number) {
  localStorage.setItem('osa.active', String(chatId))
  set(s => ({ active: chatId, view: 'chat', unread: { ...s.unread, [chatId]: 0 } }))
  await loadChat(chatId)
}

// 对话还没加载过就拉最近 60 条（不切换主区显示什么）
async function loadChat(chatId: number) {
  if (!state.messages[chatId]) {
    const list: Message[] = await api(`/api/chats/${chatId}/messages?limit=60`)
    set(s => ({ messages: { ...s.messages, [chatId]: list }, hasMore: { ...s.hasMore, [chatId]: list.length === 60 } }))
  }
}

// ---------- 任务过程 ----------
// 加载过程的请求还没回来时，收到的过程事件先排着，回来以后按顺序补上，不丢也不重复
const stepQueue = new Map<number, any[]>()

function applyStep(list: TaskStep[], d: any): TaskStep[] {
  if (d.step) {
    const i = list.findIndex(x => x.id === d.step.id)
    if (i >= 0) return list.map((x, j) => (j === i ? d.step : x))
    return [...list, d.step].sort((a, b) => a.id - b.id)
  }
  if (d.append) return list.map(x => {
    // at：这段从第几个字开始。已经有的部分跳过；中间缺了一段就先不接，这一步写完时会整段补上
    if (x.id !== d.id || d.at > x.body.length) return x
    return { ...x, body: x.body + d.append.slice(x.body.length - d.at) }
  })
  return list
}

// 拉任务详情和全部过程；之后过程靠事件更新。同一个任务同时只拉一次
export async function loadTask(id: number): Promise<TaskInfo | undefined> {
  if (stepQueue.has(id)) return
  stepQueue.set(id, [])
  try {
    const r = await api(`/api/tasks/${id}`)
    const queued = stepQueue.get(id) || []
    set(s => ({ taskSteps: { ...s.taskSteps, [id]: queued.reduce(applyStep, r.steps as TaskStep[]) } }))
    return r.task
  } finally { stepQueue.delete(id) }
}

// 只要任务详情（状态、结果、花费），过程不动
export const fetchTask = async (id: number): Promise<TaskInfo> => (await api(`/api/tasks/${id}`)).task

export function dropTask(id: number) {
  set(s => { const t = { ...s.taskSteps }; delete t[id]; return { taskSteps: t } })
}

// 在右栏打开某个任务的过程（任务卡片、结果、定时任务的运行记录里点“过程”）
export function showTask(id: number | null) {
  set({ openTask: id })
  if (id) window.dispatchEvent(new CustomEvent('osa-open-task'))
}

export function openLibrary(pageId: number | null = null) {
  set({ view: 'library', libraryPage: pageId })
}

export function openApps() {
  set({ view: 'apps' })
  refreshApps()
}

export async function loadOlder(chatId: number) {
  const cur = state.messages[chatId] || []
  if (!cur.length || !state.hasMore[chatId]) return
  const list: Message[] = await api(`/api/chats/${chatId}/messages?before=${cur[0].id}&limit=60`)
  set(s => ({ messages: { ...s.messages, [chatId]: [...list, ...(s.messages[chatId] || [])] },
              hasMore: { ...s.hasMore, [chatId]: list.length === 60 } }))
}

// ---------- WebSocket ----------
let ws: WebSocket | null = null
// 告诉服务端这个页面是不是正被看着：没有页面被看着时，服务端才推送到手机
const reportVisibility = () => {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'visibility', visible: document.visibilityState === 'visible' }))
}
document.addEventListener('visibilitychange', reportVisibility)

export function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws')
  ws.onopen = () => { set({ connected: true }); reportVisibility(); if (state.ready) resync() }
  ws.onclose = () => {
    set({ connected: false })
    // WebSocket 被拒时拿不到状态码：问一下接口，没登录的话 api() 会跳去登录页
    if (state.auth) api('/api/me').catch(() => {})
    setTimeout(connect, 1500)
  }
  ws.onmessage = e => handle(JSON.parse(e.data))
}

async function resync() {
  // 断线重连后：刷新列表，丢掉已缓存的消息，重新加载当前对话
  await refreshLists()
  const b = await api('/api/bootstrap')
  if (syncLang(b.settings?.language)) return new Promise<void>(() => {})   // 语言和服务端的不一样：重新加载中
  const tasks: Record<number, Task> = {}
  b.tasks.forEach((t: Task) => (tasks[t.id] = t))
  set({ messages: {}, streams: {}, tasks, teach: b.teach || null })
  if (state.active) loadChat(state.active)
  Object.keys(state.taskSteps).forEach(id => loadTask(Number(id)))   // 断线期间漏掉的过程
}

export function send(chatId: number, text: string) {
  set(s => ({ errors: { ...s.errors, [chatId]: undefined } }))
  ws?.send(JSON.stringify({ type: 'send', chat_id: chatId, text }))
}

export function approve(id: number, ok: boolean, note = '', always = false) {
  ws?.send(JSON.stringify({ type: 'approve', id, ok, note, always }))
}

function handle(d: any) {
  const cid: number = d.chat_id
  switch (d.type) {
    case 'message': {
      const m: Message = d.message
      set(s => {
        const list = s.messages[cid]
        const chats = s.chats.map(c => c.id === cid ? { ...c, updated: m.ts, preview: m.content.slice(0, 80) } : c)
          .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updated - a.updated)
        const unread = cid !== s.active && m.role !== 'user' ? { ...s.unread, [cid]: (s.unread[cid] || 0) + 1 } : s.unread
        const streams = m.role === 'assistant' ? { ...s.streams, [cid]: undefined } : s.streams
        return { chats, unread, streams, messages: list ? { ...s.messages, [cid]: [...list, m] } : s.messages }
      })
      if (m.role !== 'user' && cid !== state.active) notifyOS(cid, m)
      break
    }
    case 'stream_start':
      set(s => ({ streams: { ...s.streams, [cid]: { sid: d.sid, text: '', thinking: '', started: Date.now(), bot_id: d.bot_id } } }))
      break
    case 'stream':
      set(s => {
        const st = s.streams[cid]
        if (!st || st.sid !== d.sid) return {}
        return { streams: { ...s.streams, [cid]: { ...st, text: st.text + (d.delta || ''), thinking: st.thinking + (d.thinking || '') } } }
      })
      break
    case 'task':
      set(s => ({ tasks: { ...s.tasks, [d.task.id]: d.task } }))
      break
    case 'task_step': {
      const q = stepQueue.get(d.task_id)
      if (q) q.push(d)
      else if (state.taskSteps[d.task_id]) set(s => ({ taskSteps: { ...s.taskSteps, [d.task_id]: applyStep(s.taskSteps[d.task_id], d) } }))
      break
    }
    case 'approval':
      set(s => ({ approvals: { ...s.approvals, [d.id]: { ...(s.approvals[d.id] || {}), ...d } } }))
      if (d.status === 'pending' && document.hidden) notifyOS(cid, null, t('有一个操作需要你确认'))
      break
    case 'error':
      set(s => ({ errors: { ...s.errors, [cid]: d.message }, streams: { ...s.streams, [cid]: undefined } }))
      break
    case 'computer':
      setComputer(d.computer)
      break
    case 'apps_changed':
      refreshApps()
      break
    case 'bots_changed':
      refreshLists()
      break
    case 'skills_changed':
      Object.keys(state.skills).forEach(id => loadSkills(Number(id)))
      break
    case 'routines_changed':
      window.dispatchEvent(new CustomEvent('osa-routines', { detail: d.bot_id }))
      break
    case 'memories_changed':
      window.dispatchEvent(new CustomEvent('osa-memories'))
      break
    case 'pages_changed':
      window.dispatchEvent(new CustomEvent('osa-pages'))
      break
    case 'channels_changed':
      window.dispatchEvent(new CustomEvent('osa-channels'))
      break
    case 'teach':   // 录制开始 / 结束；到时自动结束时会带上草稿
      set({ teach: d.teach || null, ...(d.draft ? { teachDraft: { bot_id: d.bot_id, draft: d.draft, steps: d.steps || [] } } : {}) })
      if (d.error) alert(d.error)
      break
  }
}

function notifyOS(chatId: number, m: Message | null, text?: string) {
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return
  if (localStorage.getItem('osa.push') === '1') return   // 这台设备开了推送，由服务端推，免得重复
  const chat = state.chats.find(c => c.id === chatId)
  const n = new Notification(chat?.title || 'Open Super Agent', { body: text || m?.content.slice(0, 120) })
  n.onclick = () => { window.focus(); openChat(chatId) }
}

export const botById = (id: number | null | undefined) => state.bots.find(b => b.id === id)
export const botForChat = (s: State, chatId: number | null) => {
  const c = s.chats.find(x => x.id === chatId)
  return c && c.kind === 'direct' ? s.bots.find(b => b.id === c.bot_ids[0]) : undefined
}
export const chatById = (s: State, chatId: number | null) => s.chats.find(x => x.id === chatId)
export const membersOf = (s: State, chatId: number | null) => {
  const c = chatById(s, chatId)
  return c ? c.bot_ids.map(i => s.bots.find(b => b.id === i)).filter((b): b is Bot => !!b) : []
}
