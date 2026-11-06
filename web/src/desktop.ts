// 桌面 App（Tauri 外壳）在页面加载前放进来的 window.osaDesktop。普通浏览器里没有，下面的东西都不起作用。
// 新窗口、下载由外壳自己处理。点了系统通知，外壳发一个 osa-open-chat 事件（detail 是对话 id）。
export type Bridge = {
  notify(title: string, body: string, chatId?: number, active?: boolean): void   // active：是不是当前打开的对话，外壳看窗口在前台时就不弹
  setBadge(count: number): void   // 菜单栏和程序坞上显示的待确认数
  openExternal(url: string): void // 用系统浏览器打开
  speech?: boolean                // 外壳里能用语音识别时为 true；否则不显示麦克风
}

export const desktop: Bridge | undefined = typeof window !== 'undefined' ? (window as any).osaDesktop : undefined

// 点到别的网站的链接：用系统浏览器打开，不在 App 的窗口里跳走。
// 外壳分不清是页面跳转还是页面里的 iframe 在加载，所以由页面自己在点击时处理
export function handleExternalLinks() {
  if (!desktop) return
  document.addEventListener('click', e => {
    if (e.defaultPrevented || e.button !== 0) return
    const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
    if (!a) return
    const u = new URL(a.href, location.href)
    if (u.origin === location.origin || !/^(https?|mailto):$/.test(u.protocol)) return
    e.preventDefault()
    desktop!.openExternal(u.href)
  })
}
