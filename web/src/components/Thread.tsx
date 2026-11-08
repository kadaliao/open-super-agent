import { useEffect, useLayoutEffect, useRef } from 'react'
import { chatById, closeThread, loadOlder, markThreadSeen, sendInThread, useStore } from '../store'
import type { Bot } from '../types'
import Composer from './Composer'
import { MessageList, PendingApprovals, StreamMessage } from './Chat'
import { t } from '../i18n'
import { useBack } from '../swipe'

// 右边的讨论串：上面是被回复的那条消息，下面是针对它的回复。Bot 在这里回答，主对话不受影响
export default function ThreadPanel() {
  const th = useStore(s => s.thread)
  const root = useRef<HTMLElement>(null)
  useBack(closeThread, root)
  const chat = useStore(s => (s.thread ? chatById(s, s.thread.chat) : undefined))
  const bots = useStore(s => s.bots)
  const list = useStore(s => (s.thread?.id ? s.messages[s.thread.id] : undefined))
  const stream = useStore(s => (s.thread?.id ? s.streams[s.thread.id] : undefined))
  const error = useStore(s => (s.thread?.id ? s.errors[s.thread.id] : undefined))
  const hasMore = useStore(s => (s.thread?.id ? s.hasMore[s.thread.id] : false))
  const unread = useStore(s => (s.thread?.id ? s.threads[s.thread.id]?.unread : 0) || 0)
  const visible = useStore(s => s.visible)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const prevHeight = useRef(0)

  // 和主对话一样：本来就在底部才跟着新消息往下滚
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    if (stick.current) el.scrollTop = el.scrollHeight
    else if (prevHeight.current && el.scrollTop < 40) el.scrollTop = el.scrollHeight - prevHeight.current
    prevHeight.current = el.scrollHeight
  }, [list, th?.root, stream?.text, stream?.thinking])
  useEffect(() => { stick.current = true }, [th?.chat, th?.rootId])
  // 开着、页面正被看着、回复也加载出来了：这个讨论串的新回复算看过（窗口藏着时来的回复，回来再算）
  useEffect(() => { if (th?.id && unread && visible && list) markThreadSeen(th.id) }, [th?.id, unread, visible, list])
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.defaultPrevented && !document.querySelector('.modal-bg')) closeThread() }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [])

  if (!th || !chat) return null
  const group = chat.kind === 'group'
  const members = chat.bot_ids.map(i => bots.find(b => b.id === i)).filter((b): b is Bot => !!b)
  const bot = group ? undefined : members[0]
  if (!group && !bot) return null
  // 和服务端算“N 条回复”的规则一样：人和 Bot 说的话、任务结果，派任务、交接这类记录不算
  const count = (list || []).filter(m => m.role === 'user' || (m.role === 'assistant' && m.content) ||
    ['result', 'dm_reply'].includes(m.meta.kind)).length
  const onScroll = () => {
    const el = scroller.current!
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (el.scrollTop < 40 && hasMore && th.id) { prevHeight.current = el.scrollHeight; loadOlder(th.id) }
  }
  const busy = stream ? (group ? bots.find(b => b.id === stream.bot_id)?.name || t('有人') : bot!.name) : ''

  return (
    <aside className="panel th-panel" ref={root}>
      <div className="panel-head">
        <div className="th-title"><b>{t('讨论串')}</b><span className="muted small">{group ? chat.title : bot!.name}</span></div>
        <button className="icon-btn" onClick={closeThread} title={t('关闭')}>✕</button>
      </div>
      <div className="scroller" ref={scroller} onScroll={onScroll}>
        <div className="th-body">
          {th.root ? <MessageList chat={chat} list={[th.root]} /> : <div className="muted small">{t('加载中…')}</div>}
          <div className="th-divider"><span>{count ? t('{n} 条回复', { n: count }) : t('针对这条消息的回复只在这里，不进主对话')}</span></div>
          {list && <MessageList chat={chat} list={list} />}
          {th.id && <StreamMessage chat={chat} chatId={th.id} />}
          {th.id && <PendingApprovals here={a => a.origin_thread === th.id && a.chat_id === th.chat} from={a => a.origin_thread === th.id} />}
          {error && <div className="error-line">{error}</div>}
        </div>
      </div>
      <Composer key={`${th.chat}:${th.rootId}`} chatId={th.id ?? 0} bot={bot} members={group ? members : []} busy={busy}
        onSend={sendInThread} inThread />
    </aside>
  )
}
