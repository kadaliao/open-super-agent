export type Avatar = { shape: string; color: string }
export type Bot = {
  id: number; name: string; avatar: Avatar; instructions: string
  is_primary: boolean; created: number; chat_id: number
}
export type Chat = {
  id: number; kind: 'direct' | 'group'; title: string; bot_ids: number[]
  pinned: boolean; updated: number; preview: string; lead: number | null   // lead：群聊的协调者
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
  id: number; kind?: 'approval' | 'help'; task_id: number; bot_id: number; chat_id: number; origin_chat_id?: number; tool: string; goal: string
  args: string; ts: number; expires?: number | null; status?: string; always?: boolean
}
export type Stream = { sid: string; text: string; thinking: string; started: number; bot_id: number }
export type Settings = {
  providers: { name: string; base_url: string; key_env: string; key_set: boolean }[]
  roles: Record<string, { provider: string; model: string; thinking?: boolean; reasoning_effort?: string }>
  limits: Record<string, number>
  heavy_workers: number
  known_models: string[]
  embedding: { provider: string; model: string }
}
export type SearchHit = { kind: 'memory' | 'page'; ref: number; title: string; snippet: string; bot_id: number | null; sub: string; score: number }
export type AppState = 'connected' | 'connecting' | 'login' | 'needs_login' | 'off' | 'error'
export type App = {
  name: string; catalog: string; type: 'stdio' | 'http' | 'sse'
  command: string; args: string[]; env: Record<string, string>; url: string; headers: string[]; oauth: boolean
  deny: string[]; confirm: string | string[]
  enabled: boolean; status: string; state: AppState; tools: string[]; connected: boolean; confirm_tools: string[]
}
// 应用市场里的一项。kind：oauth 登录授权 / token 填密钥 / none 免配置。
// 登录授权的也可以先填字段（比如自己建的 OAuth 客户端）；client：用户自己建的客户端，要告诉他回调地址
export type CatalogField = { key: string; label: string; placeholder?: string; secret?: boolean; default?: string; optional?: boolean }
// 清单里的图标：品牌色 + 品牌图形（svg 是 24×24 的 path）或线条图标的名字（glyph）
export type AppIconSpec = { color: string; svg?: string; glyph?: string }
export type CatalogItem = {
  id: string; title: string; category: string; desc: string; about: string; can: string[]; example: string
  icon?: AppIconSpec; by: string; kind: 'oauth' | 'token' | 'none'
  fields: CatalogField[]; steps: string[]; link?: string; link_label?: string; note?: string; how?: string
  remote: boolean; client: boolean
  copy?: { label: string; text: string }   // 让用户整段复制到对方那里的配置，{callback} 换成回调地址
}
export type Rule = { id: number; pattern: string; action: 'allow' | 'ask'; note: string; created: number }
export type Skill = {
  id: number; bot_id: number | null; name: string; description: string; body: string; source: string; created: number; updated: number
  share_token: string | null
}
export type SkillDraft = { name: string; description: string; body: string; apps: { name: string; title: string; connected: boolean }[]; exists: boolean }
export type Trigger =
  | { kind: 'daily'; times: string[]; days: number[] }
  | { kind: 'every'; minutes: number }
  | { kind: 'webhook'; include: string[]; exclude: string[]; llm_filter: string }
export type RoutineRun = {
  id: number; ts: number; source: string; status: string; task_id: number | null; detail: string; finished: number | null
}
export type Routine = {
  id: number; bot_id: number; name: string; prompt: string; skill: string; trigger: Trigger; enabled: boolean
  next_run: number | null; running: boolean; last_run: RoutineRun | null
}
export type Memory = {
  id: number; bot_id: number | null; shared: boolean; kind: 'note' | 'work'; text: string; source: string; created: number; updated: number
}
export type MemoryList = { shared: Memory[]; own: Memory[]; work: Memory[] }
export type Page = {
  id: number; bot_id: number | null; title: string; format: 'markdown' | 'html'; share_token: string | null; pinned: boolean
  task_id: number | null; created: number; updated: number; size: number; content?: string
}
export type Teach = { bot_id: number; started: number; note: string; max_s: number }
export type TeachDraft = { bot_id: number; draft: { name: string; description: string; body: string }; steps: string[] }
export type FileEntry ={ name: string; path: string; dir: boolean; size: number; mtime: number; missing?: boolean }
export type FileListing =
  | { running: false; state: string; path: string; root: string }
  | { running: true; path: string; root: string; entries: FileEntry[]; pins: FileEntry[] }
export type ChannelKind = 'telegram' | 'feishu' | 'dingtalk' | 'wecom' | 'discord'
export type Channel = {
  id: number; kind: ChannelKind; title: string; name: string; chat_id: number; enabled: boolean
  state: 'connected' | 'connecting' | 'error' | 'off'; error: string; paired: boolean; peer_name: string; created: number
  pair_code?: string; pair_expires?: number; pair_link?: string; setup_link?: string
}
