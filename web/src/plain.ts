import { createElement, Fragment } from 'react'
import { t } from './i18n'
import { Icon } from './icons'

// 预览用的一行纯文字（侧栏、讨论串列表、新回复提醒）：去掉任务标记和 Markdown 符号
export const plain = (s: string) => s === '[新话题]' ? t('— 新话题 —') : s.replace(/\[附件\] ([^\n]+)/g, (_, p: string) => '📎 ' + p.split('/').pop()).replace(/!\[([^\]]*)\]\([^)]*\)/g, '🖼 $1').replace(/^\[[^\]]+\]\s*/, '').replace(/[#*`>_~|]+/g, '').replace(/\s+/g, ' ').trim()   // i18n-ignore：存在消息里的标记

// 同一行预览，plain 留下的附件、图片记号（📎 🖼）换成图标
export const Preview = ({ text }: { text: string }) => createElement(Fragment, null, ...plain(text).split(/(📎|🖼) ?/u).map((x, i) =>
  x === '📎' ? createElement(Icon, { key: i, name: 'paperclip', size: 14, className: 'lead' })
    : x === '🖼' ? createElement(Icon, { key: i, name: 'image', size: 14, className: 'lead' }) : x))
