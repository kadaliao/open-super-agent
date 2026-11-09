import { desktop } from './desktop'

// 明亮 / 夜间：默认跟随系统，也可以在设置里固定一种。选择记在这台设备上（localStorage），不同设备各管各的。
// 页面上实际用哪种写在 <html data-theme="light|dark">，CSS 按它取颜色；index.html 里有一小段在页面画出来之前先设好，免得闪一下。
// 桌面 App 和手机 App 的外壳（标题栏、状态栏）通过 setTheme 跟着换
export type ThemePref = 'system' | 'light' | 'dark'
const KEY = 'osa.theme'
const system = matchMedia('(prefers-color-scheme: dark)')

export function themePref(): ThemePref {
  try { const v = localStorage.getItem(KEY); if (v === 'light' || v === 'dark') return v } catch { /* 无痕窗口 */ }
  return 'system'
}

export const isDark = () => document.documentElement.dataset.theme !== 'light'

// 外壳支持换主题时才能在 App 里固定（旧版手机 App 的状态栏不跟着变，固定了会看不清）
export const canSetTheme = !(desktop?.platform === 'ios' && !desktop.setTheme)

function apply() {
  const pref = canSetTheme ? themePref() : 'system'
  const theme = pref === 'system' ? (system.matches ? 'dark' : 'light') : pref
  const root = document.documentElement
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#0d0d0e' : '#fbfbfa')
  if (root.dataset.theme === theme) return
  root.dataset.theme = theme
  window.dispatchEvent(new CustomEvent('osa-theme'))
}

export function setThemePref(pref: ThemePref) {
  try { pref === 'system' ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, pref) } catch { /* 无痕窗口 */ }
  desktop?.setTheme?.(pref === 'system' ? null : pref)
  apply()
}

// 页面实际的明暗变了（换了设置、或跟随系统时系统变了）就调 fn；返回取消的函数
export function onThemeChange(fn: () => void) {
  window.addEventListener('osa-theme', fn)
  return () => window.removeEventListener('osa-theme', fn)
}

export function initTheme() {
  apply()
  system.addEventListener('change', apply)
  window.addEventListener('storage', e => { if (e.key === KEY) apply() })   // 别的标签页改了
  if (canSetTheme && desktop?.setTheme) { const p = themePref(); desktop.setTheme(p === 'system' ? null : p) }
}
