// 任务、消息所属的卡（看板上的需求）：编号加标题的小标记，点了打开卡片页
import type { MouseEvent } from 'react'
import { openNeed } from '../store'
import { t } from '../i18n'
import { Icon } from '../icons'
import { useNeedRef } from '../board'
import type { NeedRef } from '../types'

// need：已经知道的简称（任务字段里带的）；只有 id 时（消息的 meta.need_id）自己去取。from：消息是从看板开始卡片时替你发的
export default function NeedTag({ need, id, from }: { need?: NeedRef | null; id?: number | null; from?: boolean }) {
  const ref = useNeedRef(need?.id ?? id, need)
  const nid = need?.id ?? id
  if (!nid) return null
  const open = (e: MouseEvent) => { e.stopPropagation(); openNeed(nid) }
  return (
    <button className="need-tag" onClick={open} title={from ? t('从看板上的这张卡开始的，点了打开卡片') : t('属于看板上的这张卡，点了打开卡片')}>
      <Icon name="kanban" size={14} />
      {from && <span className="need-tag-from">{t('来自看板')}</span>}
      {ref?.key && <b>{ref.key}</b>}
      {/* 替你发的那条消息开头就是编号或标题：有编号时不再重复标题 */}
      {!(from && ref?.key) && <span className="need-tag-title">{ref ? ref.title : `#${nid}`}</span>}
    </button>
  )
}
