import { t } from './i18n'

// 预览用的一行纯文字（侧栏、讨论串列表、新回复提醒）：去掉任务标记和 Markdown 符号
export const plain = (s: string) => s === '[新话题]' ? t('— 新话题 —') : s.replace(/\[附件\] ([^\n]+)/g, (_, p: string) => '📎 ' + p.split('/').pop()).replace(/!\[([^\]]*)\]\([^)]*\)/g, '🖼 $1').replace(/^\[[^\]]+\]\s*/, '').replace(/[#*`>_~|]+/g, '').replace(/\s+/g, ' ').trim()   // i18n-ignore：存在消息里的标记
