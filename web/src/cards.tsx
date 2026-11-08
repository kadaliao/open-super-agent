import type { ReactNode } from 'react'
import type { Bot, Chat as ChatT, Message, Task } from './types'

// 对话里按消息种类（meta.kind）显示的卡片注册表。内置的在 Chat.tsx 里注册（任务、结果、画的图、交接、私信……），
// 新的一种消息只要 registerCard 一下，Chat 的渲染不用改。和后端的 osa/registry.py 一样，注册返回撤销函数。

export type CardProps = {
  m: Message
  kind: string
  taskId?: number
  chat: ChatT
  group: boolean
  bot?: Bot                                   // 说话的 Bot（群聊里按消息的 bot_id，私聊里就是这个 Bot）
  botOf: (id: number | null | undefined) => Bot | undefined
  reply?: { chat: number }
  tasks: Record<number, Task>
  byId: Map<number, Message>
  attached: Set<number>                       // 已经挂在某条回复下面显示的任务卡片
  task: (tid: number) => ReactNode            // 某个任务的卡片
}
export type Card = (p: CardProps) => ReactNode

const CARDS = new Map<string, Card>()

export function registerCard(kind: string, card: Card) {
  CARDS.set(kind, card)
  return () => { if (CARDS.get(kind) === card) CARDS.delete(kind) }
}

export const cardFor = (kind: string) => CARDS.get(kind)
