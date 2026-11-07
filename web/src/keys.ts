import type { KeyboardEvent } from 'react'

// 按了回车，而且不是输入法里的回车：用拼音选字、把拼音原样上屏时按的回车不算。
// Chrome 里这时 isComposing 是 true；Safari 和桌面 / 手机 App（WebKit）先结束拼字再发 keydown，
// isComposing 已经变回 false，只能靠 keyCode 229（“输入法在处理这个键”）认出来。
export const isEnter = (e: KeyboardEvent) => e.key === 'Enter' && !e.nativeEvent.isComposing && e.keyCode !== 229
