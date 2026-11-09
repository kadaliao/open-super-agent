import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, approve, chatById, loadOlder, markThreadSeen, openChat, openChatOrThread, openLibrary, openThread, pinChat, showTask, useStore } from '../store'
import type { Approval, Bot, Chat as ChatT, Message, Task, ThreadInfo } from '../types'
import { plain } from '../plain'
import BotAvatar from './BotAvatar'
import Composer from './Composer'
import Markdown from './Markdown'
import { locale, t, tn } from '../i18n'
import { ask } from './Dialog'
import { Icon } from '../icons'
import { Attachments, mediaUrl, splitMarks, type ViewerItem } from './Media'
import { imagePrice, setAsAvatar } from './AvatarTools'
import { LinkEmbed, Linkify, textEmbeds } from './Embeds'
import { cardFor, registerCard, type CardProps } from '../cards'
import { McpAppView } from './Applet'
import type { AppView } from '../mcpapp'

const time = (ts: number) => new Date(ts * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
const VIA: Record<string, string> = {
  telegram: t('来自 Telegram'), feishu: t('来自飞书'), dingtalk: t('来自钉钉'), wecom: t('来自企业微信'), discord: t('来自 Discord'),
}
const secs = (s: number) => (s < 60 ? t('{n} 秒', { n: Math.round(s) }) : t('{m} 分 {s} 秒', { m: Math.floor(s / 60), s: Math.round(s % 60) }))

// 旧版本的任务记录没有 kind，从文字里推断
function taskKind(m: Message): { kind: string; taskId?: number } {
  if (m.meta.kind) return { kind: m.meta.kind, taskId: m.meta.task_id ?? (m.meta.kind === 'dispatch' ? m.id : undefined) }
  if (m.content.startsWith('[任务已派发]')) return { kind: 'dispatch', taskId: m.id }   // i18n-ignore：存在消息里的标记
  const r = m.content.match(/^\[任务 #(\d+) 结果\]/)   // i18n-ignore：存在消息里的标记
  if (r) return { kind: 'result', taskId: Number(r[1]) }
  if (/^\[任务 #\d+ 已取消/.test(m.content)) return { kind: 'cancel' }   // i18n-ignore：存在消息里的标记
  if (m.content.startsWith('[触发器')) return { kind: 'trigger' }   // i18n-ignore：存在消息里的标记
  return { kind: 'note' }
}

function Thinking({ text, seconds, live }: { text: string; seconds?: number; live?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  return (
    <div className={'thinking' + (open ? ' open' : '')}>
      <button onClick={() => setOpen(!open)}>
        <span className={live ? 'pulse' : ''}>{live ? t('正在思考') : t('思考了 {time}', { time: secs(seconds || 0) })}</span>
        <span className="chev">›</span>
      </button>
      {open && <div className="thinking-body">{text}</div>}
    </div>
  )
}

function CopyBtn({ text }: { text: string }) {
  const [ok, setOk] = useState(false)
  return <button className="ghost-btn" onClick={() => { navigator.clipboard?.writeText(text); setOk(true); setTimeout(() => setOk(false), 1200) }}>{ok ? t('已复制') : t('复制')}</button>
}

// 群聊里每条消息上面标出是哪个 Bot 说的
const Speaker = ({ bot, show }: { bot?: Bot; show?: boolean }) => show ? <div className="speaker">{bot?.name || t('已删除的 Bot')}</div> : null

// 针对这条消息回复：在右边打开它下面的讨论串。reply 为空（讨论串里的消息）时不显示
type Reply = { chat: number } | undefined
const ReplyBtn = ({ m, reply }: { m: Message; reply: Reply }) =>
  reply ? <button className="ghost-btn" title={t('针对这条消息回复，在旁边单独讨论')} onClick={() => openThread(reply.chat, m.id)}>{t('回复')}</button> : null

// 消息下面的“N 条回复”：谁回复过、最后一条什么时候
function ThreadLink({ m, reply }: { m: Message; reply: Reply }) {
  const bots = useStore(s => s.bots)
  const open = useStore(s => !!reply && s.thread?.chat === reply.chat && s.thread.rootId === m.id)
  const fresh = useStore(s => (m.thread ? s.threads[m.thread.id]?.unread : 0) || 0)   // 还没看过的回复
  if (!reply || !m.thread?.count) return null
  const who = m.thread.bots.map(id => bots.find(b => b.id === id)).filter((b): b is Bot => !!b)
  return (
    <button className={'th-link' + (open ? ' on' : '') + (fresh ? ' new' : '')} onClick={() => openThread(reply.chat, m.id)}>
      {who.length > 0 && <GroupAvatars bots={who} size={18} />}
      <b>{t('{n} 条回复', { n: m.thread.count })}</b>
      {fresh > 0 ? <span className="th-new">{t('{n} 条新回复', { n: fresh })}</span>
        : m.thread.last && <span className="muted">{t('最后回复 {time}', { time: time(m.thread.last) })}</span>}
    </button>
  )
}

// ---------- 讨论串的新回复 ----------
// 讨论串在右边打开、回复只在里面，被回复的消息一往上滚就看不到“N 条回复”了。
// 所以有新回复时对话底部浮一条提醒，点了直接打开；对话顶上的“讨论串”列出这个对话的全部讨论串。

// 这个对话下面有新回复、又没正开着的讨论串，最近回复的在前
const freshIn = (threads: Record<number, ThreadInfo>, chatId: number, open?: number | null) =>
  Object.values(threads).filter(x => x.parent === chatId && x.unread > 0 && x.id !== open)
    .sort((a, b) => (b.last || 0) - (a.last || 0))

// 说话的人：你，或者某个 Bot（私聊里没记 bot_id 的就是这个私聊的 Bot）
function speakerName(role: string | undefined, botId: number | null | undefined, chat: ChatT, bots: Bot[]) {
  if (role === 'user') return t('你')
  const b = bots.find(x => x.id === (botId ?? (chat.kind === 'direct' ? chat.bot_ids[0] : null)))
  return b?.name || 'Bot'
}

// 对话底部的提醒：一个讨论串有新回复时显示谁回了什么，点了打开它；好几个时点了打开讨论串列表
function NewReplies({ chat, onList }: { chat: ChatT; onList: () => void }) {
  const threads = useStore(s => s.threads)
  const open = useStore(s => s.thread?.id)
  const bots = useStore(s => s.bots)
  const fresh = freshIn(threads, chat.id, open)
  if (!fresh.length) return null
  const one = fresh.length === 1 ? fresh[0] : null
  const n = fresh.reduce((a, x) => a + x.unread, 0)
  const faces = [...new Set(fresh.flatMap(x => x.preview_bot ? [x.preview_bot] : x.bots))]
    .map(id => bots.find(b => b.id === id)).filter((b): b is Bot => !!b)
  return (
    <div className="new-replies">
      <button className="nr-main" onClick={() => one ? openThread(chat.id, one.root) : onList()}>
        {faces.length > 0 && <GroupAvatars bots={faces} size={20} />}
        <span className="nr-text">
          {one ? tn('{who} 在讨论串里回复了', { who: <b>{speakerName('assistant', one.preview_bot, chat, bots)}</b> })
            : t('{n} 个讨论串有新回复', { n: fresh.length })}
          {one?.preview && <span className="nr-preview">{plain(one.preview)}</span>}
        </span>
        {n > 1 && <span className="badge">{n}</span>}
        <span className="nr-go">{one ? t('查看') : t('列表')}</span>
      </button>
      <button className="nr-x" title={t('都看过了')} onClick={() => fresh.forEach(x => markThreadSeen(x.id))}><Icon name="x" size={14} /></button>
    </div>
  )
}

// 对话顶上的“讨论串”：这个对话的全部讨论串，最近有回复的在前，有新回复的标出来
function ThreadsMenu({ chat, open, setOpen }: { chat: ChatT; open: boolean; setOpen: (v: boolean) => void }) {
  const threads = useStore(s => s.threads)
  const current = useStore(s => s.thread?.id)
  const bots = useStore(s => s.bots)
  const [list, setList] = useState<ThreadInfo[] | null>(null)
  const box = useRef<HTMLDivElement>(null)
  const fresh = freshIn(threads, chat.id)
  const n = fresh.reduce((a, x) => a + x.unread, 0)
  useEffect(() => {
    if (!open) return
    let live = true
    setList(null)
    api<ThreadInfo[]>(`/api/chats/${chat.id}/threads`).then(r => { if (live) setList(r) }).catch(() => { if (live) setList([]) })
    const away = (e: PointerEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); setOpen(false) } }
    window.addEventListener('pointerdown', away)
    window.addEventListener('keydown', esc)
    return () => { live = false; window.removeEventListener('pointerdown', away); window.removeEventListener('keydown', esc) }
  }, [open, chat.id])
  useEffect(() => { setOpen(false) }, [chat.id])
  if (!chat.threads && !fresh.length) return null
  return (
    <div className="th-menu-wrap" ref={box}>
      <button className={'icon-btn' + (open ? ' open' : '')} title={t('讨论串')} onClick={() => setOpen(!open)}>
        <Icon name="threads" size={17} />{n > 0 && <span className="badge dot">{n}</span>}
      </button>
      {open && (
        <div className="menu th-menu">
          <div className="th-menu-head"><b>{t('讨论串')}</b>{n > 0 && <button className="ghost-btn" onClick={() => fresh.forEach(x => markThreadSeen(x.id))}>{t('都看过了')}</button>}</div>
          <div className="th-menu-list">
            {list === null ? <div className="muted small th-menu-empty">{t('加载中…')}</div>
              : list.length === 0 ? <div className="muted small th-menu-empty">{t('还没有讨论串。点消息下面的“回复”，就能针对它单独讨论。')}</div>
              : list.map(x => {
                const y = { ...x, ...threads[x.id] }   // 列表拉下来以后又来的回复、刚看过的
                const who = y.bots.map(id => bots.find(b => b.id === id)).filter((b): b is Bot => !!b)
                return (
                  <button key={x.id} className={'th-row' + (y.unread ? ' new' : '') + (x.id === current ? ' on' : '')}
                          onClick={() => { setOpen(false); openThread(chat.id, x.root) }}>
                    <div className="th-row-root"><span className="muted">{speakerName(x.root_role, x.root_bot, chat, bots)}：</span>{plain(x.root_text || '') || '…'}</div>
                    {y.preview && <div className="th-row-last">{who.length > 0 && <GroupAvatars bots={who} size={16} />}<span>{plain(y.preview)}</span></div>}
                    <div className="th-row-meta">
                      <span>{t('{n} 条回复', { n: y.count })}{y.last ? ' · ' + when(y.last) : ''}</span>
                      {y.unread > 0 && <span className="th-new">{t('{n} 条新回复', { n: y.unread })}</span>}
                    </div>
                  </button>
                )
              })}
          </div>
        </div>
      )}
    </div>
  )
}

// 今天的只写时间，更早的写日期
const when = (ts: number) => new Date(ts * 1000).toDateString() === new Date().toDateString()
  ? time(ts) : new Date(ts * 1000).toLocaleDateString(locale, { month: 'numeric', day: 'numeric' })

function BotMessage({ bot, m, children, showName, reply }: { bot?: Bot; m: Message; children?: React.ReactNode; showName?: boolean; reply?: Reply }) {
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Speaker bot={bot} show={showName} />
        <Thinking text={m.meta.thinking} seconds={m.meta.thought_s} />
        <Markdown text={m.content} embeds />
        <Attachments items={m.meta.attachments} />
        {children}
        <ThreadLink m={m} reply={reply} />
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={m.content} /><ReplyBtn m={m} reply={reply} /></div>
      </div>
    </div>
  )
}

// generate_image 画好的图（kind=image）：图片、描述、模型和价格；可以下载、设为头像
function ImageMessage({ bot, m, chat, showName, reply }: { bot?: Bot; m: Message; chat: ChatT; showName?: boolean; reply?: Reply }) {
  const cur = useStore(s => s.settings?.prices.currency) || '¥'
  const [done, setDone] = useState('')
  const att = (m.meta.attachments || [])[0]
  const avatar = async (target: Parameters<typeof setAsAvatar>[1], label: string) => {
    if (!att?.media) return
    try { await setAsAvatar(mediaUrl(att.media), target); setDone(label) } catch (e: any) { setDone(e.message) }
  }
  const actions: ViewerItem['actions'] = [
    ...(bot ? [{ label: t('设为 {name} 的头像', { name: bot.name }), run: () => avatar({ bot }, t('已设为 {name} 的头像', { name: bot.name })) }] : []),
    ...(chat.kind === 'group' ? [{ label: t('设为群头像'), run: () => avatar({ chat: chat.id }, t('已设为群头像')) }] : []),
  ]
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Speaker bot={bot} show={showName} />
        <Attachments items={m.meta.attachments} actions={() => actions} />
        <div className="img-caption" title={m.meta.prompt}>🎨 {m.meta.prompt}</div>
        <div className="img-meta">{m.meta.model}{m.meta.price !== undefined ? ' · ' + imagePrice(m.meta.price, cur) : ''}{done && <> · <b>{done}</b></>}</div>
        <ThreadLink m={m} reply={reply} />
        <div className="msg-actions"><span>{time(m.ts)}</span>
          {att?.media && <a className="ghost-btn" href={mediaUrl(att.media) + '?download=1'} download={att.name}>{t('下载')}</a>}
          {actions.map(a => <button key={a.label} className="ghost-btn" onClick={a.run}>{a.label}</button>)}
          <CopyBtn text={m.meta.prompt || ''} /><ReplyBtn m={m} reply={reply} /></div>
      </div>
    </div>
  )
}

function TaskCard({ m, task, owner, handoffTo, replyTo }: { m: Message; task?: Task; owner?: Bot; handoffTo?: Bot; replyTo?: Bot }) {
  const status = task?.status || m.meta.status || 'done'
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (status !== 'running') return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [status])
  const goal = m.content.replace(/^\[任务已派发\]\s*/, '')   // i18n-ignore：存在消息里的标记
  const label: Record<string, string> = { running: task?.waiting_approval ? t('等你确认') : t('进行中'), done: t('已完成'), failed: t('失败'), cancelled: t('已取消'), interrupted: t('已中断') }
  const heavy = m.meta.mode === 'heavy'
  const name = owner ? (heavy ? t('{name} 的Heavy 任务', { name: owner.name }) : t('{name} 的后台任务', { name: owner.name })) : heavy ? t('Heavy 任务') : t('后台任务')
  return (
    <div className={`task-card ${status}${task?.waiting_approval ? ' waiting' : ''}`}>
      <div className="task-top">
        <span className="dot" />
        <b>{name} #{m.id}</b>
        <span className="muted">{label[status] || status}{status === 'running' && task ? ` · ${secs((now / 1000) - task.started)}` : ''}{task?.steps ? ` · ${t('{n} 步', { n: task.steps })}` : ''}</span>
        <button className="ghost-btn" title={t('看这个任务每一步做了什么')} onClick={() => showTask(m.id)}>{t('过程')}</button>
        {status === 'running' && <button className="ghost-btn" onClick={() => api(`/api/tasks/${m.id}/cancel`, { method: 'POST' })}>{t('停止')}</button>}
      </div>
      <div className="task-goal">{goal}</div>
      {handoffTo && <div className="task-action">{t('做完交给 {name}', { name: handoffTo.name })}</div>}
      {replyTo && <div className="task-action">{t('做完把结果回给 {name}', { name: replyTo.name })}</div>}
      {status === 'running' && task?.last_action && <div className="task-action">→ {task.last_action}</div>}
    </div>
  )
}

// 任务结果存进资料库，标题取任务描述的第一行
function SavePageBtn({ bot, text, goal, taskId }: { bot?: Bot; text: string; goal: string; taskId?: number }) {
  const [saved, setSaved] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  if (saved) return <button className="ghost-btn" onClick={() => openLibrary(saved)}>{t('已存为页面 · 打开')}</button>
  const save = async () => {
    setBusy(true)
    try {
      const h1 = text.match(/^#\s+(.+)$/m)?.[1]
      const title = (h1 || goal.split('\n').find(l => l.trim()) || t('任务 #{id} 的结果', { id: taskId ?? '' })).replace(/^\[(定时任务|事件) [^\]]+\]\s*/, '').slice(0, 60)   // i18n-ignore：去掉存在消息里的标记
      const p = await api('/api/pages', { method: 'POST', body: { bot_id: bot?.id, title, content: text, task_id: taskId } })
      setSaved(p.id)
    } finally { setBusy(false) }
  }
  return <button className="ghost-btn" disabled={busy} onClick={save}>{t('存为页面')}</button>
}

function ResultMessage({ bot, m, goal, showName, reply }: { bot?: Bot; m: Message; goal: string; showName?: boolean; reply?: Reply }) {
  const text = m.content.replace(/^\[任务 #\d+ 结果\]\n?/, '')   // i18n-ignore：存在消息里的标记
  const [open, setOpen] = useState(text.length < 1800)
  const status = m.meta.status || 'done'
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <Speaker bot={bot} show={showName} />
        <div className={`result-head ${status}`}>
          {status === 'failed' ? t('任务失败') : t('任务完成')} · #{taskKind(m).taskId}{m.meta.elapsed ? ` · ${t('用时 {time}', { time: secs(m.meta.elapsed) })}` : ''}
        </div>
        <div className={open ? '' : 'clamp'}><Markdown text={text} embeds /></div>
        {!open && <button className="expand" onClick={() => setOpen(true)}>{t('展开全文')}</button>}
        <Attachments items={m.meta.attachments} />
        {(m.meta.apps as AppView[] | undefined)?.map((v, i) => <McpAppView key={i} view={v} botId={m.bot_id} />)}
        <ThreadLink m={m} reply={reply} />
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={text} />
          {status === 'done' && <SavePageBtn bot={bot} text={text} goal={goal} taskId={taskKind(m).taskId} />}
          {taskKind(m).taskId && <button className="ghost-btn" onClick={() => showTask(taskKind(m).taskId!)}>{t('查看过程')}</button>}
          <ReplyBtn m={m} reply={reply} /></div>
      </div>
    </div>
  )
}

// 群里派的任务：卡片在私聊里，注明来自哪个群
function Origin({ a }: { a: Approval }) {
  const g = useStore(s => (a.origin_chat_id && a.origin_chat_id !== a.chat_id ? chatById(s, a.origin_chat_id) : undefined))
  return g ? <> · {tn('在群聊「{chat}」里派的', { chat: <button className="link-btn" onClick={() => openChat(g.id)}>{g.title}</button> })}</> : null
}

function HelpCard({ a }: { a: Approval }) {
  const done = a.status && a.status !== 'pending'
  let reason = ''
  try { reason = JSON.parse(a.args)['需要你做的事'] } catch { reason = a.args }   // i18n-ignore：工具参数的键
  if (done) return <div className="sys-line">{a.status === 'approved' ? '✓ ' + t('你已处理完，Bot 继续') : '✕ ' + t('没有处理')}{t('（任务 #{id}）', { id: a.task_id })}</div>
  return (
    <div className="approval-card help">
      <div className="approval-title">🙋 {t('需要你帮忙')}</div>
      <div className="help-reason">{reason}</div>
      <div className="muted small">{tn('任务 #{id} 已暂停{origin}。接手电脑处理完后点“交还”，Bot 会接着做。', { id: a.task_id, origin: <Origin a={a} /> })}</div>
      <div className="approval-actions">
        <button className="btn" onClick={() => approve(a.id, false, '用户表示处理不了')}>{t('我处理不了')}</button>{/* i18n-ignore：拒绝理由是给模型看的 */}
        <span className="grow" />
        <button className="btn" onClick={() => window.dispatchEvent(new CustomEvent('osa-open-computer', { detail: { botId: a.bot_id, takeover: true, taskId: a.task_id } }))}>{t('接手电脑')}</button>
        <button className="btn primary" onClick={() => api(`/api/bots/${a.bot_id}/computer/handback`, { method: 'POST' })}>{t('处理完了，交还')}</button>
      </div>
    </div>
  )
}

// 画图前的确认（generate_image）：参数是 {描述, 模型, 价格}
function DrawCard({ a }: { a: Approval }) {
  let d: Record<string, string> = {}
  try { d = JSON.parse(a.args) } catch { /* 显示原文 */ }
  const bot = useStore(s => s.bots.find(b => b.id === a.bot_id))
  if (a.status && a.status !== 'pending') return <div className="sys-line">{a.status === 'approved' ? '✓ ' + t('已允许画图') + (a.always ? t('，以后不再询问') : '')
    : a.status === 'cancelled' ? '✕ ' + t('画图已取消') : '✕ ' + t('没画')}</div>
  return (
    <div className="approval-card draw">
      <div className="approval-title">🎨 {t('{name} 想画一张图', { name: bot?.name || 'Bot' })}<Origin a={a} /></div>
      <div className="draw-prompt">{d['描述'] ?? a.args}</div>{/* i18n-ignore：工具参数的键 */}
      <div className="muted small">{t('模型：{model}', { model: d['模型'] || '' })} · {t('价格：{price}', { price: d['价格'] || '' })}</div>{/* i18n-ignore：工具参数的键 */}
      <div className="approval-actions">
        <span className="grow" />
        <button className="btn" onClick={() => approve(a.id, false)}>{t('不画了')}</button>
        <button className="btn" onClick={() => approve(a.id, true)}>{t('画吧')}</button>
        <button className="btn primary" title={t('以后画图不再询问，可在设置 → 确认规则里撤销')} onClick={() => approve(a.id, true, '', true)}>{t('总是允许')}</button>
      </div>
    </div>
  )
}

// 新建、修改、删除 Bot（create_bot / update_bot / delete_bot，见 botadmin.py）：每次都要确认，没有“总是允许”。
// 参数的键是中文：新建 {名字, 职责, 技能, 头像}；修改 {Bot, 编号, 新名字, 职责, 职责改动, 头像, 新技能, 改技能, 删技能}；
// 删除 {Bot, 编号, 原因, 会删掉, 群聊, 在做的任务}；别的 Bot 托它做的还有 请求方
type BotCardArgs = Record<string, any>
type Named = (name: string) => string
const BOT_DONE: Record<string, [Named, Named]> = {   // 卡片有了结果：(同意了, 没同意)
  create_bot: [name => t('已同意新建 Bot「{name}」', { name }), name => t('没新建 Bot「{name}」', { name })],
  update_bot: [name => t('已同意修改 Bot「{name}」', { name }), name => t('没修改 Bot「{name}」', { name })],
  delete_bot: [name => t('已同意删除 Bot「{name}」', { name }), name => t('没删除 Bot「{name}」', { name })],
}

function BotAdminCard({ a }: { a: Approval }) {
  let d: BotCardArgs = {}
  try { d = JSON.parse(a.args) } catch { /* 显示原文 */ }
  const bot = useStore(s => s.bots.find(b => b.id === a.bot_id))
  const target = useStore(s => s.bots.find(b => b.id === d['编号']))   // i18n-ignore：工具参数的键
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState<string | null>(null)   // 正在改的职责全文；null 是没在改
  const name: string = d['名字'] || d['Bot'] || ''   // i18n-ignore：工具参数的键
  const who = bot?.name || 'Bot'
  if (a.status && a.status !== 'pending') {
    const [yes, no] = BOT_DONE[a.tool]
    return <div className="sys-line">{a.status === 'approved' ? '✓ ' + yes(name) + (a.edited?.length ? t('（职责按你改的）') : '') : '✕ ' + no(name)}</div>
  }
  const role: string = d['职责'] ?? ''   // i18n-ignore：工具参数的键
  const canEdit = !!a.editable?.includes('职责')   // i18n-ignore：工具参数的键
  const changed = draft !== null && !!draft.trim() && draft.trim() !== role.trim()
  // 职责上面那行：说明 + “编辑 / 还原”
  const roleHead = (label: string) => (
    <div className="botadmin-label">
      <span className="muted small">{draft !== null ? t('职责（全文，改好后点下面的按钮）') : label}</span><span className="grow" />
      {canEdit && (draft === null
        ? <button className="link-btn small" onClick={() => setDraft(role)}>✎ {t('编辑')}</button>
        : <button className="link-btn small" onClick={() => setDraft(null)}>{t('还原')}</button>)}
    </div>
  )
  const roleEditor = draft !== null && (
    <textarea className="newbot-role edit" value={draft} maxLength={4000} autoFocus onChange={e => setDraft(e.target.value)}
      rows={Math.min(14, Math.max(4, draft.split('\n').length + Math.ceil(draft.length / 40)))} />
  )
  const lost = d['会删掉'] || {}   // i18n-ignore：工具参数的键
  const what = [lost['消息'] ? t('私聊里的 {n} 条消息', { n: lost['消息'] }) : '', lost['技能'] ? t('{n} 个技能', { n: lost['技能'] }) : '',   // i18n-ignore：工具参数的键
    lost['定时任务和事件'] ?? lost['定时任务'] ? t('{n} 个定时任务和事件', { n: lost['定时任务和事件'] ?? lost['定时任务'] }) : '', lost['记忆'] ? t('它自己的 {n} 条记忆', { n: lost['记忆'] }) : ''].filter(Boolean)   // i18n-ignore：工具参数的键
  const skillRows: [string, string[]][] = [[t('新技能：'), d['新技能']], [t('改技能：'), d['改技能']], [t('删技能：'), d['删技能']]]   // i18n-ignore：工具参数的键
  const actions: Record<string, [string, string]> = { create_bot: [t('不建了'), t('新建')], update_bot: [t('不改了'), t('修改')], delete_bot: [t('不删了'), t('删除')] }
  const [no, yes] = actions[a.tool]
  return (
    <div className={'approval-card botadmin' + (a.tool === 'delete_bot' ? ' danger' : '')}>
      <div className="approval-title">
        {a.tool === 'create_bot' ? '🤖 ' + t('{name} 想新建一个 Bot：{bot}', { name: who, bot: name })
          : a.tool === 'update_bot' ? '✏️ ' + t('{name} 想修改 Bot「{bot}」', { name: who, bot: name })
          : '🗑 ' + t('{name} 想删除 Bot「{bot}」', { name: who, bot: name })}<Origin a={a} />
      </div>
      {d['请求方'] && <div className="muted small">{t('{name} 私信请它做的', { name: d['请求方'] })}</div>}{/* i18n-ignore：工具参数的键 */}
      {a.tool === 'create_bot' && <>
        {d['头像'] && <div className="botadmin-row"><span className="muted">{t('头像')}</span><BotAvatar avatar={d['头像']} size={22} /></div>}{/* i18n-ignore：工具参数的键 */}
        {roleHead(t('职责'))}
        {roleEditor || <div className="newbot-role">{d['职责'] ?? a.args}</div>}{/* i18n-ignore：工具参数的键 */}
        {!!d['技能']?.length && <div className="muted small">{t('一起带上的技能：')}{d['技能'].join(' · ')}</div>}{/* i18n-ignore：工具参数的键 */}
        <div className="muted small">{t('建好后可以再让主 Bot 改，也能在它私聊的右栏里改')}</div>
      </>}
      {a.tool === 'update_bot' && <>
        {d['新名字'] && <div className="botadmin-row"><span className="muted">{t('名字')}</span><span>{name} → <b>{d['新名字']}</b></span></div>}{/* i18n-ignore：工具参数的键 */}
        {d['头像'] && <div className="botadmin-row"><span className="muted">{t('头像')}</span>{/* i18n-ignore：工具参数的键 */}
          {target && <BotAvatar avatar={target.avatar} size={22} />}→<BotAvatar avatar={d['头像']} size={22} />{/* i18n-ignore：工具参数的键 */}
          {d['不再用图片头像'] && <span className="muted small">{t('不再用原来的图片')}</span>}</div>}{/* i18n-ignore：工具参数的键 */}
        {d['职责改动'] && <>{/* i18n-ignore：工具参数的键 */}
          {roleHead(t('职责：划掉的是删掉的，标绿的是加上的'))}
          {roleEditor || <div className="newbot-role diff">{(d['职责改动'] as [number, string][]).map(([sign, text], i) =>   // i18n-ignore：工具参数的键
            sign < 0 ? <del key={i}>{text}</del> : sign > 0 ? <ins key={i}>{text}</ins> : <span key={i}>{text}</span>)}</div>}
        </>}
        {skillRows.filter(([, xs]) => xs?.length).map(([label, xs]) =>
          <div key={label} className="small">{label}{xs.map(x => <div key={x} className="muted">{x}</div>)}</div>)}
      </>}
      {a.tool === 'delete_bot' && <>
        {target && <div className="botadmin-row"><BotAvatar avatar={target.avatar} size={22} /><b>{target.name}</b></div>}
        {d['原因'] && <div className="newbot-role">{d['原因']}</div>}{/* i18n-ignore：工具参数的键 */}
        <div className="small">{what.length ? t('会一起删掉：{what}', { what: what.join(t('、')) }) : t('它还没有聊天记录、技能、定时任务、事件和记忆')}</div>
        {!!d['群聊']?.length && <div className="small">{t('会退出群聊：{groups}', { groups: d['群聊'].map((g: string) => `「${g}」`).join(t('、')) })}</div>}{/* i18n-ignore：工具参数的键 */}
        {!!d['在做的任务'] && <div className="small">{t('正在做的 {n} 个任务会停掉', { n: d['在做的任务'] })}</div>}{/* i18n-ignore：工具参数的键 */}
        <div className="muted small">{t('删了不能恢复。')}</div>
      </>}
      <div className="approval-actions">
        <input value={note} onChange={e => setNote(e.target.value)} placeholder={t('不同意的话附一句意见，它会按意见改了重提')} />
        <button className="btn" onClick={() => approve(a.id, false, note)}>{no}</button>
        <button className={'btn ' + (a.tool === 'delete_bot' ? 'danger' : 'primary')} disabled={draft !== null && !draft.trim()}
          onClick={() => approve(a.id, true, '', false, changed ? { '职责': draft!.trim() } : undefined)}>{yes}</button>{/* i18n-ignore：工具参数的键 */}
      </div>
    </div>
  )
}

// 本机电脑：任务第一次要操作整台电脑（真实的屏幕、鼠标键盘）时问一次，参数是 {任务}
function DesktopCard({ a }: { a: Approval }) {
  const bot = useStore(s => s.bots.find(b => b.id === a.bot_id))
  if (a.status && a.status !== 'pending') return <div className="sys-line">{a.status === 'approved' ? '✓ ' + t('已允许操作整台电脑') + (a.always ? t('，以后不再询问') : '')
    : a.status === 'cancelled' ? '✕ ' + t('任务已取消') : '✕ ' + t('没让 Bot 操作整台电脑')}</div>
  return (
    <div className="approval-card desk">
      <div className="approval-title">🖥 {t('{name} 想操作整台电脑', { name: bot?.name || 'Bot' })}<Origin a={a} /></div>
      <div className="muted small">{typeof a.task_id === 'number' ? t('来自任务 #{id}：{goal}', { id: a.task_id, goal: a.goal }) : a.goal}</div>
      <div className="small">{t('它会看你的屏幕，用你的鼠标键盘操作这台电脑上的 App。操作时你动一下鼠标或键盘它就停下；点“发送”“付款”这类按钮前还会再问你。')}</div>
      <div className="approval-actions">
        <span className="grow" />
        <button className="btn" onClick={() => approve(a.id, false)}>{t('不行')}</button>
        <button className="btn" onClick={() => approve(a.id, true)}>{t('这次允许')}</button>
        <button className="btn primary" title={t('以后任务操作整台电脑不再询问，可在设置 → 确认规则里撤销')} onClick={() => approve(a.id, true, '', true)}>{t('总是允许')}</button>
      </div>
    </div>
  )
}

function ApprovalCard({ a }: { a: Approval }) {
  if (a.kind === 'help') return <HelpCard a={a} />
  if (a.tool === 'generate_image') return <DrawCard a={a} />
  if (a.tool in BOT_DONE) return <BotAdminCard a={a} />
  if (a.tool === 'desktop') return <DesktopCard a={a} />
  const [note, setNote] = useState('')
  const [prefix, setPrefix] = useState(a.prefix || '')
  const done = a.status && a.status !== 'pending'
  const label: Record<string, string> = { approved: a.always ? t('已允许，以后自动允许') : a.scope === 'task' ? t('已允许，这个任务里不再询问') : t('已允许'), rejected: t('已拒绝'), expired: t('超时未确认，按拒绝处理'), cancelled: t('任务已取消') }
  if (done) return <div className="sys-line">{a.status === 'approved' ? '✓' : '✕'} {label[a.status!] || a.status} <code>{a.tool}</code>{t('（任务 #{id}）', { id: a.task_id })}</div>
  const [app, tool] = a.tool.includes('__') ? a.tool.split('__', 2) : ['', a.tool]
  const scopes = a.scopes ?? (a.once ? ['once'] : ['once', 'always'])   // 旧版服务没有 scopes
  const shell = a.tool === 'computer_shell' && a.prefix !== undefined   // 终端：“总是允许”的是一个命令前缀，可以改
  const okPrefix = !!prefix.trim() && !/^[*?[]|[;&|<>`$()]/.test(prefix.trim())   // 和 approvals._prefix_ok 一样
  return (
    <div className="approval-card">
      <div className="approval-title">{t('需要你确认')}{app && <> · {tn('应用 {app}', { app: <b>@{app}</b> })}</>} <code>{tool}</code></div>
      <div className="muted small">{tn('来自任务 #{id}{origin}：{goal}', { id: a.task_id, origin: <Origin a={a} />, goal: a.goal })}
        {a.expires ? ' · ' + t('后台任务，{time} 前没确认按拒绝处理', { time: new Date(a.expires * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' }) }) : ''}</div>
      <pre className="approval-args">{a.args}</pre>
      {shell && scopes.includes('always') && <div className="approval-prefix">
        <span className="muted small">{t('以这个开头的命令：')}</span>
        <input value={prefix} onChange={e => setPrefix(e.target.value)} spellCheck={false} aria-label={t('命令前缀')} />
        <button className="btn" disabled={!okPrefix} title={t('以后以“{prefix}”开头的命令不再询问（带 ; | $( 这类符号的除外），可在设置 → 确认规则里撤销', { prefix: prefix.trim() })}
          onClick={() => approve(a.id, true, note, true, undefined, 'always', prefix.trim())}>{t('总是允许这个前缀')}</button>
      </div>}
      {shell && a.compound && <div className="muted small">{t('这条命令带 ; | $( 这类符号，按前缀放行管不到它，下次还会问。')}</div>}
      <div className="approval-actions">
        <input value={note} onChange={e => setNote(e.target.value)} placeholder={t('附一句意见（可选）')} />
        <button className="btn" onClick={() => approve(a.id, false, note)}>{t('拒绝')}</button>
        <button className="btn" onClick={() => approve(a.id, true, note)}>{t('允许一次')}</button>
        {scopes.includes('task') && <button className={'btn' + (shell || !scopes.includes('always') ? ' primary' : '')} title={t('这个任务里 {tool} 不再询问，任务结束就失效', { tool: a.tool })}
          onClick={() => approve(a.id, true, note, false, undefined, 'task')}>{t('本次任务内允许')}</button>}
        {!shell && scopes.includes('always') && <button className="btn primary" title={t('以后 {tool} 不再询问，可在设置 → 确认规则里撤销', { tool: a.tool })} onClick={() => approve(a.id, true, note, true)}>{t('总是允许')}</button>}
      </div>
    </div>
  )
}

function HandoffLine({ from, to, text }: { from?: Bot; to?: Bot; text: string }) {
  return (
    <div className="handoff">
      {from && <BotAvatar avatar={from.avatar} size={18} />}<b>{from?.name || t('已删除的 Bot')}</b>
      <span className="muted">{t('交给')}</span>
      {to && <BotAvatar avatar={to.avatar} size={18} />}<b>{to?.name || t('已删除的 Bot')}</b>
      <span className="handoff-text">{text}</span>
    </div>
  )
}

// Bot 之间的私信：谁私信了谁、说了什么；点名字去看对方那边
function DmLine({ m, from, to, here }: { m: Message; from?: Bot; to?: Bot; here: number }) {
  const [open, setOpen] = useState(m.content.length < 240)
  // 在收信的私聊里，链接回发信的那个对话；在发信的对话里，链接到收信 Bot 的私聊
  const there = m.meta.chat && m.meta.chat !== here ? m.meta.chat : to && to.chat_id !== here ? to.chat_id : null
  return (
    <div className="handoff dm">
      <span className="dm-icon">✉</span>
      {from && <BotAvatar avatar={from.avatar} size={18} />}<b>{from?.name || t('已删除的 Bot')}</b>
      <span className="muted">{t('私信')}</span>
      {to && <BotAvatar avatar={to.avatar} size={18} />}<b>{to?.name || t('已删除的 Bot')}</b>
      {there && <button className="link-btn small" onClick={() => openChatOrThread(there)}>{m.meta.chat && m.meta.chat !== here ? t('去发信的对话') : t('去看 {name}', { name: to?.name || '' })}</button>}
      <span className={'handoff-text' + (open ? '' : ' clamp3')}>{m.content}</span>
      {!open && <button className="expand" onClick={() => setOpen(true)}>{t('展开')}</button>}
    </div>
  )
}

// 私信的回信：显示成发信 Bot 的一条消息，长的折起来
function DmReply({ m, bot, to, reply }: { m: Message; bot?: Bot; to?: Bot; reply?: Reply }) {
  const [open, setOpen] = useState(m.content.length < 1800)
  const bad = m.meta.status && m.meta.status !== 'done'
  return (
    <div className="msg bot">
      {bot && <BotAvatar avatar={bot.avatar} size={28} />}
      <div className="msg-body">
        <div className="speaker">{bot?.name || t('已删除的 Bot')}</div>
        <div className={'result-head' + (bad ? ' failed' : '')}>↩ {t('回了 {name} 的私信', { name: to?.name || t('已删除的 Bot') })}{m.meta.task_id ? ' · ' + t('任务 #{id}', { id: m.meta.task_id }) : ''}</div>
        <div className={open ? '' : 'clamp'}><Markdown text={m.content} embeds /></div>
        {!open && <button className="expand" onClick={() => setOpen(true)}>{t('展开全文')}</button>}
        <ThreadLink m={m} reply={reply} />
        <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={m.content} />
          {bot && <button className="ghost-btn" onClick={() => openChat(bot.chat_id)}>{t('去 {name} 的私聊', { name: bot.name })}</button>}
          {m.meta.task_id && <button className="ghost-btn" onClick={() => showTask(m.meta.task_id)}>{t('查看过程')}</button>}
          <ReplyBtn m={m} reply={reply} /></div>
      </div>
    </div>
  )
}

const STARTERS = [
  t('每天早上 9 点给我整理 AI 领域的重要新闻'),
  t('帮我调研一下自部署 SearxNG 的最佳实践'),
  t('你能做哪些事？先介绍一下你的能力'),
]

// 群聊的头像：有自己的图片用图片（size 大小），没有就用成员的头像拼（每个 stack 大小）
export function GroupFace({ image, bots, size, stack }: { image?: string | null; bots: Bot[]; size: number; stack?: number }) {
  return image ? <BotAvatar avatar={{ shape: 'circle', color: '', image }} size={size} /> : <GroupAvatars bots={bots} size={stack ?? size} />
}

export function GroupAvatars({ bots, size = 26 }: { bots: Bot[]; size?: number }) {
  return (
    <span className="group-avatars" style={{ width: size + (Math.min(bots.length, 3) - 1) * size * 0.45 }}>
      {bots.slice(0, 3).map((b, i) => <span key={b.id} style={{ left: i * size * 0.45, zIndex: 3 - i }}><BotAvatar avatar={b.avatar} size={size} /></span>)}
    </span>
  )
}

// 一串消息，主对话和讨论串共用。reply：能针对消息回复（主对话里给；讨论串里不给，不能再套讨论串）
export function MessageList({ chat, list, reply }: { chat: ChatT; list: Message[]; reply?: Reply }) {
  const allBots = useStore(s => s.bots)
  const tasks = useStore(s => s.tasks)
  const group = chat.kind === 'group'
  const botOf = (id: number | null | undefined) => allBots.find(b => b.id === id)
  const bot = group ? undefined : botOf(chat.bot_ids[0])
  const speaker = (m: Message) => (group ? botOf(m.bot_id) : bot)
  // 回复里记录了本轮派发的任务：任务卡片挂到回复下面，不再单独显示
  const byId = new Map(list.map(m => [m.id, m]))
  const attached = new Set(list.flatMap(m => (m.role === 'assistant' ? m.meta.tasks || [] : [])))
  const task = (tid: number) => byId.get(tid) && <TaskCard key={tid} m={byId.get(tid)!} task={tasks[tid]}
    owner={group ? botOf(byId.get(tid)!.bot_id) : undefined} handoffTo={botOf(byId.get(tid)!.meta.handoff_to)}
    replyTo={botOf(byId.get(tid)!.meta.reply_to)} />
  return <>{list.map(m => {
    if (m.role === 'user') {
      // “[附件] 路径”这几行不显示，换成缩略图和文件卡片
      const { text, items } = splitMarks(m.content, m.meta.attachments)
      return (
        <div key={m.id} className="msg user">
          <div className="user-col">
            {items.length > 0 && <Attachments items={items} />}
            {(text || !items.length) && <div className="user-line">{m.meta.via && <span className="via">{VIA[m.meta.via] || m.meta.via}</span>}
              {m.meta.applet?.app ? <span className="via">{t('来自应用 @{name} 的界面', { name: m.meta.applet.app })}</span>
                : m.meta.applet && <button className="via link-btn" onClick={() => openLibrary(m.meta.applet.id)}>{t('来自小应用「{title}」', { title: m.meta.applet.title })}</button>}
              <div className="bubble"><Linkify text={text} /></div></div>}
            {!text && items.length > 0 && m.meta.via && <span className="via">{VIA[m.meta.via] || m.meta.via}</span>}
            {text && textEmbeds(text).map(u => <div key={u} className="user-embed"><LinkEmbed href={u} fallback={null} /></div>)}
            <ThreadLink m={m} reply={reply} />
            {reply && <div className="msg-actions"><span>{time(m.ts)}</span><CopyBtn text={text || m.content} /><ReplyBtn m={m} reply={reply} /></div>}
          </div>
        </div>)
    }
    if (m.role === 'assistant') return m.content || m.meta.tasks?.length ? (
      <BotMessage key={m.id} bot={speaker(m)} m={m} showName={group} reply={reply}>
        {(m.meta.tasks || []).map(task)}
      </BotMessage>) : null
    const k = taskKind(m)
    const card = cardFor(k.kind)   // 按种类找卡片（cards.tsx）；没登记的种类显示成一行系统消息
    if (card) return <Fragment key={m.id}>{card({ m, kind: k.kind, taskId: k.taskId, chat, group, bot: speaker(m), botOf, reply, tasks, byId, attached, task })}</Fragment>
    return <div key={m.id} className="sys-line">{m.content.replace(/^\[|\]$/g, '').slice(0, 160)}</div>
  })}</>
}

// ---------- 内置的几种消息卡片 ----------
// 没有新内容的定时任务（跑完才知道）不显示，运行记录在右栏“定时”里
registerCard('dispatch', ({ m, attached, tasks, task }) => attached.has(m.id) || m.meta.quiet || tasks[m.id]?.quiet ? null : task(m.id))
registerCard('result', ({ m, bot, group, reply, taskId, byId }) => <ResultMessage bot={bot} m={m} showName={group} reply={reply}
  goal={(taskId && byId.get(taskId)?.content.replace(/^\[任务已派发\]\s*/, '')) || ''} />)   // i18n-ignore：存在消息里的标记
registerCard('image', ({ m, bot, chat, group, reply }) => <ImageMessage bot={bot} m={m} chat={chat} showName={group} reply={reply} />)
registerCard('handoff', ({ m, botOf }) => <HandoffLine from={botOf(m.bot_id)} to={botOf(m.meta.to)} text={m.meta.note || m.content} />)
registerCard('dm', ({ m, botOf, chat }) => <DmLine m={m} from={botOf(m.bot_id)} to={botOf(m.meta.to)} here={chat.id} />)
registerCard('dm_reply', ({ m, botOf, chat, reply }) => m.meta.sent
  ? <div className="sys-line">↩ {t('已把回信发给 {name}', { name: botOf(m.meta.to)?.name || t('已删除的 Bot') })}
      {m.meta.chat && m.meta.chat !== chat.id && <> · <button className="link-btn" onClick={() => openChatOrThread(m.meta.chat)}>{t('去看')}</button></>}</div>
  : <DmReply m={m} bot={botOf(m.bot_id)} to={botOf(m.meta.to)} reply={reply} />)
const botAdmin = ({ m, kind, botOf, chat }: CardProps) => {
  const nb = kind !== 'bot_deleted' ? botOf(m.meta.new_bot ?? m.meta.bot) : undefined
  return <div className="sys-line">🤖 {m.content.replace(/^\[|\]$/g, '')}
    {nb?.chat_id && nb.chat_id !== chat.id && <> · <button className="link-btn" onClick={() => openChat(nb.chat_id)}>
      {kind === 'bot_created' ? t('去和它聊') : t('去看看')}</button></>}</div>
}
registerCard('bot_created', botAdmin)
registerCard('bot_updated', botAdmin)
registerCard('bot_deleted', botAdmin)
// 插件以 Bot 的名义发进对话的消息（osa/services.py 的 messenger）：像 Bot 的回复，下面标着是哪个插件发的
registerCard('plugin', ({ m, bot, group, reply }) => (
  <BotMessage bot={bot} m={m} showName={group} reply={reply}>
    <div className="muted small">{t('来自插件「{name}」', { name: m.meta.plugin || '' })}</div>
  </BotMessage>))
registerCard('reset', () => <div className="topic-line"><span>{t('新话题 · 之前的对话不再带进上下文，长期记忆还在')}</span></div>)
registerCard('trigger', ({ m }) => <div className="sys-line">⚡ {m.content.replace(/^\[触发器 ([^\]]+)\]/, (_, name) => t('触发器 {name}：', { name })).slice(0, 160)}</div>)   // i18n-ignore：存在消息里的标记

// 正在流式输出的回复
export function StreamMessage({ chat, chatId }: { chat: ChatT; chatId: number }) {
  const stream = useStore(s => s.streams[chatId])
  const who = useStore(s => s.bots.find(b => b.id === (chat.kind === 'group' ? stream?.bot_id : chat.bot_ids[0])))
  if (!stream) return null
  return (
    <div className="msg bot">
      {who && <BotAvatar avatar={who.avatar} size={28} busy />}
      <div className="msg-body">
        <Speaker bot={who} show={chat.kind === 'group'} />
        <Thinking text={stream.thinking} live={!stream.text} seconds={(Date.now() - stream.started) / 1000} />
        {stream.text ? <Markdown text={stream.text + ' ▍'} /> : !stream.thinking && <div className="typing"><i /><i /><i /></div>}
      </div>
    </div>
  )
}

// 要你确认的卡片；群里派的任务卡片在 Bot 的私聊里，这里只放一条“去处理”。
// here：哪些卡片在这里显示；from：哪些是在这里派的任务
export function PendingApprovals({ here, from }: { here: (a: Approval) => boolean; from: (a: Approval) => boolean }) {
  const approvals = useStore(s => s.approvals)
  const bots = useStore(s => s.bots)
  const list = Object.values(approvals)
  const pending = list.filter(a => here(a) && (a.status === 'pending' || Date.now() / 1000 - a.ts < 600))
  const elsewhere = list.filter(a => from(a) && !here(a) && a.status === 'pending')
  return <>
    {pending.map(a => <ApprovalCard key={a.id} a={a} />)}
    {elsewhere.map(a => {
      const b = bots.find(x => x.id === a.bot_id)
      return (
        <div key={a.id} className="elsewhere">
          <span>⚠ {a.kind === 'help' ? t('{name} 的任务 #{id} 需要你帮忙，卡片在它的私聊里', { name: b?.name || '', id: a.task_id })
            : a.tool === 'generate_image' ? t('{name} 想画一张图，等你确认，卡片在它的私聊里', { name: b?.name || '' })
            : a.tool === 'create_bot' ? t('{name} 想新建一个 Bot，等你确认，卡片在它的私聊里', { name: b?.name || '' })
            : a.tool === 'update_bot' ? t('{name} 想修改一个 Bot，等你确认，卡片在它的私聊里', { name: b?.name || '' })
            : a.tool === 'delete_bot' ? t('{name} 想删除一个 Bot，等你确认，卡片在它的私聊里', { name: b?.name || '' })
            : t('{name} 的任务 #{id} 需要你确认，卡片在它的私聊里', { name: b?.name || '', id: a.task_id })}</span>
          {b && <button className="btn" onClick={() => openChat(b.chat_id)}>{t('去处理')}</button>}
        </div>
      )
    })}
  </>
}

export default function Chat({ onToggleDetails }: { onToggleDetails: () => void }) {
  const active = useStore(s => s.active)
  const chat = useStore(s => chatById(s, s.active))
  const allBots = useStore(s => s.bots)
  const messages = useStore(s => (s.active ? s.messages[s.active] : undefined))
  const stream = useStore(s => (s.active ? s.streams[s.active] : undefined))
  const hasMore = useStore(s => (s.active ? s.hasMore[s.active] : false))
  const error = useStore(s => (s.active ? s.errors[s.active] : undefined))
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const prevHeight = useRef(0)
  const lastSeen = useRef<{ chat: number | null; id: number }>({ chat: null, id: 0 })
  const [below, setBelow] = useState(0)         // 往上翻着时新来了几条消息：底部显示“↓ N 条新消息”
  const [threadsOpen, setThreadsOpen] = useState(false)
  // 底部浮着提醒时，消息列表下面留出地方，滚到底也不被挡住
  const floating = useStore(s => !!s.active && freshIn(s.threads, s.active, s.thread?.id).length > 0) || below > 0

  // 新消息到达时，只有用户本来就在底部才自动滚到底；不在底部就记下来了几条
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (stick.current) el.scrollTop = el.scrollHeight
    else if (prevHeight.current && el.scrollTop < 40) el.scrollTop = el.scrollHeight - prevHeight.current
    prevHeight.current = el.scrollHeight
    const last = messages?.length ? messages[messages.length - 1].id : 0
    const before = lastSeen.current
    if (before.chat === active && before.id && last > before.id && !stick.current) {
      const n = messages!.filter(m => m.id > before.id && m.role !== 'user').length
      if (n) setBelow(b => b + n)
    }
    lastSeen.current = { chat: active, id: last }
  }, [messages, stream?.text, stream?.thinking, floating])
  useEffect(() => { stick.current = true; setBelow(0) }, [active])

  const onScroll = () => {
    const el = scroller.current!
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (stick.current && below) setBelow(0)
    if (el.scrollTop < 40 && hasMore && active) { prevHeight.current = el.scrollHeight; loadOlder(active) }
  }
  const toBottom = () => { const el = scroller.current; if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }); setBelow(0) }

  if (!active || !chat) return <main className="chat empty-main" />
  const group = chat.kind === 'group'
  const botOf = (id: number | null | undefined) => allBots.find(b => b.id === id)
  const members = chat.bot_ids.map(botOf).filter((b): b is Bot => !!b)
  const bot = group ? undefined : members[0]
  if (!group && !bot) return <main className="chat empty-main" />
  const lead = botOf(chat.lead)
  const empty = messages && messages.length === 0 && !stream
  const title = group ? chat.title : bot!.name
  const starters = group && members.length >= 2
    ? [t('@所有人 先各自介绍一下你们能做什么'), t('@{a} 查一下今天的 AI 新闻，查完交给 @{b} 写一段 100 字的总结', { a: members[0].name, b: members[1].name })]
    : STARTERS

  return (
    <main className="chat">
      <header className="chat-head">
        <button className="head-bot" onClick={onToggleDetails} title={group ? t('群聊设置') : t('Bot 详情')}>
          {group ? <GroupFace image={chat.image} bots={members} size={26} /> : <BotAvatar avatar={bot!.avatar} size={26} />}
          <span className="head-name">{title}</span>
          {group ? <span className="muted small">{t('{n} 个 Bot', { n: members.length })}</span> : bot!.is_primary && <span className="star" title={t('主 Bot')}>★</span>}
        </button>
        <div className="head-actions">
          <button className="ghost-btn" title={t('之前的对话不再带进上下文，省 token；长期记忆还在')} onClick={async () => {
            if (await ask(group ? t('开始新话题？群里的 Bot 不再参考之前的对话（记录还在，长期记忆也还在）。') : t('开始新话题？{name} 不再参考之前的对话（记录还在，长期记忆也还在）。', { name: bot!.name }), { ok: t('新话题') })) api(`/api/chats/${active}/reset`, { method: 'POST' })
          }}>{t('新话题')}</button>
          <button className={'icon-btn' + (chat.pinned ? ' on' : '')} onClick={() => pinChat(chat.id, !chat.pinned)}
                  title={chat.pinned ? t('取消置顶') : t('置顶到侧栏上方')}><Icon name="pin" size={16} /></button>
          <ThreadsMenu chat={chat} open={threadsOpen} setOpen={setThreadsOpen} />
          <button className="icon-btn" onClick={onToggleDetails} title={t('详情')}>☰</button>
        </div>
      </header>
      <div className="scroller" ref={scroller} onScroll={onScroll}>
        <div className={'thread' + (floating ? ' floating' : '')}>
          {empty && (group ? (
            <div className="welcome">
              <GroupFace image={chat.image} bots={members} size={72} stack={56} />
              <h2>{chat.title}</h2>
              <p className="muted">{lead ? t('@ 某个 Bot 让它来做，@所有人 让大家都说说；不 @ 的消息由协调者 {lead} 接，它会分给合适的 Bot。', { lead: lead.name }) : t('@ 某个 Bot 让它来做，@所有人 让大家都说说；不 @ 的消息由协调者接，它会分给合适的 Bot。')}
                {' '}{t('Bot 之间可以交接工作；要你确认的操作会出现在各自的私聊里。')}</p>
              <div className="starters">{starters.map(s => <button key={s} onClick={() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: s }))}>{s}</button>)}</div>
            </div>
          ) : (
            <div className="welcome">
              <BotAvatar avatar={bot!.avatar} size={72} />
              <h2>{bot!.name}</h2>
              <p className="muted">{t('给 {name} 布置第一份工作。它会记住你交代的职责，在后台替你完成任务。', { name: bot!.name })}</p>
              <div className="starters">{starters.map(s => <button key={s} onClick={() => window.dispatchEvent(new CustomEvent('osa-fill', { detail: s }))}>{s}</button>)}</div>
            </div>
          ))}
          {messages && <MessageList chat={chat} list={messages} reply={{ chat: chat.id }} />}
          <StreamMessage chat={chat} chatId={chat.id} />
          <PendingApprovals here={a => a.chat_id === active} from={a => a.origin_chat_id === active} />
          {error && <div className="error-line">{error}</div>}
        </div>
      </div>
      <div className="float-anchor">
        <div className="float-stack">
          <NewReplies chat={chat} onList={() => setThreadsOpen(true)} />
          {below > 0 && <button className="new-below" onClick={toBottom}><Icon name="chevron" size={14} />{t('{n} 条新消息', { n: below })}</button>}
        </div>
      </div>
      <SetupHint />
      <Composer chatId={active} bot={bot} members={group ? members : []}
        busy={stream ? (group ? botOf(stream.bot_id)?.name || t('有人') : bot!.name) : ''} />
    </main>
  )
}

// 对话或后台任务的模型还没法用（比如没填 API Key）：提示去设置里配好
function SetupHint() {
  const problem = useStore(s => s.settings?.problem)
  if (!problem) return null
  return (
    <div className="setup-hint">
      <span>⚠ {t('{problem}，Bot 现在没法回复。', { problem })}</span>
      <button className="btn small" onClick={() => window.dispatchEvent(new CustomEvent('osa-open-settings', { detail: 'models' }))}>{t('去配置模型')}</button>
    </div>
  )
}
