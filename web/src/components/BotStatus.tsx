import { taskWaiting, useStore } from '../store'
import { lang, t } from '../i18n'
import { Icon } from '../icons'
import { plain } from '../plain'
import type { Approval, Bot, Chat, Task } from '../types'

// 一个对话现在的状态，侧栏和对话头部共用：
// wait：有要你处理的（回答 / 接手 / 确认），比“在做”优先；away：群里派出去的任务在某个 Bot 的私聊里等你确认；
// doing：正在做的任务（最近开始的那个）的目标，n 是一共几个；busy：有任务在做，或者 Bot 正在回话（replying）
export type ChatStatus = {
  wait: 'question' | 'help' | 'approval' | null
  away: boolean
  doing: string
  n: number
  replying: boolean
  busy: boolean
}

// 任务目标的第一行，去掉 Markdown 符号，给“正在做：……”用
const firstLine = (s: string) => s.split('\n').map(x => x.trim()).find(Boolean)?.replace(/[#*`>_~|]+/g, '').trim() || ''

export function chatStatus(c: Chat, tasks: Record<number, Task>, approvals: Record<number, Approval>, replying = false): ChatStatus {
  const pending = Object.values(approvals).filter(a => a.status === 'pending')
  const here = pending.find(a => a.chat_id === c.id)
  const away = c.kind === 'group' && pending.some(a => a.origin_chat_id === c.id)
  // 在做的：没在等你的。等你的那些由 wait / away 说
  const running = Object.values(tasks).filter(x => x.chat_id === c.id && x.status === 'running' && !taskWaiting(x)).sort((a, b) => b.started - a.started)
  return {
    wait: here ? (here.kind || 'approval') : null,
    away,
    doing: running.length ? firstLine(running[0].goal) : '',
    n: running.length,
    replying,
    busy: running.length > 0 || replying,
  }
}

// 正在回话的对话（流式输出还没完）：返回 id 串成的字符串，只在开始、结束时变，不会每来一个字就重画
export function useReplying(): Set<number> {
  const ids = useStore(s => Object.keys(s.streams).filter(k => s.streams[Number(k)]).join(','))
  return new Set(ids ? ids.split(',').map(Number) : [])
}

// 头像角上的状态点：忙是绿色（脉冲），等你是警示色；空闲不画
export function StatusDot({ s }: { s: ChatStatus }) {
  const wait = !!s.wait || s.away
  if (!wait && !s.busy) return null
  return <span className={'st-dot' + (wait ? ' wait' : ' busy')} />
}

// 第二行的状态：等你处理的（警示色图标加字）、正在做的（脉冲圆点加任务目标）、正在回话的；都没有返回 null，各处显示自己平时的那句
export function statusLine(s: ChatStatus) {
  if (s.wait) return <span className="st-line wait"><Icon name="alert" size={14} className="warn" />{s.wait === 'question' ? t('等你回答') : s.wait === 'help' ? t('请你接手') : t('等你确认')}</span>
  if (s.away) return <span className="st-line wait"><Icon name="alert" size={14} className="warn" />{t('有 Bot 等你确认（在私聊里）')}</span>
  if (s.n) return <span className="st-line busy"><span className="st-pulse" />{s.n > 1 ? t('正在做 {n} 件事：{goal}', { n: s.n, goal: s.doing }) : t('正在做：{goal}', { goal: s.doing })}</span>
  if (s.replying) return <span className="st-line busy"><span className="st-pulse" />{t('正在回复…')}</span>
  return null
}

// 对话头部的第二行：等你处理的、正在做的（和侧栏同一套说法），都没有时是一句职责（私聊）或成员（群聊）
// 设定是写给模型的，多半以“你是 Radar，负责……”“你是我的邮件管家，……”开头：原样显示像把提示词露出来了。
// 取第一行的第一句，去掉开头对模型的称呼，剩下的才是给人看的职责；认不出这种开头就原样显示
const reEsc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
export const role = (s: string, name = '') => {
  const first = plain(s.split('\n').find(l => l.trim()) || '').split(/(?<=[。！？!?；;])\s*|\.\s+/)[0] || ''   // i18n-ignore
  const n = name.trim() && reEsc(name.trim())
  const cuts = [
    n && new RegExp(`^(?:你是|你叫|你的名字是|我是)\\s*${n}\\s*[，,、：:；;—-]+\\s*`),   // i18n-ignore
    n && new RegExp(`^you(?:'re|\\s+are)\\s+${n}\\s*[,:;—-]+\\s*`, 'i'),
    /^你是(?:我的|一个|一位|一名|个)?\s*/, /^你(?:帮我|替我)\s*/, /^你(?=负责)/,   // i18n-ignore
    /^you(?:'re|\s+are)\s+(?:(?:my|an?|the)\s+)?/i, /^you\s+help\s+me\s+(?:to\s+)?/i,
  ].filter((r): r is RegExp => !!r)
  const hit = cuts.find(r => r.test(first))
  const out = (hit ? first.replace(hit, '') : first).replace(/[。.；;，,：:\s]+$/, '')   // i18n-ignore
  return out.charAt(0).toUpperCase() + out.slice(1)
}
export const names = (bots: Bot[]) => bots.map(b => b.name).join(lang === 'en' ? ', ' : '、')
export function HeadStatus({ chat, sub }: { chat: Chat; sub: string }) {
  const tasks = useStore(s => s.tasks)
  const approvals = useStore(s => s.approvals)
  const replying = useReplying().has(chat.id)
  const line = statusLine(chatStatus(chat, tasks, approvals, replying))
  return line ? <span className="head-sub">{line}</span> : sub ? <span className="head-sub">{sub}</span> : null
}
