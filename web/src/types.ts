// image：自己的头像图片（/avatars/…，上传的或 AI 画的）。有图片时显示图片，形状和颜色留着备用
export type Avatar = { shape: string; color: string; image?: string }
export type Bot = {
  id: number; name: string; avatar: Avatar; instructions: string
  is_primary: boolean; created: number; chat_id: number
}
export type Chat = {
  id: number; kind: 'direct' | 'group'; title: string; bot_ids: number[]
  // pinned：置顶的时间（侧栏上面那排按它排，先置顶的在前），0 是没置顶。lead：群聊的协调者
  pinned: number; updated: number; preview: string; lead: number | null
  image: string | null   // 群聊自己的头像图片；没有就用成员的头像拼
  threads?: number       // 下面有几个讨论串：有的话对话顶上显示“讨论串”按钮
}
// 消息的附件（meta.attachments，见 osa/media.py）：path 是电脑里的文件，media 是存在服务端的图片（缩略图、给模型看）
export type Attachment = { name: string; size?: number; path?: string; media?: string; mime?: string; w?: number; h?: number }
export type Message = {
  id: number; ts: number; role: 'user' | 'assistant' | 'task'; content: string
  meta: Record<string, any>; bot_id: number | null
  thread?: ThreadSummary   // 这条消息下面有讨论串
}
// 讨论串：针对某条消息的回复，是挂在原对话下面的一个子对话（id 是它的对话 id）。bots：回复过的 Bot；unread：你还没看过的 Bot 回复数
export type ThreadSummary = { id: number; count: number; last: number | null; bots: number[]; unread?: number }
// 讨论串列表里的一项（对话顶上的“讨论串”列表、新回复提醒）：parent 是所在的对话，root 是被回复的那条消息。
// root_text / root_role / root_bot：被回复的那条消息；preview / preview_bot：最后一条回复
export type ThreadInfo = ThreadSummary & {
  parent: number; root: number; unread: number
  root_text?: string; root_role?: string; root_bot?: number | null; preview?: string; preview_bot?: number | null
}
// 右边打开着的讨论串：chat 是原对话，rootId 是被回复的那条消息；id 在第一次回复之前是 null
export type OpenThread = { chat: number; rootId: number; root: Message | null; id: number | null }
export type Task = {
  id: number; goal: string; mode: string; status: string; steps: number
  bot_id: number; chat_id: number; started: number; waiting_approval: number | null; elapsed: number; last_action?: string
  quiet?: boolean   // 定时任务这次没有新内容：对话里不显示
}
// 任务过程的一步。kind：think 思考、text 正在写、say 调工具前说的话、tool 工具调用（detail 参数、body 返回）、
// answer 写出的结果（Heavy 里研究员的草稿也是）、note 过程提示。worker：Heavy 里的研究员 h0、h1……，汇总是 review
export type TaskStep = {
  id: number; task_id: number; ts: number; worker: string; kind: 'think' | 'text' | 'say' | 'tool' | 'answer' | 'note'
  title: string; body: string; status: string; dur: number; detail: string
}
// 任务列表、任务详情里的一项：数据库里的记录，正在跑的盖上实时状态
export type TaskInfo = {
  id: number; ts: number; goal: string; mode: string; status: string; chat_id: number; bot_id: number
  elapsed?: number | null; steps?: number | null; handoff_to?: number | null
  started?: number; waiting_approval?: number | null; last_action?: string
  result?: string; finished?: number; cost?: number; tokens?: number; currency?: string
}
// 正在用电脑的任务：每个占一个浏览器窗口（window：已经开了窗口）
export type ComputerUser = { key: string; label: string; task_id: number | null; bot_id: number | null; window: boolean }
export type ComputerState = {
  bot_id: number; backend: string; controller: 'bot' | 'human'; state: string; detail: string; width: number; height: number
  parallel?: number; users?: ComputerUser[]; waiting?: string[]; watch?: string | null; exclusive?: string
  workspace?: string; shell?: boolean   // shell：本机电脑的终端开没开
  desktop?: DesktopState                // 本机电脑的“操作整台电脑”（只有 Mac）
  link?: { connected: boolean; name: string }   // 借给服务器的 Mac 连没连上（backend 是 linked 时用的就是它）
}
// supported：是 Mac；accessibility / screen：两个 macOS 权限（都给了 Bot 才能操作整个屏幕）
// problem：现在不能操作的原因（缺权限、屏幕锁着）；owner：谁在用屏幕；paused_by=input：你动了鼠标键盘，Bot 自己停下的
export type DesktopState = {
  supported: boolean; accessibility?: boolean; screen?: boolean; problem?: string; owner?: string; paused_by?: string
}
export type Approval = {
  id: number; kind?: 'approval' | 'help'; task_id: number; bot_id: number; chat_id: number; origin_chat_id?: number; tool: string; goal: string
  origin_thread?: number   // 任务是在讨论串里派的：origin_chat_id 是讨论串所在的对话，这个是讨论串
  args: string; ts: number; expires?: number | null; status?: string; always?: boolean
  once?: boolean   // 只能一次一次地允许（新建、修改、删除 Bot，Worker 的 ask_approval），没有“本次任务内允许”“总是允许”
  scopes?: string[]   // 同意时能选的作用域：once / task（这个任务里不再问）/ always。旧版服务没有，按 once 推
  prefix?: string     // 终端命令：“总是允许这个前缀”的默认前缀（命令的第一个词）
  compound?: boolean  // 终端命令带 ; | $( 这类符号：按前缀放行管不到它
  scope?: string      // 有了结果：用户选的作用域
  editable?: string[]   // 能在卡片上直接改的参数（args 里的键），同意时把改过的一起发回去
  edited?: string[]     // 同意时改过的
}
export type Stream = { sid: string; text: string; thinking: string; started: number; bot_id: number }
// 模型服务：一个兼容 OpenAI 的接口地址加一个密钥。source：config.yaml 里写的，还是界面上加的
export type Provider = {
  name: string; title: string; preset: string; base_url: string; source: 'config' | 'ui'
  key_env: string; key_source: 'vault' | 'env' | ''; key_hint: string; needs_key: boolean
}
export type Preset = {
  id: string; title: string; base_url: string; color: string; desc: string; key_url?: string; embedding?: string; no_key?: boolean; note?: string
  models?: string[]   // 推荐的对话模型，前面的优先
}
// 单价是元 / 百万 token。source：config（config.yaml 手写）、models.dev（自动获取）、local（本机，免费）
export type ModelInfo = {
  source: '' | 'config' | 'models.dev' | 'local'; input?: number; output?: number; cache?: number | null
  offpeak?: [number, number]; vision?: boolean; kind?: string; name?: string; via?: string
}
export type ModelItem = { id: string; kind: string; vision?: boolean; input?: number; output?: number }
export type ModelList = { name: string; ok: boolean; error: string; note: string; models: string[]; items: ModelItem[]; ts: number }
export type Role = { provider: string; model: string; thinking?: boolean; reasoning_effort?: string }
export type Settings = {
  providers: Provider[]
  presets: Preset[]
  roles: Record<string, Role>
  limits: Record<string, number>
  heavy_workers: number
  computer_parallel: number
  computer_backend: string                              // auto / host / local / instabox
  computer_backends: { id: string; ok: boolean; locked?: boolean }[]   // 这台机器上能用哪些；locked：服务器锁不让用
  computer_on_desktop: boolean                          // 服务是桌面 App 自带的（本地模式）；否则“本机”指服务器那台机器
  computer_host_shell: boolean
  embedding: { provider: string; model: string }
  image_model: { provider: string; model: string }   // 画图用哪个模型（对话里画图、生成头像共用），空的是用排在最前面的
  model_info: Record<string, ModelInfo>   // 键是 服务/模型
  prices: { source: string; updated: number; error: string; rate: number; currency: string; models: number }
  problem: string                         // 对话或后台任务的模型没法用的原因；能用是空
  language: 'zh' | 'en'                   // 界面语言
  // 服务器锁（local_exec 为 false：网页上不能让服务器执行任意命令）和演示模式，只能在服务器的 config.yaml 里改。旧版服务没有这一项
  // exec / plugins / apps：策略表（osa/security.py），没写新键时按 local_exec / demo 推出来
  security?: { local_exec: boolean; demo: boolean; exec?: 'host' | 'sandbox' | 'off'; plugins?: 'any' | 'market' | 'off'; apps?: 'any' | 'market' }
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
  files: Record<string, string>   // 技能带的文件：相对路径 → 文字（scripts/、references/）
  apps: string[] | null           // frontmatter 里声明的应用；null 是没声明（按做法里的 @名字 找）
}
export type SkillDraft = {
  name: string; description: string; body: string; exists: boolean
  apps: { name: string; title: string; connected: boolean; in_market: boolean }[]
  declared_apps: string[] | null; files: Record<string, string>; skipped: string[]; warning?: string
}
export type MarketSkill = { id: string; name: string; description: string; body: string; files: string[]; apps: string[]; installed: boolean }
export type EventTrigger = {
  kind: 'event'; source: string; params: Record<string, any>; every?: number
  include: string[]; exclude: string[]; llm_filter: string; key?: string; path?: string
}
export type Trigger =
  | { kind: 'once'; at: string }
  | { kind: 'daily'; times: string[]; days: number[] }
  | { kind: 'monthly'; month_days: number[]; times: string[] }
  | { kind: 'every'; minutes: number }
  | EventTrigger
export type RoutineRun = {
  id: number; ts: number; source: string; status: string; task_id: number | null; detail: string; finished: number | null
}
// 事件的状态：要轮询的上次什么时候看的、出没出错；排队等处理的条数；push 的地址
export type EventState = {
  checked: number | null; error: string; fails: number; has_sample: boolean; pending: number
  mode: 'push' | 'poll' | 'internal' | ''; hook: string; missing: boolean
}
export type Routine = {
  id: number; bot_id: number; name: string; prompt: string; skill: string; trigger: Trigger; enabled: boolean
  next_run: number | null; running: boolean; last_run: RoutineRun | null
  category: 'schedule' | 'event'; event?: EventState; note?: string
  describe?: string   // 触发方式的一句话（服务端按界面语言写好；事件的来源说明只有服务端知道）
}
export type SourceField = {
  key: string; label: string; type: 'text' | 'textarea' | 'url' | 'number' | 'select' | 'multi' | 'bool' | 'secret' | 'list'
  required?: boolean; placeholder?: string; default?: any; help?: string; min?: number; max?: number
  options?: { value: string; label: string }[]
}
export type Scenario = {
  source: string; title: string; description: string; name: string; params: Record<string, any>; prompt: string
  include: string[]; exclude: string[]; llm_filter: string; every?: number
}
export type EventSource = {
  id: string; title: string; description: string; mode: 'push' | 'poll' | 'internal'; category: string; icon: string
  fields: SourceField[]; every: number; min_every: number; setup: string; plugin: string; unavailable: string
  scenarios: Scenario[]
}
export type Memory = {
  id: number; bot_id: number | null; shared: boolean; kind: 'note' | 'work'; text: string; source: string; created: number; updated: number
}
export type MemoryList = { shared: Memory[]; own: Memory[]; work: Memory[] }
export type Page = {
  id: number; bot_id: number | null; title: string; format: 'markdown' | 'html'; share_token: string | null; pinned: boolean
  task_id: number | null; created: number; updated: number; size: number; content?: string
  app?: { tools: string[]; chat: boolean } | null   // 小应用的清单（见 osa/applets.py），普通页面没有
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
