import { useSyncExternalStore } from 'react'
import { syncLang, t } from './i18n'
import type { App, Approval, Attachment, Bot, CatalogItem, Chat, ComputerState, Message, OpenThread, Settings, Skill, Stream, Task, TaskInfo, TaskStep, Teach, TeachDraft, ThreadInfo, ThreadSummary } from './types'
import { notice } from './components/Dialog'
import { desktop, phone } from './desktop'

type State = {
  ready: boolean
  connected: boolean
  bots: Bot[]
  chats: Chat[]
  settings: Settings | null
  shapes: string[]
  colors: string[]
  active: number | null              // 当前打开的对话
  thread: OpenThread | null          // 右边打开着的讨论串（在当前对话里）
  threads: Record<number, ThreadInfo> // 知道的讨论串（id → 摘要）：unread 大于 0 的是有新回复、要提醒你去看的
  visible: boolean                   // 页面正被看着（没切到后台、窗口没藏起来）：这时打开着的讨论串才算看过
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
  view: 'chat' | 'library' | 'apps' | 'observe'  // 主区显示对话、资料库、应用市场还是观测页
  libraryPage: number | null         // 资料库里打开的页面
  teach: Teach | null                // 正在“演示一遍”的录制
  teachDraft: TeachDraft | null      // 录完生成的技能草稿，等你在技能面板里审核
  auth: boolean                      // 服务端开了登录（设置了 OSA_PASSWORD）
  onboarding: boolean                // 第一次打开：显示引导
}

let state: State = {
  ready: false, connected: false, bots: [], chats: [], settings: null, shapes: [], colors: [],
  active: null, thread: null, threads: {}, visible: document.visibilityState === 'visible', messages: {}, hasMore: {}, streams: {}, tasks: {}, taskSteps: {}, openTask: null, approvals: {}, unread: {}, errors: {}, computers: {}, apps: [], catalog: [], categories: [], oauthCallback: '', skills: {},
  view: 'chat', libraryPage: null, teach: null, teachDraft: null, auth: false, onboarding: false,
}
const subs = new Set<() => void>()
const set = (patch: Partial<State> | ((s: State) => Partial<State>)) => {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) }
  subs.forEach(f => f())
  if (desktop) syncBadge()
}

// 桌面 App：菜单栏上显示有几个操作等你确认
let badge = -1
function syncBadge() {
  const n = Object.values(state.approvals).filter(a => a.status === 'pending').length
  if (n !== badge) { badge = n; desktop!.setBadge(n) }
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
  const params = new URLSearchParams(location.search)
  const fromUrl = Number(params.get('chat')), threadRoot = Number(params.get('thread'))   // 讨论串里的消息推送：?chat=12&thread=34
  if (fromUrl) history.replaceState(null, '', location.pathname)
  const saved = fromUrl || Number(localStorage.getItem('osa.active'))
  const active = b.chats.some((c: Chat) => c.id === saved) ? saved : b.bots[0]?.chat_id ?? null
  set({ bots: b.bots, chats: b.chats, settings: b.settings, shapes: b.shapes, colors: b.colors, threads: byId(b.threads),
        tasks, approvals, apps: b.apps || [], catalog: b.catalog || [], categories: b.categories || [], oauthCallback: b.oauth_callback || '', teach: b.teach || null, auth: !!b.auth, onboarding: !!b.onboarding, ready: true })
  if (active) {
    const opened = openChat(active)
    if (active === fromUrl && threadRoot) opened.then(() => openThread(active, threadRoot))
  }
  syncPush()
}

// 打开站内地址指向的对话：推送通知里的地址是 /?chat=12，讨论串里的消息是 /?chat=12&thread=34
export async function openFromUrl(url: string) {
  const q = new URL(url, location.origin).searchParams
  const chat = Number(q.get('chat')), root = Number(q.get('thread'))
  if (!chat || !state.chats.some(c => c.id === chat)) return
  await openChat(chat)
  if (root) openThread(chat, root)
}

// 手机 App：开着推送的话，每次打开都把订阅交给服务器一遍。中转或服务器的推送密钥变了时，App 会重新登记出新的订阅
async function syncPush() {
  if (!desktop?.push) return
  try {
    const { key } = await api('/api/push')
    const sub = await desktop.push.current(key)
    if (sub) await api('/api/push/subscribe', { method: 'POST', body: { subscription: sub } })
  } catch { /* 下次打开再试 */ }
}

// 有新回复的讨论串（bootstrap 里的 threads）按 id 放好
const byId = (list: ThreadInfo[] = []) => Object.fromEntries(list.map(x => [x.id, x]))

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

// 置顶：放进侧栏上面那排；下面的列表照旧按最新消息排
export async function pinChat(chatId: number, pinned: boolean) {
  set(s => ({ chats: s.chats.map(c => c.id === chatId ? { ...c, pinned: pinned ? Date.now() / 1000 : 0 } : c) }))
  try { await api(`/api/chats/${chatId}/pin`, { method: 'POST', body: { pinned } }) }
  catch (e: any) { notice(e.message); refreshLists() }
}

export async function openChat(chatId: number) {
  localStorage.setItem('osa.active', String(chatId))
  set(s => ({ active: chatId, view: 'chat', unread: { ...s.unread, [chatId]: 0 }, thread: s.active === chatId ? s.thread : null }))
  await loadChat(chatId)
}

// ---------- 讨论串 ----------
// 在右边打开某条消息下的讨论串。还没人回复过时先不建，第一次回复时再建（sendInThread）
export async function openThread(chatId: number, rootId: number) {
  const root = state.messages[chatId]?.find(m => m.id === rootId) || null
  set({ thread: { chat: chatId, rootId, root, id: root?.thread?.id ?? null } })
  let r: { root: Message; id: number | null }
  try { r = await api(`/api/chats/${chatId}/threads/${rootId}`) } catch (e: any) {
    if (same(chatId, rootId)) { closeThread(); notice(e.message) }
    return
  }
  if (!same(chatId, rootId)) return
  set(s => ({ thread: { ...s.thread!, root: r.root, id: r.id } }))
  if (r.id) await loadChat(r.id)
}

export const closeThread = () => set({ thread: null })

// 讨论串里的新回复看过了：先在本页去掉提醒，再告诉服务端（其他页面、手机收到 thread_seen 也去掉）。
// 只报到本页已经加载的最后一条，没显示出来的不算看过
export function markThreadSeen(id: number) {
  if (!state.threads[id]?.unread) return
  const list = state.messages[id]
  set(s => ({ threads: { ...s.threads, [id]: { ...s.threads[id], unread: 0 } } }))
  api(`/api/threads/${id}/seen`, { method: 'POST', body: list?.length ? { id: list[list.length - 1].id } : {} }).catch(() => {})
}
const same = (chatId: number, rootId: number) => state.thread?.chat === chatId && state.thread.rootId === rootId

export async function sendInThread(text: string, attachments: Attachment[] = []) {
  const th = state.thread
  if (!th) return
  let id = th.id
  if (!id) {
    const r: { root: Message; id: number } = await api(`/api/chats/${th.chat}/threads`, { method: 'POST', body: { root: th.rootId } })
    id = r.id
    set(s => ({ messages: { ...s.messages, [r.id]: s.messages[r.id] || [] },
                thread: same(th.chat, th.rootId) ? { ...s.thread!, id: r.id, root: r.root } : s.thread }))
  }
  send(id, text, attachments)
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

export function openObserve() {
  set({ view: 'observe' })
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
// 手机 App 退到后台超过 30 秒，系统可能已经把连接掐了而页面不知道：回到前台时直接换一条新连接
let hiddenAt = 0
document.addEventListener('visibilitychange', () => {
  set({ visible: document.visibilityState === 'visible' })
  if (document.visibilityState === 'hidden') hiddenAt = Date.now()
  else if (phone && ws && hiddenAt && Date.now() - hiddenAt > 30_000) {
    const old = ws
    old.onclose = null; old.onmessage = null
    try { old.close() } catch { /* 已经断了 */ }
    connect()
    return
  }
  reportVisibility()
})

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
  set({ messages: {}, streams: {}, tasks, teach: b.teach || null, threads: byId(b.threads) })
  if (state.active) loadChat(state.active)
  if (state.thread) openThread(state.thread.chat, state.thread.rootId)
  Object.keys(state.taskSteps).forEach(id => loadTask(Number(id)))   // 断线期间漏掉的过程
}

// attachments：附件（图片的 media id、电脑里的路径），服务端在正文后面接上“[附件] 路径”
export function send(chatId: number, text: string, attachments: Attachment[] = []) {
  set(s => ({ errors: { ...s.errors, [chatId]: undefined } }))
  ws?.send(JSON.stringify({ type: 'send', chat_id: chatId, text, ...(attachments.length ? { attachments } : {}) }))
}

// edits：在卡片上改过的参数（只认卡片的 editable 里的）
// scope：同意时管多久（once / task / always，见 approvals.resolve）；prefix：终端命令“总是允许这个前缀”的前缀
export function approve(id: number, ok: boolean, note = '', always = false, edits?: Record<string, string>, scope?: string, prefix?: string) {
  ws?.send(JSON.stringify({ type: 'approve', id, ok, note, always, ...(edits ? { edits } : {}), ...(scope ? { scope } : {}), ...(prefix ? { prefix } : {}) }))
}

function handle(d: any) {
  const cid: number = d.chat_id
  switch (d.type) {
    case 'message': {
      const m: Message = d.message
      // 讨论串里的消息：更新原对话里那条消息下面的“N 条回复”；未读和通知都算在原对话上
      const th: (ThreadSummary & { parent: number; root: number }) | undefined = d.thread
      const home = th ? th.parent : cid
      const learned = !!th && same(th.parent, th.root) && !state.thread!.id   // 别的页面先回复了这个讨论串
      set(s => {
        const list = s.messages[cid]
        let messages = list ? { ...s.messages, [cid]: [...list, m] } : s.messages
        const up = th && messages[th.parent]
        if (th && up) messages = { ...messages, [th.parent]: up.map(x => x.id === th.root
          ? { ...x, thread: { id: th.id, count: th.count, last: th.last, bots: th.bots } } : x) }
        // 讨论串里的消息：上级对话的“有讨论串”和讨论串的新回复数（服务端算好的）；侧栏的数字也算在上级对话上
        const chats = th ? s.chats.map(c => c.id === th.parent && !c.threads ? { ...c, threads: 1 } : c)
          : s.chats.map(c => c.id === cid ? { ...c, updated: m.ts, preview: m.content.slice(0, 80) } : c)
            .sort((a, b) => b.updated - a.updated)   // 置顶的也按最新消息排，另外在上面那排
        const said = m.role !== 'user' && !!m.content
        const threads = th ? { ...s.threads, [th.id]: { ...s.threads[th.id], ...th, unread: th.unread ?? 0,
          ...(said ? { preview: m.content.slice(0, 200), preview_bot: m.bot_id } : {}) } } : s.threads
        const unread = !th && home !== s.active && m.role !== 'user' ? { ...s.unread, [home]: (s.unread[home] || 0) + 1 } : s.unread
        const streams = m.role === 'assistant' ? { ...s.streams, [cid]: undefined } : s.streams
        return { chats, threads, unread, streams, messages, ...(learned ? { thread: { ...s.thread!, id: cid } } : {}) }
      })
      if (learned) loadChat(cid)
      if (th && state.thread?.id === cid && state.visible) markThreadSeen(cid)   // 正开着这个讨论串
      // 桌面 App 里窗口藏起来时，当前对话的回复也要通知
      if (m.role !== 'user' && (home !== state.active || (th && state.thread?.id !== cid) || desktop)) notifyOS(home, m, undefined, th && { id: cid, root: th.root })
      break
    }
    case 'thread_seen':   // 别的页面或手机上看过了这个讨论串
      set(s => ({ threads: { ...s.threads, [d.thread.id]: { ...s.threads[d.thread.id], ...d.thread, parent: d.thread.parent, root: d.thread.root } } }))
      break
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
      if (d.status === 'pending' && (document.hidden || desktop)) notifyOS(cid, null, t('有一个操作需要你确认'))
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
    case 'plugins_changed':
      window.dispatchEvent(new CustomEvent('osa-plugins'))
      break
    case 'page_data':   // 小应用的数据被 Bot 或别的窗口改了：转给打开着的小应用（applet.ts）
      window.dispatchEvent(new CustomEvent('osa-page-data', { detail: { page_id: d.page_id, data: d.data } }))
      break
    case 'channels_changed':
      window.dispatchEvent(new CustomEvent('osa-channels'))
      break
    case 'teach':   // 录制开始 / 结束；到时自动结束时会带上草稿
      set({ teach: d.teach || null, ...(d.draft ? { teachDraft: { bot_id: d.bot_id, draft: d.draft, steps: d.steps || [] } } : {}) })
      if (d.error) notice(d.error)
      break
  }
}

// th：消息在讨论串里（id 是讨论串，root 是被回复的那条消息），点通知时打开那个讨论串
function notifyOS(chatId: number, m: Message | null, text?: string, th?: { id: number; root: number }) {
  if (phone) return   // 手机 App 在前台时看得到；退到后台由服务端推送
  if (desktop) return notifyDesktop(chatId, m, text, th?.id)
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return
  if (localStorage.getItem('osa.push') === '1') return   // 这台设备开了推送，由服务端推，免得重复
  const chat = state.chats.find(c => c.id === chatId)
  const n = new Notification(chat?.title || 'Open Super Agent', { body: text || m?.content.slice(0, 120) })
  n.onclick = async () => { window.focus(); await openChat(chatId); if (th) openThread(chatId, th.root) }
}

// 桌面 App 用系统通知：Bot 的回复、任务结果、等你确认的操作，和服务端推送的规则一样。
// 窗口在不在前台由外壳判断（WebKit 的 hasFocus 在 App 里不准）：正看着这个对话时不打扰（讨论串的新回复对话里有提醒条）。
// 讨论串里的回复用讨论串的 id 发：点通知时外壳把这个 id 发回来，页面打开那个讨论串
function notifyDesktop(chatId: number, m: Message | null, text?: string, threadId?: number) {
  if (m && m.role !== 'assistant' && m.meta?.kind !== 'result' && m.meta?.kind !== 'image') return
  const chat = state.chats.find(c => c.id === chatId)
  const title = (chat?.title || 'Open Super Agent') + (threadId ? ' · ' + t('讨论串') : '')
  desktop!.notify(title, text || (m?.content || '').replace(/[#*`>_~\[\]]/g, '').slice(0, 120), threadId ?? chatId, chatId === state.active)
}

// 打开一个对话或讨论串：是对话就打开对话，是讨论串就打开它所在的对话和这个讨论串。
// 系统通知（桌面 App 发回的 id）和消息里的“去看”都用它，因为指向的可能是讨论串，讨论串不在侧栏的对话列表里
export async function openChatOrThread(id: number) {
  if (state.chats.some(c => c.id === id)) return openChat(id)
  const th = state.threads[id] || await api<ThreadInfo>(`/api/threads/${id}`).catch(() => null)   // 页面重新加载过、早看过了
  if (th && state.chats.some(c => c.id === th.parent)) { await openChat(th.parent); openThread(th.parent, th.root) }
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
