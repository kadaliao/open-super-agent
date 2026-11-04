import { useSyncExternalStore } from 'react'
import type { App, Approval, Bot, Chat, ComputerState, Message, Settings, Stream, Task } from './types'

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
  approvals: Record<number, Approval>
  unread: Record<number, number>
  errors: Record<number, string | undefined>
  computers: Record<number, ComputerState>
  apps: App[]
}

let state: State = {
  ready: false, connected: false, bots: [], chats: [], settings: null, shapes: [], colors: [],
  active: null, messages: {}, hasMore: {}, streams: {}, tasks: {}, approvals: {}, unread: {}, errors: {}, computers: {}, apps: [],
}
const subs = new Set<() => void>()
const set = (patch: Partial<State> | ((s: State) => Partial<State>)) => {
  state = { ...state, ...(typeof patch === 'function' ? patch(state) : patch) }
  subs.forEach(f => f())
}
export const getState = () => state
export const setComputer = (c: ComputerState) => set(s => ({ computers: { ...s.computers, [c.bot_id]: c } }))

export function useStore<T>(sel: (s: State) => T): T {
  return useSyncExternalStore(cb => (subs.add(cb), () => subs.delete(cb)), () => sel(state))
}

// ---------- REST ----------
export async function api<T = any>(path: string, opts: { method?: string; body?: any } = {}): Promise<T> {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'content-type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  })
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(data.detail || `请求失败（${r.status}）`)
  return data
}

export async function bootstrap() {
  const b = await api('/api/bootstrap')
  const tasks: Record<number, Task> = {}
  b.tasks.forEach((t: Task) => (tasks[t.id] = t))
  const approvals: Record<number, Approval> = {}
  b.approvals.forEach((a: Approval) => (approvals[a.id] = { ...a, status: 'pending' }))
  const saved = Number(localStorage.getItem('osa.active'))
  const active = b.chats.some((c: Chat) => c.id === saved) ? saved : b.bots[0]?.chat_id ?? null
  set({ bots: b.bots, chats: b.chats, settings: b.settings, shapes: b.shapes, colors: b.colors,
        tasks, approvals, apps: b.apps || [], ready: true })
  if (active) openChat(active)
}

export async function refreshLists() {
  const b = await api('/api/bootstrap')
  set({ bots: b.bots, chats: b.chats, settings: b.settings, apps: b.apps || [] })
}

export async function refreshApps() {
  const r = await api('/api/apps')
  set({ apps: r.apps })
  return r
}

export async function openChat(chatId: number) {
  localStorage.setItem('osa.active', String(chatId))
  set(s => ({ active: chatId, unread: { ...s.unread, [chatId]: 0 } }))
  if (!state.messages[chatId]) {
    const list: Message[] = await api(`/api/chats/${chatId}/messages?limit=60`)
    set(s => ({ messages: { ...s.messages, [chatId]: list }, hasMore: { ...s.hasMore, [chatId]: list.length === 60 } }))
  }
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
export function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws')
  ws.onopen = () => { set({ connected: true }); if (state.ready) resync() }
  ws.onclose = () => { set({ connected: false }); setTimeout(connect, 1500) }
  ws.onmessage = e => handle(JSON.parse(e.data))
}

async function resync() {
  // 断线重连后：刷新列表，丢掉已缓存的消息，重新加载当前对话
  await refreshLists()
  const b = await api('/api/bootstrap')
  const tasks: Record<number, Task> = {}
  b.tasks.forEach((t: Task) => (tasks[t.id] = t))
  set({ messages: {}, streams: {}, tasks })
  if (state.active) openChat(state.active)
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
      set(s => ({ streams: { ...s.streams, [cid]: { sid: d.sid, text: '', thinking: '', started: Date.now() } } }))
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
    case 'approval':
      set(s => ({ approvals: { ...s.approvals, [d.id]: { ...(s.approvals[d.id] || {}), ...d } } }))
      if (d.status === 'pending' && document.hidden) notifyOS(cid, null, '有一个操作需要你确认')
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
  }
}

function notifyOS(chatId: number, m: Message | null, text?: string) {
  if (!('Notification' in window) || Notification.permission !== 'granted' || !document.hidden) return
  const chat = state.chats.find(c => c.id === chatId)
  const n = new Notification(chat?.title || 'Open Super Agent', { body: text || m?.content.slice(0, 120) })
  n.onclick = () => { window.focus(); openChat(chatId) }
}

export const botById = (id: number | null | undefined) => state.bots.find(b => b.id === id)
export const botForChat = (s: State, chatId: number | null) => {
  const c = s.chats.find(x => x.id === chatId)
  return c ? s.bots.find(b => b.id === c.bot_ids[0]) : undefined
}
