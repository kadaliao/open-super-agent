export type Avatar = { shape: string; color: string }
export type Bot = {
  id: number; name: string; avatar: Avatar; instructions: string
  is_primary: boolean; created: number; chat_id: number
}
export type Chat = {
  id: number; kind: 'direct' | 'group'; title: string; bot_ids: number[]
  pinned: boolean; updated: number; preview: string
}
export type Message = {
  id: number; ts: number; role: 'user' | 'assistant' | 'task'; content: string
  meta: Record<string, any>; bot_id: number | null
}
export type Task = {
  id: number; goal: string; mode: string; status: string; steps: number
  bot_id: number; chat_id: number; started: number; waiting_approval: number | null; elapsed: number; last_action?: string
}
export type ComputerState = {
  bot_id: number; backend: string; controller: 'bot' | 'human'; state: string; detail: string; width: number; height: number
}
export type Approval = {
  id: number; kind?: 'approval' | 'help'; task_id: number; bot_id: number; chat_id: number; tool: string; goal: string
  args: string; ts: number; expires?: number | null; status?: string; always?: boolean
}
export type Stream = { sid: string; text: string; thinking: string; started: number }
export type Settings = {
  providers: { name: string; base_url: string; key_env: string; key_set: boolean }[]
  roles: Record<string, { provider: string; model: string; thinking?: boolean; reasoning_effort?: string }>
  limits: Record<string, number>
  heavy_workers: number
  known_models: string[]
}
export type App = {
  name: string; command: string; args: string[]; env: Record<string, string>; deny: string[]; confirm: string | string[]
  enabled: boolean; status: string; tools: string[]; connected: boolean; confirm_tools: string[]
}
export type Preset = {
  title: string; desc: string; command: string; args: string[]; deny?: string[]
  fields: { key: string; kind: 'arg' | 'env'; label: string; default?: string; secret?: boolean }[]
}
export type Rule = { id: number; pattern: string; action: 'allow' | 'ask'; note: string; created: number }
