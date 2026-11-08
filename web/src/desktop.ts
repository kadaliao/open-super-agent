// 桌面 App 和手机 App（Tauri 外壳）在页面加载前放进来的 window.osaDesktop。普通浏览器里没有，下面的东西都不起作用。
// 桌面版：新窗口、下载由外壳自己处理；点了系统通知，外壳发一个 osa-open-chat 事件（detail 是对话 id）。
// 手机版：多了推送订阅、弹出页、切换服务器；点了推送通知，外壳发 osa-open-url 事件（detail 是站内地址，如 /?chat=12&thread=34）。
export type PushSub = { endpoint: string; keys: { p256dh: string; auth: string } }
// 服务器借用这台 Mac（macOS 桌面 App 连着服务器时，电脑选“本机”）：available 是能借（连着服务器），linked 借出去了没有，
// id 是服务器那边的借用编号，running 是 Mac 上的小程序在不在跑
export type LinkState = { available: boolean; linked: boolean; id: number | null; running: boolean }

export type Bridge = {
  notify(title: string, body: string, chatId?: number, active?: boolean): void   // active：是不是当前打开的对话，外壳看窗口在前台时就不弹
  setBadge(count: number): void   // 菜单栏、程序坞或手机图标上显示的待确认数
  openExternal(url: string): void // 用系统浏览器打开
  speech?: boolean                // 外壳里能用语音识别时为 true；否则不显示麦克风
  // 桌面 App：把这台 Mac 借给服务器（connect 会先在 Mac 上弹窗问你，返回你同不同意）
  link?: {
    state(): Promise<LinkState>
    connect(token: string, id: number): Promise<boolean>
    disconnect(): Promise<void>
  }
  // 下面这些只有手机 App 有
  platform?: 'macos' | 'ios'
  push?: {
    // 这台手机给这台服务器开着推送时，返回要交给 /api/push/subscribe 的订阅（中转或服务器密钥变了会自动重新登记）；没开返回 null
    current(serverKey: string): Promise<PushSub | null>
    subscribe(serverKey: string): Promise<PushSub>   // 申请通知权限、向推送中转登记
    unsubscribe(serverKey: string): Promise<void>
  }
  openSheet?(url: string): Promise<void>       // 在 App 里弹出一页打开同站地址（报告页、应用授权），关掉时完成
  switchServer?(): void                        // 回到 App 的连接页
  takePendingOpen?(): Promise<string | null>   // 点通知冷启动 App 时要打开的站内地址
}

export const desktop: Bridge | undefined = typeof window !== 'undefined' ? (window as any).osaDesktop : undefined
export const phone = desktop?.platform === 'ios'

// 点到别的网站的链接：用系统浏览器打开，不在 App 的窗口里跳走。
// 外壳分不清是页面跳转还是页面里的 iframe 在加载，所以由页面自己在点击时处理。
// 手机 App 里没有新窗口：同站的新窗口链接（报告页、文件）在弹出页里打开
export function handleExternalLinks() {
  if (!desktop) return
  document.addEventListener('click', e => {
    if (e.defaultPrevented || e.button !== 0) return
    const a = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
    if (!a) return
    const u = new URL(a.href, location.href)
    if (u.origin === location.origin) {   // 下载链接交给外壳的下载处理
      if (desktop!.openSheet && a.target === '_blank' && !a.hasAttribute('download')) { e.preventDefault(); desktop!.openSheet(u.href) }
      return
    }
    if (!/^(https?|mailto):$/.test(u.protocol)) return
    e.preventDefault()
    desktop!.openExternal(u.href)
  })
}
